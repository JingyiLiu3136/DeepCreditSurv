from DeepCreditSurv.models.DATE.preprocessing import flatten_nested, risk_set, get_missing_mask
from DeepCreditSurv.models.DATE.utilities import batch_metrics, l1_loss, l2_loss, plot_predicted_distribution
from DeepCreditSurv.models.DATE.tf_helpers import create_nn_weights, mlp_neuron, hidden_mlp_layers, uniform, \
    show_all_variables, hidden_mlp_layers_noise

import numpy as np

import tensorflow as tf
import tensorflow.contrib.slim as slim

import logging
import math
import os
import threading
import time
from datetime import timedelta

from lifelines.utils import concordance_index
from scipy.stats.stats import spearmanr

import pprint
import sys


class DATE_AE(object):
    def __init__(self,
                 batch_size,
                 learning_rate,
                 beta1,
                 beta2,
                 require_improvement,
                 seed,
                 num_iterations,
                 hidden_dim,
                 latent_dim,
                 input_dim,
                 num_examples,
                 keep_prob,
                 train_data,
                 valid_data,
                 test_data,
                 end_t,
                 gen_updates,
                 covariates,
                 imputation_values,
                 sample_size,
                 disc_updates,
                 categorical_indices,
                 l2_reg,
                 max_epochs,
                 path_large_data=""
                 ):
        self.max_epochs = max_epochs
        self.hidden_dim = hidden_dim
        self.batch_size = batch_size
        self.disc_updates = disc_updates
        self.gen_updates = gen_updates
        self.latent_dim = latent_dim
        self.path_large_data = path_large_data
        self.seed = seed
        self.require_improvement = require_improvement
        self.num_iterations = num_iterations
        self.learning_rate, self.beta1, self.beta2 = learning_rate, beta1, beta2
        self.l2_reg = l2_reg
        self.log_file = 'model.log'
        logging.basicConfig(filename=self.log_file, filemode='w', level=logging.DEBUG)
        np.random.seed(seed)
        tf.set_random_seed(seed)
        self.batch_norm = True
        self.covariates = covariates
        self.sample_size = sample_size
        self.z_sample_size = 10  # num of z_samples

        self.config = tf.ConfigProto(log_device_placement=False, allow_soft_placement=True)
        self.config.gpu_options.allow_growth = True
        # self.config.gpu_options.per_process_gpu_memory_fraction = gpu_memory_fraction
        # Load Data
        self.train_x, self.train_t, self.train_e = train_data['x'], train_data['t'], train_data['e']
        self.valid_x, self.valid_t, self.valid_e = valid_data['x'], valid_data['t'], valid_data['e']

        self.test_x, self.test_t, self.test_e = test_data['x'], test_data['t'], test_data['e']
        self.end_t = end_t
        self.keep_prob = keep_prob
        self.input_dim = input_dim
        self.imputation_values = imputation_values
        self.imputation_values = np.zeros(shape=self.input_dim)
        self.num_examples = num_examples
        self.categorical_indices = categorical_indices
        self.continuous_indices = np.setdiff1d(np.arange(input_dim), flatten_nested(categorical_indices))
        print_features = "input_dim:{}, continuous:{}, size:{}, categorical:{}, " \
                         "size{}".format(self.input_dim,
                                         self.continuous_indices,
                                         len(
                                             self.continuous_indices),
                                         self.categorical_indices,
                                         len(
                                             self.categorical_indices))
        print(print_features)
        logging.debug(print_features)
        print_model = "model is DATE_AE"
        print(print_model)
        logging.debug("Imputation values:{}".format(imputation_values))
        logging.debug(print_model)
        self.model = 'DATE_AE'

        self._build_graph()
        self.train_cost, self.train_ci, self.train_t_rae, self.train_gen, self.train_disc, self.train_ranking, \
        self.train_layer_one_recon = [], [], [], [], [], [], []
        self.valid_cost, self.valid_ci, self.valid_t_rae, self.valid_gen, self.valid_disc, self.valid_ranking, \
        self.valid_layer_one_recon = [], [], [], [], [], [], []

    def _build_graph(self):
        self.G = tf.Graph()
        with self.G.as_default():
            self.x = tf.placeholder(tf.float32, shape=[None, self.input_dim], name='x')
            self.e = tf.placeholder(tf.float32, shape=[None], name='e')
            self.t = tf.placeholder(tf.float32, shape=[None], name='t')
            self.t_lab = tf.placeholder(tf.float32, shape=[None], name='t_lab')
            # are used to feed data into our queue
            self.batch_size_tensor = tf.placeholder(tf.int32, shape=[], name='batch_size')
            self.risk_set = tf.placeholder(tf.float32, shape=[None, None])
            self.impute_mask = tf.placeholder(tf.float32, shape=[None, self.input_dim], name='impute_mask')
            self.is_training = tf.placeholder(tf.bool)
            self.noise_dim = len(self.hidden_dim) + 1
            self.noise_alpha = tf.placeholder(tf.float32, shape=[self.noise_dim])

            self._objective()
            self.session = tf.Session(config=self.config)

            self.capacity = 1400
            self.coord = tf.train.Coordinator()
            enqueue_thread = threading.Thread(target=self.enqueue)
            self.queue = tf.RandomShuffleQueue(capacity=self.capacity, dtypes=[tf.float32, tf.float32, tf.float32],
                                               shapes=[[self.input_dim], [], []], min_after_dequeue=self.batch_size)
            # self.queue = tf.FIFOQueue(capacity=self.capacity, dtypes=[tf.float32, tf.float32, tf.float32],
            #                           shapes=[[self.input_dim], [], []])
            self.enqueue_op = self.queue.enqueue_many([self.x, self.t, self.e])
            # enqueue_thread.isDaemon()
            enqueue_thread.start()
            dequeue_op = self.queue.dequeue()
            self.x_batch, self.t_batch, self.e_batch = tf.train.batch(dequeue_op, batch_size=self.batch_size,
                                                                      capacity=self.capacity)
            self.threads = tf.train.start_queue_runners(coord=self.coord, sess=self.session)

            self.saver = tf.train.Saver()
            self.merged = tf.summary.merge_all()
            self.current_dir = os.getcwd()
            self.save_path = "D:\\DeepCreditSurv\\DeepCreditSurv\\models\\DATE\\summaries\\".format(
                self.model)
            self.train_writer = tf.summary.FileWriter(self.save_path, self.session.graph)

    def _objective(self):
        self.num_batches = self.num_examples / self.batch_size
        logging.debug("num batches:{}, batch_size:{} epochs:{}".format(self.num_batches, self.batch_size,
                                                                       int(self.num_iterations / self.num_batches)))
        self._build_model()
        self.reg_loss = l2_loss(self.l2_reg) + l1_loss(self.l2_reg)
        self.cost = self.t_regularization_loss + self.disc_one_loss + self.disc_two_loss + self.gen_one_loss + \
                    self.gen_two_loss + self.layer_one_recon
        optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate, beta1=self.beta1,
                                           beta2=self.beta2)

        dvars1 = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "Discriminator_one")
        dvars2 = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "Discriminator_two")
        genvars1 = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "generate_t_given_x")
        genvars2 = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "generate_t_given_z")

        self.disc_solver = optimizer.minimize(self.disc_one_loss + self.disc_two_loss, var_list=dvars1 + dvars2)
        self.gen_solver = optimizer.minimize(
            self.gen_one_loss + self.gen_two_loss + self.t_regularization_loss + self.layer_one_recon,
            var_list=genvars1 + genvars2)

    def _build_model(self):
        self._denoising_date()
        self._risk_date()

    @staticmethod
    def log(x):
        return tf.log(x + 1e-8)

    def _risk_date(self):
        def expand_t_dim(t):
            return tf.expand_dims(t, axis=1)

        indices_lab = tf.where(tf.equal(tf.constant(1.0, dtype=tf.float32), self.e))
        z_lab = tf.squeeze(tf.gather(self.z_real, indices_lab), axis=[1])
        t_lab_exp = expand_t_dim(self.t_lab)

        t_gen = pt_given_z(z=self.z_real, hidden_dim=self.hidden_dim, is_training=self.is_training,
                           batch_norm=self.batch_norm, keep_prob=self.keep_prob, batch_size=self.batch_size_tensor,
                           latent_dim=self.latent_dim, noise_alpha=self.noise_alpha)

        # Discriminator B
        d_two_real, f_two_real = discriminator_two(pair_one=z_lab, pair_two=t_lab_exp, hidden_dim=self.hidden_dim,
                                                   is_training=self.is_training, batch_norm=self.batch_norm,
                                                   keep_prob=self.keep_prob)  # (z_nc, t_nc)
        d_two_fake, f_two_fake = discriminator_two(pair_one=self.z_real, pair_two=t_gen, hidden_dim=self.hidden_dim,
                                                   is_training=self.is_training, batch_norm=self.batch_norm,
                                                   reuse=True, keep_prob=self.keep_prob)  # (z, t_gen)

        # Discriminator loss
        self.disc_two_loss = -tf.reduce_mean(self.log(d_two_real)) - tf.reduce_mean(self.log(1 - d_two_fake))

        # Generator loss
        self.gen_two_loss = tf.reduce_mean(f_two_real) - tf.reduce_mean(f_two_fake)
        self.disc_logit = d_two_fake
        self.disc_f = f_two_fake
        self.predicted_time = tf.squeeze(t_gen)
        self.ranking_partial_lik, self.total_rae, self.total_t_recon_loss = \
            batch_metrics(e=self.e,
                          risk_set=self.risk_set,
                          predicted=self.predicted_time,
                          batch_size=self.batch_size_tensor,
                          empirical=self.t)

        # self.t_regularization_loss = tf.add(self.ranking_partial_lik, self.total_t_recon_loss)
        self.t_regularization_loss = self.total_t_recon_loss
        self.t_mse = tf.losses.mean_squared_error(labels=self.t_lab,
                                                  predictions=tf.gather(self.predicted_time, indices_lab))

    def _denoising_date(self):
        self.z_real = generate_z_given_x(latent_dim=self.latent_dim,
                                         is_training=self.is_training,
                                         batch_norm=self.batch_norm,
                                         input_dim=self.input_dim, batch_size=self.batch_size_tensor,
                                         hidden_dim=self.hidden_dim, x=self.impute_mask, keep_prob=self.keep_prob,
                                         reuse=True, sample_size=self.z_sample_size)

        z_ones = np.ones(shape=self.latent_dim, dtype=np.float32)
        print("z_ones:{}".format(z_ones.shape))

        z_fake = tf.distributions.Uniform(low=-z_ones, high=z_ones).sample(sample_shape=[self.batch_size_tensor])
        x_fake = generate_x_given_z(z=z_fake, latent_dim=self.latent_dim,
                                    is_training=self.is_training, batch_norm=self.batch_norm,
                                    hidden_dim=self.hidden_dim, keep_prob=self.keep_prob,
                                    batch_size=self.batch_size_tensor, input_dim=self.input_dim)

        self.x_recon = generate_x_given_z(z=self.z_real, latent_dim=self.latent_dim,
                                          is_training=self.is_training, batch_norm=self.batch_norm,
                                          hidden_dim=self.hidden_dim, reuse=True, keep_prob=self.keep_prob,
                                          batch_size=self.batch_size_tensor, input_dim=self.input_dim)

        z_rec = generate_z_given_x(x=x_fake, latent_dim=self.latent_dim,
                                   is_training=self.is_training,
                                   batch_norm=self.batch_norm,
                                   input_dim=self.input_dim, batch_size=self.batch_size_tensor,
                                   hidden_dim=self.hidden_dim, reuse=True, keep_prob=self.keep_prob,
                                   sample_size=self.z_sample_size)
        # Reconstruction Loss

        self.x_recon_loss = x_reconstruction(x_recon=self.x_recon, x=self.x,
                                             categorical_indices=self.categorical_indices,
                                             continuous_indices=self.continuous_indices,
                                             batch_size=self.batch_size_tensor)

        self.z_recon_loss = tf.losses.mean_squared_error(z_fake, z_rec)
        self.layer_one_recon = tf.add(self.x_recon_loss, self.z_recon_loss)

        d_one_real, f_one_real = discriminator_one(pair_one=self.impute_mask, pair_two=self.z_real,
                                                   hidden_dim=self.hidden_dim,
                                                   is_training=self.is_training, batch_norm=self.batch_norm,
                                                   keep_prob=self.keep_prob)  # real
        d_one_fake, f_one_fake = discriminator_one(pair_one=x_fake, pair_two=z_fake, hidden_dim=self.hidden_dim,
                                                   is_training=self.is_training, batch_norm=self.batch_norm,
                                                   reuse=True, keep_prob=self.keep_prob)  # fake

        self.disc_one_loss = -tf.reduce_mean(self.log(d_one_real)) - tf.reduce_mean(self.log(1 - d_one_fake))

        # Generator loss
        self.gen_one_loss = tf.reduce_mean(f_one_real) - tf.reduce_mean(f_one_fake)

    def predict_concordance_index(self, x, t, e, outcomes=None):
        input_size = x.shape[0]
        i = 0
        num_batches = input_size / self.batch_size
        predicted_time = np.zeros(shape=input_size, dtype=np.int)
        total_ranking = 0.0
        total_rae = 0.0
        total_cost = 0.0
        total_gen_loss = 0.0
        total_disc_loss = 0.0
        total_layer_one_recon = 0.0
        total_t_reg_loss = 0.0
        total_reg = 0.0
        total_mse = 0.0
        while i < input_size:
            # The ending index for the next batch is denoted j.
            j = min(i + self.batch_size, input_size)
            feed_dict = self.batch_feed_dict(e=e, i=i, j=j, t=t, x=x, outcomes=outcomes)
            cost, ranking, gen_loss, rae, reg, disc_loss, layer_one_recon, t_reg_loss, t_mse = self.session.run(
                [self.cost, self.ranking_partial_lik, self.gen_one_loss, self.total_rae,
                 self.reg_loss,
                 self.disc_one_loss, self.layer_one_recon, self.t_regularization_loss, self.t_mse],
                feed_dict=feed_dict)

            temp_pred_time = []
            for p in range(self.sample_size):
                gen_time = self.session.run(self.predicted_time, feed_dict=feed_dict)
                temp_pred_time.append(gen_time)

            temp_pred_time = np.array(temp_pred_time)
            # print("temp_pred_time:{}".format(temp_pred_time.shape))
            predicted_time[i:j] = np.median(temp_pred_time, axis=0)

            total_ranking += ranking
            total_cost += cost
            total_rae += rae
            total_gen_loss += gen_loss
            total_reg += reg
            total_layer_one_recon += layer_one_recon
            total_disc_loss += disc_loss
            total_t_reg_loss += t_reg_loss
            total_mse += t_mse
            i = j

        predicted_event_times = predicted_time.reshape(input_size)

        ci_index = concordance_index(event_times=t, predicted_scores=predicted_event_times.tolist(),
                                     event_observed=e)

        # ci_index = 0
        def batch_average(total):
            return total / num_batches

        return ci_index, batch_average(total_cost), batch_average(total_rae), batch_average(
            total_ranking), batch_average(
            total_gen_loss), batch_average(total_reg), batch_average(total_disc_loss), batch_average(
            total_layer_one_recon), batch_average(total_t_reg_loss), batch_average(total_mse)

    def batch_feed_dict(self, e, i, j, t, x, outcomes):
        batch_x = x[i:j, :]
        batch_t = t[i:j]
        batch_risk = risk_set(batch_t)
        batch_impute_mask = get_missing_mask(batch_x, self.imputation_values)
        batch_e = e[i:j]
        idx_observed = batch_e == 1
        feed_dict = {self.x: batch_x,
                     self.impute_mask: batch_impute_mask,
                     self.t: batch_t,
                     self.t_lab: batch_t[idx_observed],
                     self.e: batch_e,
                     self.risk_set: batch_risk,
                     self.batch_size_tensor: len(batch_t),
                     self.is_training: False,
                     self.noise_alpha: np.ones(shape=self.noise_dim)}
        # TODO replace with abstract methods

        updated_feed_dic = self.outcomes_function(idx=i, j=j, feed_dict=feed_dict, outcomes=outcomes)
        return updated_feed_dic

    def outcomes_function(self, idx, j, feed_dict, outcomes):
        return feed_dict

    def train_neural_network(self):
        train_print = "Training {0} Model:".format(self.model)
        params_print = "Parameters:, l2_reg:{}, learning_rate:{}," \
                       " momentum: beta1={} beta2={}, batch_size:{}, batch_norm:{}," \
                       " hidden_dim:{}, latent_dim:{}, num_of_batches:{}, keep_prob:{}, disc_update:{}" \
            .format(self.l2_reg, self.learning_rate, self.beta1, self.beta2, self.batch_size,
                    self.batch_norm, self.hidden_dim, self.latent_dim, self.num_batches, self.keep_prob,
                    self.disc_updates)
        print(train_print)
        print(params_print)
        logging.debug(train_print)
        logging.debug(params_print)
        self.session.run(tf.global_variables_initializer())

        best_ci = 0
        best_t_reg = np.inf
        best_validation_epoch = 0
        last_improvement = 0

        start_time = time.time()
        epochs = 0
        show_all_variables()
        j = 0

        for i in range(self.num_iterations):
            # Batch Training
            run_options = tf.RunOptions(timeout_in_ms=4000)
            x_batch, t_batch, e_batch = self.session.run([self.x_batch, self.t_batch, self.e_batch],
                                                         options=run_options)
            risk_batch = risk_set(data_t=t_batch)
            batch_impute_mask = get_missing_mask(x_batch, self.imputation_values)
            batch_size = len(t_batch)
            idx_observed = e_batch == 1
            # TODO simplify batch processing
            feed_dict_train = {self.x: x_batch,
                               self.impute_mask: batch_impute_mask,
                               self.t: t_batch,
                               self.t_lab: t_batch[idx_observed],
                               self.e: e_batch,
                               self.risk_set: risk_batch, self.batch_size_tensor: batch_size, self.is_training: True,
                               self.noise_alpha: np.ones(shape=self.noise_dim)}
            for k in range(self.disc_updates):
                _ = self.session.run([self.disc_solver], feed_dict=feed_dict_train)

            for m in range(self.gen_updates):
                _ = self.session.run([self.gen_solver], feed_dict=feed_dict_train)

            summary, train_time, train_cost, train_ranking, train_rae, train_reg, train_gen, train_layer_one_recon, \
            train_t_reg, train_t_mse, train_disc = self.session.run(
                [self.merged, self.predicted_time, self.cost, self.ranking_partial_lik, self.total_rae,
                 self.reg_loss, self.gen_one_loss, self.layer_one_recon, self.t_regularization_loss, self.t_mse,
                 self.disc_one_loss],
                feed_dict=feed_dict_train)
            try:

                # train_ci = 0.0
                train_ci = concordance_index(event_times=t_batch,
                                             predicted_scores=train_time.reshape(t_batch.shape),
                                             event_observed=e_batch)
            except IndexError:
                train_ci = 0.0
                print("C-Index IndexError")

            tf.verify_tensor_all_finite(train_cost, "Training Cost has Nan or Infinite")
            if j >= self.num_examples:
                epochs += 1
                is_epoch = True
                # idx = 0
                j = 0
            else:
                # idx = j
                j += self.batch_size
                is_epoch = False

            if i % 100 == 0:
                train_print = "it:{}, trainCI:{}, train_ranking:{}, train_RAE:{},  train_Gen:{}, train_Disc:{}, " \
                              "train_reg:{}, train_t_reg:{}, train_t_mse:{}, train_layer_one_recon:{}".format(
                    i, train_ci, train_ranking, train_rae, train_gen, train_disc, train_reg, train_t_reg, train_t_mse,
                    train_layer_one_recon)
                print(train_print)
                logging.debug(train_print)

            if is_epoch or (i == (self.num_iterations - 1)):
                improved_str = ''
                # Calculate  Vaid CI the CI
                self.train_ci.append(train_ci)
                self.train_cost.append(train_cost)
                self.train_t_rae.append(train_rae)
                self.train_gen.append(train_gen)
                self.train_disc.append(train_disc)
                self.train_ranking.append(train_ranking)
                self.train_layer_one_recon.append(train_layer_one_recon)

                self.train_writer.add_summary(summary, i)
                valid_ci, valid_cost, valid_rae, valid_ranking, valid_gen, valid_reg, valid_disc, \
                valid_layer_one_recon, valid_t_reg, valid_t_mse = \
                    self.predict_concordance_index(
                        x=self.valid_x,
                        e=self.valid_e,
                        t=self.valid_t)
                self.valid_cost.append(valid_cost)
                self.valid_ci.append(valid_ci)
                self.valid_t_rae.append(valid_rae)
                self.valid_gen.append(valid_gen)
                self.valid_disc.append(valid_disc)
                self.valid_ranking.append(valid_ranking)
                self.valid_layer_one_recon.append(valid_layer_one_recon)
                tf.verify_tensor_all_finite(valid_cost, "Validation Cost has Nan or Infinite")

                if valid_t_reg < best_t_reg:
                    self.saver.save(sess=self.session, save_path=self.save_path)
                    best_validation_epoch = epochs
                    best_t_reg = valid_t_reg
                    last_improvement = i
                    improved_str = '*'
                    # Save  Best Perfoming all variables of the TensorFlow graph to file.
                # update best validation accuracy
                optimization_print = "Iteration: {} epochs:{}, Training: RAE:{}, Loss: {}," \
                                     " Ranking:{}, Reg:{}, Gen:{}, Disc:{}, Recon_One:{}, T_Reg:{},T_MSE:{},  CI:{}" \
                                     " Validation RAE:{} Loss:{}, Ranking:{}, Reg:{}, Gen:{}, Disc:{}, " \
                                     "Recon_One:{}, T_Reg:{}, T_MSE:{}, CI:{}, {}" \
                    .format(i + 1, epochs, train_rae, train_cost, train_ranking, train_reg, train_gen,
                            train_disc, train_layer_one_recon, train_t_reg, train_t_mse,
                            train_ci, valid_rae, valid_cost, valid_ranking, valid_reg, valid_gen, valid_disc,
                            valid_layer_one_recon, valid_t_reg, valid_t_mse, valid_ci, improved_str)

                print(optimization_print)
                logging.debug(optimization_print)
                if i - last_improvement > self.require_improvement or math.isnan(
                        train_cost) or epochs >= self.max_epochs:
                    # if i - last_improvement > self.require_improvement:
                    print("No improvement found in a while, stopping optimization.")
                    # Break out from the for-loop.
                    break
        # Ending time.

        end_time = time.time()
        time_dif = end_time - start_time
        time_dif_print = "Time usage: " + str(timedelta(seconds=int(round(time_dif))))
        print(time_dif_print)
        logging.debug(time_dif_print)
        # shutdown everything to avoid zombies
        self.session.run(self.queue.close(cancel_pending_enqueues=True))
        self.coord.request_stop()
        self.coord.join(self.threads)
        return best_validation_epoch, epochs

    def train_test(self, train=True):

        def get_dict(x, t, e):
            observed_idx = e == 1
            feed_dict = {self.x: x,
                         self.impute_mask: get_missing_mask(x, self.imputation_values),
                         self.t: t,
                         self.t_lab: t[observed_idx],
                         self.e: e,
                         self.batch_size_tensor: len(t),
                         self.is_training: False, self.noise_alpha: np.ones(shape=self.noise_dim)}
            return {'feed_dict': feed_dict, 'outcomes': {}}

        session_dict = {'Test': get_dict(x=self.test_x, t=self.test_t, e=self.test_e),
                        'Train': get_dict(x=self.train_x, t=self.train_t, e=self.train_e),
                        'Valid': get_dict(x=self.valid_x, t=self.valid_t, e=self.valid_e)}

        if train:
            best_epoch, epochs = self.train_neural_network()
            self.time_related_metrics(best_epoch, epochs, session_dict=session_dict)
        else:
            self.generate_statistics(data_x=self.test_x, data_e=self.test_e, data_t=self.test_t, name='Test',
                                     session_dict=session_dict['Test'])

        self.session.close()

    def time_related_metrics(self, best_epoch, epochs, session_dict):
        # plot_cost(training=self.train_cost, validation=self.valid_cost, model=self.model, name="Cost",
        #          epochs=epochs,
        #          best_epoch=best_epoch)
        # plot_cost(training=self.train_ci, validation=self.valid_ci, model=self.model, name="CI",
        #          epochs=epochs,
        #          best_epoch=best_epoch)
        # plot_cost(training=self.train_t_rae, validation=self.valid_t_rae, model=self.model, name="RAE",
        #          epochs=epochs,
        #          best_epoch=best_epoch)
        # plot_cost(training=self.train_ranking, validation=self.valid_ranking, model=self.model, name="Rank",
        #          epochs=epochs,
        #          best_epoch=best_epoch)
        # plot_cost(training=self.train_gen, validation=self.valid_gen, model=self.model, name="Gen_Loss",
        #          epochs=epochs, best_epoch=best_epoch)

        # plot_cost(training=self.train_disc, validation=self.valid_disc, model=self.model, name="Disc_Loss",
        #          epochs=epochs, best_epoch=best_epoch)

        # plot_cost(training=self.train_layer_one_recon, validation=self.valid_layer_one_recon, model=self.model,
        #          name="Recon",
        #         epochs=epochs, best_epoch=best_epoch)
        # TEST
        self.generate_statistics(data_x=self.test_x, data_e=self.test_e, data_t=self.test_t, name='Test',
                                 session_dict=session_dict['Test'], t_train_R=self.train_t, y_train_R=self.train_e)

        # VALID
        self.generate_statistics(data_x=self.valid_x, data_e=self.valid_e, data_t=self.valid_t, name='Valid',
                                 session_dict=session_dict['Valid'], t_train_R=self.train_t, y_train_R=self.train_e)
        # TRAIN
        self.generate_statistics(data_x=self.train_x, data_e=self.train_e, data_t=self.train_t, name='Train',
                                 session_dict=session_dict['Train'], t_train_R=self.train_t, y_train_R=self.train_e)

    def generate_statistics(self, data_x, data_e, data_t, name, session_dict, t_train_R, y_train_R, save=True):
        self.saver.restore(sess=self.session, save_path=self.save_path)
        ci, cost, rae, ranking, gen, reg, disc, layer_one_recon, t_reg, t_mse = \
            self.predict_concordance_index(x=data_x,
                                           e=data_e,
                                           t=data_t,
                                           outcomes=
                                           session_dict[
                                               'outcomes'])

        observed_idx = self.extract_observed_death(name=name, observed_e=data_e, observed_t=data_t, save=save)

        median_predicted_time, median_disc_prob, median_disc_score, median_prob_t_gen = self.median_predict_time(
            session_dict)

        if name == 'Test':
            self.save_time_samples(x=data_x[observed_idx], e=data_e[observed_idx],
                                   t=data_t[observed_idx], name='obs_samples_predicted', cens=False)

            self.save_time_samples(x=data_x[np.logical_not(observed_idx)], e=data_e[np.logical_not(observed_idx)],
                                   t=data_t[np.logical_not(observed_idx)], name='cen_samples_predicted', cens=True)

            np.save('D:\DeepCreditSurv\DeepCreditSurv\models\DATE\\matrix\\{}_predicted_time'.format(name),
                    median_predicted_time)
            np.save('D:\DeepCreditSurv\DeepCreditSurv\models\DATE\\matrix\\{}_disc_prob'.format(name),
                    median_disc_prob)
            np.save('D:\DeepCreditSurv\DeepCreditSurv\models\DATE\\matrix\\{}_disc_score'.format(name),
                    median_disc_score)
            np.save('D:\DeepCreditSurv\DeepCreditSurv\models\DATE\\matrix\\{}_prob_t_gen'.format(name),
                    median_prob_t_gen)
            np.save('D:\DeepCreditSurv\DeepCreditSurv\models\DATE\\matrix\\{}_empirical_time'.format(name),
                    data_t)
            np.save('D:\DeepCreditSurv\DeepCreditSurv\models\DATE\\matrix\\{}_data_e'.format(name), data_e)
            np.save('D:\DeepCreditSurv\DeepCreditSurv\models\DATE\\matrix\\{}_t_train'.format(name), t_train_R)
            np.save('D:\DeepCreditSurv\DeepCreditSurv\models\DATE\\matrix\\{}_y_train'.format(name), y_train_R)

        observed_empirical = data_t[observed_idx]
        observed_predicted = median_predicted_time[observed_idx]

        observed_ci = concordance_index(event_times=observed_empirical, predicted_scores=observed_predicted,
                                        event_observed=data_e[observed_idx])
        observed_ci = 0

        corr = spearmanr(observed_empirical, observed_predicted)
        results = ":{} RAE:{}, Loss:{}, Gen:{}, Disc:{}, Reg:{}, Ranking{}, Recon:{}, T_Reg:{},T_MSE:{}," \
                  " CI:{}, Observed: CI:{}, " \
                  "Correlation:{}".format(name, rae, cost, gen, disc, reg, ranking, layer_one_recon, t_reg, t_mse, ci,
                                          observed_ci, corr)
        logging.debug(results)
        print(results)

    def median_predict_time(self, session_dict):
        # pdb.set_trace()
        predicted_time = []
        disc_prob = []
        disc_score = []
        prob_t_gen = []
        for p in range(self.sample_size):
            gen_time, disc_prob1, disc_score1, prob_t_gen1 = self.session.run(
                [self.predicted_time, self.disc_logit, self.disc_f, self.probability_t_gen],
                feed_dict=session_dict['feed_dict'])
            predicted_time.append(gen_time)
            disc_prob.append(disc_prob1)
            disc_score.append(disc_score1)
            prob_t_gen.append(prob_t_gen1)
        predicted_time = np.array(predicted_time)
        disc_prob = np.array(disc_prob)
        disc_score = np.array(disc_score)
        prob_t_gen = np.array(prob_t_gen)
        # print("predicted_time_shape:{}".format(predicted_time.shape))
        return np.median(predicted_time, axis=0), np.median(disc_prob, axis=0), np.median(disc_score,
                                                                                          axis=0), np.median(prob_t_gen,
                                                                                                             axis=0)

    def save_time_samples(self, x, t, e, name, cens=False):
        predicted_time = self.generate_time_samples(e, x)
        plot_predicted_distribution(predicted=predicted_time, empirical=t, data='Test_' + name, cens=cens)
        return

    def generate_time_samples(self, e, x):
        # observed = e == 1
        feed_dict = {self.x: x,
                     self.impute_mask: get_missing_mask(x, self.imputation_values),
                     # self.t: t,
                     # self.t_lab: t[observed],
                     self.e: e,
                     # self.risk_set: risk_set(t),
                     self.batch_size_tensor: len(x),
                     self.is_training: False, self.noise_alpha: np.ones(shape=self.noise_dim)}
        predicted_time = []
        for p in range(self.sample_size):
            gen_time = self.session.run(self.predicted_time, feed_dict=feed_dict)
            predicted_time.append(gen_time)
        predicted_time = np.array(predicted_time)
        return predicted_time

    def enqueue(self):
        """ Iterates over our data puts small junks into our queue."""
        # TensorFlow Input Pipelines for Large Data Sets
        # ischlag.github.io
        # http://ischlag.github.io/2016/11/07/tensorflow-input-pipeline-for-large-datasets/
        # http://web.stanford.edu/class/cs20si/lectures/slides_09.pdf
        under = 0
        max = len(self.train_x)
        try:
            while not self.coord.should_stop():
                # print("starting to write into queue")
                upper = under + self.capacity
                # print("try to enqueue ", under, " to ", upper)
                if upper <= max:
                    curr_x = self.train_x[under:upper]
                    curr_t = self.train_t[under:upper]
                    curr_e = self.train_e[under:upper]
                    under = upper
                else:
                    rest = upper - max
                    curr_x = np.concatenate((self.train_x[under:max], self.train_x[0:rest]))
                    curr_t = np.concatenate((self.train_t[under:max], self.train_t[0:rest]))
                    curr_e = np.concatenate((self.train_e[under:max], self.train_e[0:rest]))
                    under = rest

                self.session.run(self.enqueue_op,
                                 feed_dict={self.x: curr_x, self.t: curr_t, self.e: curr_e})
        except tf.errors.CancelledError:
            print("finished enqueueing")

    @staticmethod
    def extract_observed_death(name, observed_e, observed_t, save=False):
        idx_observed = observed_e == 1
        observed_death = observed_t[idx_observed]
        if save:
            death_observed_print = "{} observed_death:{}, percentage:{}".format(name, observed_death.shape, float(
                len(observed_death) / len(observed_t)))
            logging.debug(death_observed_print)
            print(death_observed_print)
        return idx_observed


