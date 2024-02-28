import numpy as np
import tensorflow as tf


class StiffRegularizer(tf.keras.regularizers.Regularizer):
    def __init__(self, strength, initial_value):
        super().__init__()
        self._strength = strength
        self._initial_value = tf.Variable(initial_value, trainable=False)

    def __call__(self, x):
        return self._strength * tf.reduce_sum(tf.square(x - self._initial_value))

        
def sample_firing_rates(firing_rates, n_neurons, rnd_seed):
    sorted_firing_rates = np.sort(firing_rates)
    percentiles = (np.arange(firing_rates.shape[-1]) + 1).astype(np.float32) / firing_rates.shape[-1]
    rate_rd = np.random.RandomState(seed=rnd_seed)
    x_rand = rate_rd.uniform(size=n_neurons)
    target_firing_rates = np.sort(np.interp(x_rand, percentiles, sorted_firing_rates))
    
    return target_firing_rates


def huber_quantile_loss(u, tau, kappa, dtype=tf.float32):
    tau = tf.cast(tau, dtype)
    num = tf.abs(tau - tf.cast(u <= 0, dtype))

    branch_1 = num / (2 * kappa) * tf.square(u)
    branch_2 = num * (tf.abs(u) - 0.5 * kappa)
    return tf.where(tf.abs(u) <= kappa, branch_1, branch_2)

### To calculate the loss of firing rates between neuron types
def compute_spike_rate_target_loss(_spikes, target_rates, core_mask=None, dtype=tf.float32):
    # TODO: define this function
    # target_rates is a dictionary that contains all the cell types.
    # I should iterate on them, and add the cost for each one at the end.
    # spikes will have a shape of (batch_size, n_steps, n_neurons)
    losses = []
    rates = tf.reduce_mean(_spikes, (0, 1))
    if core_mask is not None:
        core_neurons_ids = np.where(core_mask)[0]

    for i, (key, value) in enumerate(target_rates.items()):
        if core_mask is not None:
            key_core_mask = np.isin(value["neuron_ids"], core_neurons_ids)
            neuron_ids =  np.where(key_core_mask)[0]
            _rate_type = tf.gather(rates, neuron_ids)
            target_rate = value["sorted_target_rates"][key_core_mask]
        else:
            _rate_type = tf.gather(rates, value["neuron_ids"])
            target_rate = value["sorted_target_rates"]

        loss_type = compute_spike_rate_distribution_loss(_rate_type, target_rate, dtype=dtype)
        losses.append(tf.reduce_mean(loss_type))

    return tf.reduce_sum(losses, axis=0)

def compute_spike_rate_distribution_loss(_rates, target_rate, dtype=tf.float32):
    ind = tf.range(target_rate.shape[0])
    rand_ind = tf.random.shuffle(ind)
    _rate = tf.gather(_rates, rand_ind)
    sorted_rate = tf.sort(_rate)
    u = target_rate - sorted_rate
    # tau = (tf.range(target_rate.shape[0]), dtype) + 1) / target_rate.shape[0]
    tau = (tf.range(target_rate.shape[0]) + 1) / target_rate.shape[0]
    loss = huber_quantile_loss(u, tau, 0.002, dtype=dtype)

    return loss


class SpikeRateDistributionTarget:
    """ Instead of regularization, treat it as a target.
        The main difference is that this class will calculate the loss
        for each subtypes of the neurons."""
    def __init__(self, target_rates, rate_cost=.5, pre_delay=None, post_delay=None, core_mask=None, dtype=tf.float32):
        self._rate_cost = rate_cost
        self._target_rates = target_rates
        self._pre_delay = pre_delay
        self._post_delay = post_delay
        self._core_mask = core_mask
        self._dtype = dtype

    def __call__(self, spikes):
        if self._pre_delay is not None:
            spikes = spikes[:, self._pre_delay:, :]
        if self._post_delay is not None and self._post_delay != 0:
            spikes = spikes[:, :-self._post_delay, :]
        reg_loss = compute_spike_rate_target_loss(spikes, self._target_rates, core_mask=self._core_mask, 
                                                dtype=self._dtype) * self._rate_cost
        return reg_loss


