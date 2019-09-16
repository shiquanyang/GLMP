import tensorflow as tf
from tensorflow.python.ops import array_ops
from tensorflow.python.keras import backend as K
from tensorflow.python.util import nest


class GraphGRUCell(tf.keras.Model):
    '''
    Cell class for GraphGRU layer.
    '''
    def __init__(self,
                 units,
                 input_dim,
                 recurrent_size=4,
                 activation='tanh',
                 recurrent_activation='sigmoid',
                 use_bias=True,
                 kernel_initializer='glorot_uniform',
                 recurrent_initializer='orthogonal',
                 bias_initializer='zeros',
                 kernel_regularizer=None,
                 recurrent_regularizer=None,
                 bias_regularizer=None,
                 kernel_constraint=None,
                 recurrent_constraint=None,
                 bias_constraint=None,
                 dropout=0.,
                 recurrent_dropout=0.,
                 **kwargs):
        super(GraphGRUCell, self).__init__(**kwargs)
        self.units = units
        self.input_dim = input_dim
        self.recurrent_size = recurrent_size

        self.activation = tf.keras.layers.Activation(activation)
        self.recurrent_activation = tf.keras.layers.Activation(recurrent_activation)
        self.use_bias = use_bias

        self.kernel_initializer = kernel_initializer
        self.recurrent_initializer = recurrent_initializer
        self.bias_initializer = bias_initializer

        self.kernel_regularizer = kernel_regularizer
        self.recurrent_regularizer = recurrent_regularizer
        self.bias_regularizer = bias_regularizer

        self.kernel_constraint = kernel_constraint
        self.recurrent_constraint = recurrent_constraint
        self.bias_constraint = bias_constraint

        self.dropout = min(1., max(0., dropout))
        self.recurrent_dropout = min(1., max(0., recurrent_dropout))

        self.kernel = self.add_weight(  # self.kernel: input_dim*(3*embedding_dim)
            name='kernel',
            shape=(input_dim, 3 * units),
            initializer=kernel_initializer,
            regularizer=kernel_regularizer,
            constraint=kernel_constraint
        )

        self.recurrent_kernel = self.add_weight(  # self.recurrent_kernel: recurrent_size*embedding_dim*(3*embedding_dim)
            name='recurrent_kernel',
            shape=(recurrent_size, units, 3 * units),
            initializer=recurrent_initializer,
            regularizer=recurrent_regularizer,
            constraint=recurrent_constraint
        )

        if use_bias:
            self.bias = self.add_weight(  # self.bias: (recurrent_size+1)*(3*embedding_dim)
                name='bias',
                shape=((recurrent_size + 1), 3 * units),
                initializer=bias_initializer,
                regularizer=bias_regularizer,
                constraint=bias_constraint
            )
        else:
            self.bias = None

    def call(self, inputs, states, cell_mask, training=True):  # inputs: batch_size*embedding_dim, states:4*batch_size*embedding_dim, cell_mask: batch_size*recurrent_size
        batch_size = inputs.shape[0]
        state_size = len(states)
        if state_size > self.recurrent_size:
            raise ValueError("length of states exceeds recurrent_size.")
        if self.use_bias:
            unstacked_biases = array_ops.unstack(self.bias)  # unstacked_biases: (recurrent_size+1)*embedding_dim
            input_bias, recurrent_bias = unstacked_biases[0], unstacked_biases[1:]  # input_bias: (3*embedding_dim), recurrent_bias: recurrent_size*(3*embedding_dim)

        matrix_x = K.dot(inputs, self.kernel)  # matrix_x: batch_size*(3*embedding_dim)
        if self.use_bias:
            # biases: bias_z_i, bias_r_i, bias_h_i
            matrix_x = K.bias_add(matrix_x, input_bias)

        x_z = matrix_x[:, :self.units]  # x_z: batch_size*embedding_dim
        x_r = matrix_x[:, self.units: 2 * self.units]  # x_r: batch_size*embedding_dim
        x_h = matrix_x[:, 2 * self.units:]  # x_h: batch_size*embedding_dim

        def _expand_mask(mask_t, input_t, fixed_dim=1):  # mask_t: batch_size*1, input_t: batch_size*embedding_dim
            assert not nest.is_sequence(mask_t)
            assert not nest.is_sequence(input_t)
            rank_diff = len(input_t.shape) - len(mask_t.shape)  # rand_diff: 0
            for _ in range(rank_diff):
                mask_t = array_ops.expand_dims(mask_t, -1)
            multiples = [1] * fixed_dim + input_t.shape.as_list()[fixed_dim:]  # multiples: [1, embedding_dim]
            return array_ops.tile(mask_t, multiples)

        accumulate_h = array_ops.zeros([batch_size, self.units])  # accumulate_h: batch_size*embedding_dim
        accumulate_z_h = array_ops.zeros([batch_size, self.units])  # accumulate_z_h: batch_size*embedding_dim
        accumulate_z = array_ops.zeros([batch_size, self.units])  # accumulate_z: batch_size*embedding_dim
        for k in range(self.recurrent_size):
            matrix_inner = K.dot(states[k], self.recurrent_kernel[k])  # matrix_inner: batch_size*(3*embedding_dim), states[k]: batch_size*embedding_dim
            if self.use_bias:
                matrix_inner = K.bias_add(matrix_inner, recurrent_bias[k])
            recurrent_z = matrix_inner[:, :self.units]  # recurrent_z: batch_size*embedding_dim
            recurrent_r = matrix_inner[:, self.units: 2 * self.units]  # recurrent_r: batch_size*embedding_dim

            z = self.recurrent_activation(x_z + recurrent_z)  # z: batch_size*embedding_dim
            r = self.recurrent_activation(x_r + recurrent_r)  # r: batch_size*embedding_dim

            # mask
            tiled_mask_t = _expand_mask(cell_mask[:, k], z)  # tiled_mask_t: batch_size*embedding_dim

            recurrent_h = r * matrix_inner[:, 2 * self.units:]  # recurrent_h: batch_size*embedding_dim
            recurrent_h = array_ops.where(tiled_mask_t, recurrent_h, array_ops.zeros_like(recurrent_h))
            accumulate_h = accumulate_h + recurrent_h  # accumulate_h: batch_size*embedding_dim

            z_h = z * states[k]
            z_h = array_ops.where(tiled_mask_t, z_h, array_ops.zeros_like(z_h))
            accumulate_z_h = accumulate_z_h + z_h  # accumulate_z_h: batch_size*embedding_dim

            z = array_ops.where(tiled_mask_t, z, array_ops.zeros_like(z))
            accumulate_z = accumulate_z + z  # accumulate_z: batch_size*embedding_dim

        hh = self.activation(x_h + accumulate_h / self.recurrent_size)  # hh: batch_size*embedding_dim
        h = (1 - accumulate_z / self.recurrent_size) * hh + accumulate_z_h / self.recurrent_size  # h: batch_size*embedding_dim
        return h, [h]