class DATE(DATE_AE):
    def __init__(self,
                 batch_size,
                 learning_rate,
                 beta1,
                 beta2,
                 require_improvement,
                 seed,
                 num_iterations,
                 hidden_dim,
                 latent_dim,
                 input_dim,
                 num_examples,
                 keep_prob,
                 train_data,
                 valid_data,
                 test_data,
                 end_t,
                 covariates,
                 imputation_values,
                 sample_size,
                 disc_updates,
                 categorical_indices,
                 l2_reg,
                 gen_updates,
                 max_epochs,
                 path_large_data=""
                 ):
        DATE_AE.__init__(self, batch_size=batch_size,
                         learning_rate=learning_rate,
                         beta1=beta1,
                         beta2=beta2,
                         require_improvement=require_improvement,
                         num_iterations=num_iterations, seed=seed,
                         l2_reg=l2_reg,
                         hidden_dim=hidden_dim,
                         train_data=train_data, test_data=test_data, valid_data=valid_data,
                         input_dim=input_dim,
                         num_examples=num_examples, keep_prob=keep_prob,
                         latent_dim=latent_dim, end_t=end_t,
                         path_large_data=path_large_data,
                         covariates=covariates,
                         categorical_indices=categorical_indices,
                         disc_updates=disc_updates,
                         sample_size=sample_size, imputation_values=imputation_values,
                         max_epochs=max_epochs, gen_updates=gen_updates)

        print_model = "model is DATE"
        print(print_model)
        logging.debug(print_model)
        self.model = 'DATE'
        self.imputation_values = imputation_values

    def _objective(self):
        self.num_batches = self.num_examples / self.batch_size
        logging.debug("num batches:{}, batch_size:{} epochs:{}".format(self.num_batches, self.batch_size,
                                                                       int(self.num_iterations / self.num_batches)))
        self._build_model()
        self.reg_loss = l2_loss(self.l2_reg) + l1_loss(self.l2_reg)
        self.layer_one_recon = tf.constant(0.0)
        self.cost = self.t_regularization_loss + self.disc_one_loss + self.gen_one_loss
        optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate, beta1=self.beta1,
                                           beta2=self.beta2)

        dvars1 = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "Discriminator_one")
        genvars1 = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, "generate_t_given_x")
        self.disc_solver = optimizer.minimize(self.disc_one_loss, var_list=dvars1)
        self.gen_solver = optimizer.minimize(self.gen_one_loss + self.t_regularization_loss, var_list=genvars1)

    def _build_model(self):
        self._risk_date()

    @staticmethod
    def log(x):
        return tf.log(x + 1e-8)

    def _risk_date(self):
        def expand_t_dim(t):
            return tf.expand_dims(t, axis=1)

        indices_lab = tf.where(tf.equal(tf.constant(1.0, dtype=tf.float32), self.e))
        x_lab = tf.squeeze(tf.gather(self.x, indices_lab), axis=[1])
        t_lab_exp = expand_t_dim(self.t_lab)

        t_gen, prob_t_gen = pt_given_x(x=self.x, hidden_dim=self.hidden_dim, is_training=self.is_training,
                                       batch_norm=self.batch_norm, keep_prob=self.keep_prob,
                                       batch_size=self.batch_size_tensor,
                                       input_dim=self.input_dim, noise_alpha=self.noise_alpha)

        # Discriminator B
        d_one_real, f_one_real = discriminator_one(pair_one=x_lab, pair_two=t_lab_exp, hidden_dim=self.hidden_dim,
                                                   is_training=self.is_training, batch_norm=self.batch_norm,
                                                   keep_prob=self.keep_prob)  # (x_nc, t_nc)
        d_one_fake, f_one_fake = discriminator_one(pair_one=self.x, pair_two=t_gen, hidden_dim=self.hidden_dim,
                                                   is_training=self.is_training, batch_norm=self.batch_norm,
                                                   reuse=True, keep_prob=self.keep_prob)  # (x, t_gen)

        # Discriminator loss
        self.disc_one_loss = -tf.reduce_mean(self.log(d_one_real)) - tf.reduce_mean(self.log(1 - d_one_fake))

        # Generator loss
        self.gen_one_loss = tf.reduce_mean(f_one_real) - tf.reduce_mean(f_one_fake)
        self.disc_logit = d_one_fake
        self.disc_f = f_one_fake
        self.probability_t_gen = tf.squeeze(prob_t_gen)  #
        self.predicted_time = tf.squeeze(t_gen)
        self.ranking_partial_lik, self.total_rae, self.total_t_recon_loss = \
            batch_metrics(e=self.e,
                          risk_set=self.risk_set,
                          predicted=self.predicted_time,
                          batch_size=self.batch_size_tensor,
                          empirical=self.t)

        self.t_regularization_loss = self.total_t_recon_loss
        self.t_mse = tf.losses.mean_squared_error(labels=self.t_lab,
                                                  predictions=tf.gather(self.predicted_time, indices_lab))


