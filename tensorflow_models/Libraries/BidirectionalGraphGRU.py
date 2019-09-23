import tensorflow as tf
from tensorflow.python.keras import backend as K
from tensorflow_models.Libraries.RNN import RNN
import pdb


class BidirectionalGraphGRU(tf.keras.Model):
    '''
    Bidirectional GraphGRU layer.
    '''
    def __init__(self,
                 units,
                 input_dim,
                 edge_types,
                 recurrent_size=4,
                 merge_mode='concat',
                 **kwargs):
        super(BidirectionalGraphGRU, self).__init__(**kwargs)
        self.units = units
        self.input_dim = input_dim
        self.edge_types = edge_types
        self.recurrent_size = recurrent_size
        self.merge_mode = merge_mode
        self.edge_embeddings = tf.keras.layers.Embedding(edge_types,
                                                         units,
                                                         embeddings_initializer=tf.initializers.RandomNormal(0.0, 1.0))
        self.forward_layer = RNN(units,
                                 input_dim,
                                 edge_types,
                                 self.edge_embeddings,
                                 recurrent_size,
                                 return_sequences=True,
                                 return_state=True)
        self.backward_layer = RNN(units,
                                  input_dim,
                                  edge_types,
                                  self.edge_embeddings,
                                  recurrent_size,
                                  return_sequences=True,
                                  return_state=True,
                                  go_backwards=True)
        self.return_sequences = self.forward_layer.return_sequences
        self.return_state = self.forward_layer.return_state

    def call(self,
             inputs,  # inputs: batch_size*max_len*embedding_dim
             dependencies,  # dependencies: 2*batch_size*max_len*recurrent_size
             edge_types,  # edge_types: 2*batch_size*max_len*recurrent_size
             mask=None,  # mask: batch_size*max_len
             cell_mask=None,  # mask: 2*batch_size*max_len*recurrent_size
             initial_state=None,  # initial_state: 2*4*batch_size*embedding_dim
             training=True):
        if initial_state is not None:
            forward_inputs, backward_inputs = inputs, inputs
            forward_state, backward_state = initial_state[0], initial_state[1]
            forward_dependencies, backward_dependencies = dependencies[0], dependencies[1]
            forward_edge_types, backward_edge_types = edge_types[0], edge_types[1]
            forward_cell_mask, backward_cell_mask = cell_mask[0], cell_mask[1]
        else:
            raise ValueError("Please provide initial states for RNN.")

        y = self.forward_layer(forward_inputs, forward_dependencies, forward_edge_types, mask, forward_cell_mask, forward_state, training)
        y_rev = self.backward_layer(backward_inputs, backward_dependencies, backward_edge_types, mask, backward_cell_mask, backward_state, training)

        if self.return_state:
            states = y[1:] + y_rev[1:]
            y = y[0]
            y_rev = y_rev[0]

        if self.return_sequences:
            y_rev = K.reverse(y_rev, 1)
        if self.merge_mode == 'concat':
            output = K.concatenate([y, y_rev])
        elif self.merge_mode == 'sum':
            output = y + y_rev
        elif self.merge_mode == 'ave':
            output = (y + y_rev) / 2
        elif self.merge_mode == 'mul':
            output = y * y_rev
        elif self.merge_mode is None:
            output = [y, y_rev]
        else:
            raise ValueError('Unrecognized value for `merge_mode`: %s' % (self.merge_mode))

        if self.return_state:
            if self.merge_mode is None:
                return output + states
            return [output] + states
        return output