# class SpikeRateDistributionRegularization:
#     def __init__(self, target_rates, rate_cost=0.5, dtype=tf.float32):
#         self._rate_cost = rate_cost
#         self._target_rates = target_rates
#         self._dtype = dtype

#     def __call__(self, spikes):
#         reg_loss = (
#             compute_spike_rate_distribution_loss(spikes, self._target_rates, dtype=self._dtype)
#             * self._rate_cost
#         )
#         reg_loss = tf.reduce_sum(reg_loss)

#         return reg_loss


class VoltageRegularization:
    def __init__(self, cell, voltage_cost=1e-5, dtype=tf.float32, core_mask=None):
        self._voltage_cost = voltage_cost
        self._cell = cell
        self._dtype = dtype
        self._core_mask = core_mask

    def __call__(self, voltages):
        if self._core_mask is not None:
            voltages = tf.boolean_mask(voltages, self._core_mask, axis=2)
            
        voltage_32 = (voltages - self._cell.voltage_offset) / self._cell.voltage_scale
        voltage_32 = tf.cast(voltage_32, self._dtype)
        v_pos = tf.square(tf.nn.relu(voltage_32 - 1.0))
        v_neg = tf.square(tf.nn.relu(-voltage_32 + 1.0))
        voltage_loss = tf.reduce_mean(tf.reduce_sum(v_pos + v_neg, -1)) * self._voltage_cost
        return voltage_loss


class OrientationSelectivityLoss:
    def __init__(self, tuning_angles, osi_cost=1e-5, pre_delay=None, post_delay=None, dtype=tf.float32, core_mask=None):
        self._tuning_angles = tuning_angles
        self._osi_cost = osi_cost
        self._pre_delay = pre_delay
        self._post_delay = post_delay
        self._dtype = dtype
        self._core_mask = core_mask
        if self._core_mask is not None:
            self._tuning_angles = tf.boolean_mask(self._tuning_angles, self._core_mask)

    def calculate_delta_angle(self, stim_angle, tuning_angle):
        # angle unit is degrees.
        # this function calculates the difference between stim_angle and tuning_angle,
        # but it is fine to have the opposite direction.
        # so, delta angle is always between -90 and 90.
        # they are both vector, so dimension matche is needed.
        # stim_angle is a length of batch size
        # tuning_angle is a length of n_neurons

        # delta_angle = stim_angle - tuning_angle
        delta_angle = tf.expand_dims(stim_angle, axis=1) - tuning_angle
        delta_angle = tf.where(delta_angle > 90, delta_angle - 180, delta_angle)
        delta_angle = tf.where(delta_angle < -90, delta_angle + 180, delta_angle)
        # # do it twice to make sure
        delta_angle = tf.where(delta_angle > 90, delta_angle - 180, delta_angle)
        delta_angle = tf.where(delta_angle < -90, delta_angle + 180, delta_angle)

        return delta_angle

    def __call__(self, spikes, angle):
        # I need to access the tuning angle. of all the neurons.
        angle = tf.cast(angle, self._dtype)

        if self._core_mask is not None:
            spikes = tf.boolean_mask(spikes, self._core_mask, axis=2)
            
        if self._pre_delay is not None:
            spikes = spikes[:, self._pre_delay:, :]
        if self._post_delay is not None and self._post_delay != 0:
            spikes = spikes[:, :-self._post_delay, :]

        delta_angle = self.calculate_delta_angle(angle, self._tuning_angles)
        # sum spikes in _z, and multiply with delta_angle.
        sum_angle = tf.reduce_mean(spikes, axis=[1]) * delta_angle
        # make a huber loss for this.
        # angle_loss = tf.keras.losses.Huber(delta=1, reduction=tf.keras.losses.Reduction.SUM)(sum_angle, tf.zeros_like(sum_angle))
        angle_loss = tf.reduce_mean(tf.abs(sum_angle))
        # it might be nice to add regularization of weights
        # rec_weight_loss = rec_weight_regularizer(rsnn_layer.cell.recurrent_weight_values)
        return angle_loss * self._osi_cost