def pt_given_z(z, hidden_dim, is_training, batch_norm, batch_size, latent_dim, noise_alpha, keep_prob=0.9, reuse=False):
    size = len(hidden_dim)
    with tf.variable_scope('generate_t_given_z', reuse=reuse):
        # Variables
        noise = uniform(dim=latent_dim, batch_size=batch_size) * tf.gather(noise_alpha, 0)
        z_plus_noise = tf.concat([z, noise], axis=1)

        hidden_z = hidden_mlp_layers_noise(batch_norm=batch_norm, hidden_dim=hidden_dim,
                                           is_training=is_training, keep_prob=keep_prob,
                                           layer_input=z_plus_noise, size=size, batch_size=batch_size,
                                           noise_alpha=noise_alpha)

        w_t, b_t = create_nn_weights('t', 'encoder', [hidden_z.get_shape().as_list()[1], 1])
        t_mu = mlp_neuron(hidden_z, w_t, b_t, activation=False)
        return tf.exp(t_mu)


def pt_given_x(x, hidden_dim, is_training, batch_norm, batch_size, input_dim, noise_alpha, keep_prob=0.9, reuse=False):
    size = len(hidden_dim)
    with tf.variable_scope('generate_t_given_x', reuse=reuse):
        noise = uniform(dim=input_dim, batch_size=batch_size) * tf.gather(noise_alpha, 0)
        x_plus_noise = tf.concat([x, noise],
                                 axis=1)  # the layer input shape would be 17 + 17 = 34 (17 is the number of features)
        hidden_x = hidden_mlp_layers_noise(batch_norm=batch_norm, hidden_dim=hidden_dim,
                                           is_training=is_training, keep_prob=keep_prob,
                                           layer_input=x_plus_noise, size=size, batch_size=batch_size,
                                           noise_alpha=noise_alpha)  # hidden_x shape (?,100)

        w_t, b_t = create_nn_weights('t', 'encoder', [hidden_x.get_shape().as_list()[1], 1])
        # name:W_encoder_t, shape[100, 1]
        # name:b_encoder_t, shape[1]
        t_mu = mlp_neuron(hidden_x, w_t, b_t, activation=False)  # mlp = tf.add(tf.matmul(layer_input, weights), biases)
        # no activation is applied
        logit = tf.nn.sigmoid(t_mu)
        return tf.exp(t_mu), logit


def discriminator(pair_one, pair_two, hidden_dim, is_training, batch_norm, scope, keep_prob=1, reuse=False):
    size = len(hidden_dim)
    with tf.variable_scope(scope, reuse=reuse):
        # Variables
        print("scope:{}, pair_one:{}, pair_two:{}".format(scope, pair_one.shape, pair_two.shape))
        hidden_pair_one = hidden_mlp_layers(batch_norm=batch_norm, hidden_dim=hidden_dim,
                                            is_training=is_training, keep_prob=keep_prob,
                                            layer_input=pair_one, size=size)

        hidden_pair_two = hidden_mlp_layers(batch_norm=batch_norm, hidden_dim=hidden_dim,
                                            is_training=is_training, keep_prob=keep_prob,
                                            layer_input=pair_two, size=size)
        hidden_pairs = tf.concat([hidden_pair_one, hidden_pair_two], axis=1)
        print("hidden_pairs:{}".format(hidden_pairs.get_shape()))
        w_logit, b_logit = create_nn_weights('logits', 'discriminator', [hidden_dim[size - 1] * 2, 1])
        f = mlp_neuron(layer_input=hidden_pairs, weights=w_logit, biases=b_logit, activation=False)
        logit = tf.nn.sigmoid(f)

        return tf.squeeze(logit), tf.squeeze(f)


def discriminator_one(pair_one, pair_two, hidden_dim, is_training, batch_norm, keep_prob=1, reuse=False):
    score, f = discriminator(pair_one=pair_one, pair_two=pair_two, scope='Discriminator_one', batch_norm=batch_norm,
                             is_training=is_training,
                             keep_prob=keep_prob, reuse=reuse, hidden_dim=hidden_dim)
    return score, f


def discriminator_two(pair_one, pair_two, hidden_dim, is_training, batch_norm, keep_prob=1, reuse=False):
    score, f = discriminator(pair_one=pair_one, pair_two=pair_two, scope='Discriminator_two', batch_norm=batch_norm,
                             is_training=is_training,
                             keep_prob=keep_prob, reuse=reuse, hidden_dim=hidden_dim)
    return score, f


def generate_x_given_z(z, hidden_dim, latent_dim, is_training, batch_norm, batch_size, input_dim, keep_prob=1,
                       reuse=False):
    # Generative p(x|z)
    size = len(hidden_dim)
    with tf.variable_scope("decoder", reuse=reuse):
        noise = uniform(dim=latent_dim, batch_size=batch_size)
        z_plus_noise = tf.concat([z, noise], axis=1)
        layer_input = hidden_mlp_layers(batch_norm=batch_norm, hidden_dim=hidden_dim, is_training=is_training,
                                        keep_prob=keep_prob, layer_input=z_plus_noise, size=size)

        w_x, b_x = create_nn_weights('x', 'decoder', [hidden_dim[size - 1], input_dim])
        # Model
        # Reconstruction layer
        x = mlp_neuron(layer_input, w_x, b_x, activation=False)
        return x


def generate_z_given_x(x, hidden_dim, latent_dim, is_training, batch_norm, input_dim, batch_size,
                       keep_prob=1,
                       reuse=False, sample_size=200):
    size = len(hidden_dim)
    z = tf.zeros(shape=(batch_size, latent_dim))

    for _ in np.arange(sample_size):
        print("hidden_dim:{}, size:{}".format(hidden_dim, size))
        with tf.variable_scope("encoder_given_x", reuse=reuse):
            # Variables
            sample = sample_z(batch_norm, batch_size, hidden_dim, input_dim, is_training, keep_prob, latent_dim, size,
                              x)
            z = tf.add(z, sample)

    return tf.scalar_mul(scalar=1 / sample_size, x=z)


def sample_z(batch_norm, batch_size, hidden_dim, input_dim, is_training, keep_prob, latent_dim, size, x):
    noise = uniform(dim=input_dim, batch_size=batch_size)
    x_plus_noise = tf.concat([x, noise], axis=1)
    layer_input = hidden_mlp_layers(batch_norm=batch_norm, hidden_dim=hidden_dim,
                                    is_training=is_training, keep_prob=keep_prob,
                                    layer_input=x_plus_noise, size=size)
    w_z, b_z = create_nn_weights('z', 'encoder', [hidden_dim[size - 1], latent_dim])
    z = mlp_neuron(layer_input, w_z, b_z, activation=False)
    return z


def x_reconstruction(x_recon, x, categorical_indices, continuous_indices, batch_size):
    print("x_reconstruction categorical_indices:{}".format(categorical_indices))

    def condition(i, recon):
        return i < batch_size

    def body(i, recon):
        # get edges for observation i
        x_i = tf.gather(x, i)
        x_recon_i = tf.gather(x_recon, i)
        # print("ae x_i:{}".format(x_i.shape))
        x_masked = tf.where(tf.is_nan(x_i), tf.zeros_like(x_i), x_i)
        x_recon_masked = tf.where(tf.is_nan(x_i), tf.zeros_like(x_i), x_recon_i)
        if len(categorical_indices) == 0:
            total_recon = tf.losses.mean_squared_error(x_masked, x_recon_masked)
        else:
            continous_x = tf.gather(x_masked, continuous_indices)
            continous_x_recon = tf.gather(x_recon_masked, continuous_indices)
            # print("ae continous x_i:{}".format(continous_x.shape))
            l2_recon = tf.losses.mean_squared_error(continous_x, continous_x_recon)
            cross_entropy = tf.constant(0.0)
            for category in categorical_indices:
                labels = tf.gather(x_masked, category)
                logits = tf.gather(x_recon_masked, category)
                # print("ae categorial x_i:{}".format(labels.shape))
                cross_entropy += tf.reduce_mean(
                    tf.nn.softmax_cross_entropy_with_logits_v2(logits=logits, labels=labels))
            total_recon = tf.add(l2_recon, cross_entropy)

        cumulative_recon = tf.add(total_recon, recon)
        return [i + 1, tf.reshape(cumulative_recon, shape=())]

    # Relevant Functions
    idx = tf.constant(0, shape=())
    initial_recon = tf.constant(0.0, shape=())
    _, total_recon, = tf.while_loop(condition, body, loop_vars=[idx, initial_recon], shape_invariants=[idx.get_shape(),
                                                                                                       initial_recon.get_shape(),
                                                                                                       ])
    return tf.div(total_recon, tf.cast(batch_size, dtype=tf.float32))
