# coding: utf8
from __future__ import print_function
from __future__ import absolute_import

from keras.models import Sequential
from keras.layers import *


class ContextLayer(object):
    """Word context layer
    """
    def __init__(self, rnn_dim, rnn_unit='gru', input_shape=(0,),
                 dropout=0.0, highway=False, return_sequences=False,
                 dense_dim=0):
        if rnn_unit == 'gru':
            rnn = GRU
        else:
            rnn = LSTM
        self.model = Sequential()
        self.model.add(
            Bidirectional(rnn(rnn_dim,
                              dropout=dropout,
                              recurrent_dropout=dropout,
                              return_sequences=return_sequences),
                          input_shape=input_shape))
        if highway:
            if return_sequences:
                self.model.add(TimeDistributed(Highway(activation='tanh')))
            else:
                self.model.add(Highway(activation='tanh'))

        if dense_dim > 0:
            self.model.add(TimeDistributed(Dense(dense_dim,
                                                 activation='relu')))
            self.model.add(TimeDistributed(Dropout(dropout)))
            self.model.add(TimeDistributed(BatchNormalization()))
        self.model.summary()

    def __call__(self, inputs):
        return self.model(inputs)


class MultiPerspective(Layer):
    """Multi-perspective Matching Layer.
    # Arguments
        mp_dim: single forward/backward multi-perspective dimention
    """

    def __init__(self, mp_dim, with_full_match=True, with_maxpool_match=False, with_attention_match=False,
                 with_max_attention_match=False, **kwargs):
        self.mp_dim = mp_dim
        self.epsilon = 1e-6
        self.strategy = 4
        self.with_full_match = with_full_match
        self.with_maxpool_match = with_maxpool_match
        self.with_attention_match = with_attention_match
        self.with_max_attention_match = with_max_attention_match
        super(MultiPerspective, self).__init__(**kwargs)

    def build(self, input_shape):
        if isinstance(input_shape, list):
            input_shape = input_shape[0]
        embedding_size = input_shape[-1] / 2
        # Create a trainable weight variable for this layer.
        # input_shape is bidirectional RNN input shape
        # kernel shape (mp_dim * 2 * self.strategy, embedding_size)
        self.kernel = self.add_weight('kernel', (self.mp_dim,
                                       embedding_size * 2 * self.strategy),
                                       initializer='glorot_uniform',
                                       trainable=True)
        self.kernel_full_fw = self.kernel[:, :embedding_size]
        self.kernel_full_bw = self.kernel[:, embedding_size: embedding_size * 2]
        self.kernel_attentive_fw = self.kernel[:, embedding_size * 2: embedding_size * 3]
        self.kernel_attentive_bw = self.kernel[:, embedding_size * 3: embedding_size * 4]
        self.kernel_max_attentive_fw = self.kernel[:, embedding_size * 4: embedding_size * 5]
        self.kernel_max_attentive_bw = self.kernel[:, embedding_size * 5: embedding_size * 6]
        self.kernel_max_pool_fw = self.kernel[:, embedding_size * 6: embedding_size * 7]
        self.kernel_max_pool_bw = self.kernel[:, embedding_size * 7:]
        self.built = True
        super(MultiPerspective, self).build(input_shape)

    def compute_output_shape(self, input_shape):
        if isinstance(input_shape, list):
            input_shape = input_shape[0]
        return input_shape[0], input_shape[1], self.mp_dim * 2 * self.strategy

    def get_config(self):
        config = {'mp_dim': self.mp_dim,
                  }
        base_config = super(MultiPerspective, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def call(self, inputs, **kwargs):
        # h1, h2: bidirectional LSTM hidden states, include forward and backward states
        #         (batch_size, timesteps, embedding_size * 2)
        h1 = inputs[0]
        h2 = inputs[1]
        embedding_size = K.int_shape(h1)[-1] / 2
        h1_fw = h1[:, :, :embedding_size]
        h1_bw = h1[:, :, embedding_size:]
        h2_fw = h2[:, :, :embedding_size]
        h2_bw = h2[:, :, embedding_size:]

        # 4 matching strategy
        list_matching = []

        # full matching ops
        matching_fw = self._full_matching(h1_fw, h2_fw, self.kernel_full_fw)
        matching_bw = self._full_matching(h1_bw, h2_bw, self.kernel_full_bw)
        list_matching.extend([matching_fw, matching_bw])

        # cosine matrix
        cosine_matrix_fw = self._cosine_matrix(h1_fw, h2_fw)
        cosine_matrix_bw = self._cosine_matrix(h1_bw, h2_bw)

        # attentive matching ops
        matching_fw = self._attentive_matching(
            h1_fw, h2_fw, cosine_matrix_fw, self.kernel_attentive_fw)
        matching_bw = self._attentive_matching(
            h1_bw, h2_bw, cosine_matrix_bw, self.kernel_attentive_bw)
        list_matching.extend([matching_fw, matching_bw])

        # max attentive matching ops
        matching_fw = self._max_attentive_matching(
            h1_fw, h2_fw, cosine_matrix_fw, self.kernel_max_attentive_fw)
        matching_bw = self._max_attentive_matching(
            h1_bw, h2_bw, cosine_matrix_bw, self.kernel_max_attentive_bw)
        list_matching.extend([matching_fw, matching_bw])

        # max pooling matching ops
        matching_fw = self._max_pooling_matching(h1_fw, h2_fw, self.kernel_max_pool_fw)
        matching_bw = self._max_pooling_matching(h1_bw, h2_bw, self.kernel_max_pool_bw)
        list_matching.extend([matching_fw, matching_bw])

        return K.concatenate(list_matching, axis=-1)

    def _cosine_similarity(self, x1, x2):
        """Compute cosine similarity.
        # Arguments:
            x1: (..., embedding_size)
            x2: (..., embedding_size)
        """
        cos = K.sum(x1 * x2, axis=-1)
        x1_norm = K.sqrt(K.maximum(K.sum(K.square(x1), axis=-1), self.epsilon))
        x2_norm = K.sqrt(K.maximum(K.sum(K.square(x2), axis=-1), self.epsilon))
        cos = cos / x1_norm / x2_norm
        return cos

    def _cosine_matrix(self, x1, x2):
        """Cosine similarity matrix.
        Calculate the cosine similarities between each forward (or backward)
        contextual embedding h_i_p and every forward (or backward)
        contextual embeddings of the other sentence
        # Arguments
            x1: (batch_size, x1_timesteps, embedding_size)
            x2: (batch_size, x2_timesteps, embedding_size)
        # Output shape
            (batch_size, x1_timesteps, x2_timesteps)
        """
        # expand h1 shape to (batch_size, x1_timesteps, 1, embedding_size)
        x1 = K.expand_dims(x1, axis=2)
        # expand x2 shape to (batch_size, 1, x2_timesteps, embedding_size)
        x2 = K.expand_dims(x2, axis=1)
        # cosine matrix (batch_size, h1_timesteps, h2_timesteps)
        cos_matrix = self._cosine_similarity(x1, x2)
        return cos_matrix

    def _mean_attentive_vectors(self, x2, cosine_matrix):
        """Mean attentive vectors.
        Calculate mean attentive vector for the entire sentence by weighted
        summing all the contextual embeddings of the entire sentence
        # Arguments
            x2: sequence vectors, (batch_size, x2_timesteps, embedding_size)
            cosine_matrix: cosine similarities matrix of x1 and x2,
                           (batch_size, x1_timesteps, x2_timesteps)
        # Output shape
            (batch_size, x1_timesteps, embedding_size)
        """
        # (batch_size, x1_timesteps, x2_timesteps, 1)
        expanded_cosine_matrix = K.expand_dims(cosine_matrix, axis=-1)
        # (batch_size, 1, x2_timesteps, embedding_size)
        x2 = K.expand_dims(x2, axis=1)
        # (batch_size, x1_timesteps, embedding_size)
        weighted_sum = K.sum(expanded_cosine_matrix * x2, axis=2)
        # (batch_size, x1_timesteps, 1)
        sum_cosine = K.expand_dims(K.sum(cosine_matrix, axis=-1) + self.epsilon, axis=-1)
        # (batch_size, x1_timesteps, embedding_size)
        attentive_vector = weighted_sum / sum_cosine
        return attentive_vector

    def _max_attentive_vectors(self, x2, cosine_matrix):
        """Max attentive vectors.
        Calculate max attentive vector for the entire sentence by picking
        the contextual embedding with the highest cosine similarity
        as the attentive vector.
        # Arguments
            x2: sequence vectors, (batch_size, x2_timesteps, embedding_size)
            cosine_matrix: cosine similarities matrix of x1 and x2,
                           (batch_size, x1_timesteps, x2_timesteps)
        # Output shape
            (batch_size, x1_timesteps, embedding_size)
        """
        # (batch_size, x1_timesteps)
        max_x2_step = K.argmax(cosine_matrix, axis=-1)

        embedding_size = K.int_shape(x2)[-1]
        timesteps = K.int_shape(max_x2_step)[-1]
        if timesteps is None:
            timesteps = K.shape(max_x2_step)[-1]

        # collapse time dimension and batch dimension together
        # collapse x2 to (batch_size * x2_timestep, embedding_size)
        x2 = K.reshape(x2, (-1, embedding_size))
        # collapse max_x2_step to (batch_size * h1_timesteps)
        max_x2_step = K.reshape(max_x2_step, (-1,))
        # (batch_size * x1_timesteps, embedding_size)
        max_x2 = K.gather(x2, max_x2_step)
        # reshape max_x2, (batch_size, x1_timesteps, embedding_size)
        attentive_vector = K.reshape(max_x2, K.stack([-1, timesteps, embedding_size]))
        return attentive_vector

    def _time_distributed_multiply(self, x, w):
        """Element-wise multiply vector and weights.
        # Arguments
            x: sequence of hidden states, (batch_size, ?, embedding_size)
            w: weights of one matching strategy of one direction,
               (mp_dim, embedding_size)
        # Output shape
            (?, mp_dim, embedding_size)
        """
        # dimension of vector
        n_dim = K.ndim(x)
        embedding_size = K.int_shape(x)[-1]
        timesteps = K.int_shape(x)[1]
        if timesteps is None:
            timesteps = K.shape(x)[1]

        # collapse time dimension and batch dimension together
        x = K.reshape(x, (-1, embedding_size))
        # reshape to (?, 1, embedding_size)
        x = K.expand_dims(x, axis=1)
        # reshape weights to (1, mp_dim, embedding_size)
        w = K.expand_dims(w, axis=0)
        # element-wise multiply
        x = x * w
        # reshape to original shape
        if n_dim == 3:
            x = K.reshape(x, K.stack([-1, timesteps, self.mp_dim, embedding_size]))
            x.set_shape([None, None, None, embedding_size])
        elif n_dim == 2:
            x = K.reshape(x, K.stack([-1, self.mp_dim, embedding_size]))
            x.set_shape([None, None, embedding_size])
        return x

    def _full_matching(self, h1, h2, w):
        """Full matching operation.
        # Arguments
            h1: (batch_size, h1_timesteps, embedding_size)
            h2: (batch_size, h2_timesteps, embedding_size)
            w: weights of one direction, (mp_dim, embedding_size)
        # Output shape
            (batch_size, h1_timesteps, mp_dim)
        """
        # h2 forward last step hidden vector, (batch_size, embedding_size)
        h2_last_state = h2[:, -1, :]
        # h1 * weights, (batch_size, h1_timesteps, mp_dim, embedding_size)
        h1 = self._time_distributed_multiply(h1, w)
        # h2_last_state * weights, (batch_size, mp_dim, embedding_size)
        h2 = self._time_distributed_multiply(h2_last_state, w)
        # reshape to (batch_size, 1, mp_dim, embedding_size)
        h2 = K.expand_dims(h2, axis=1)
        # matching vector, (batch_size, h1_timesteps, mp_dim)
        matching = self._cosine_similarity(h1, h2)
        return matching

    def _max_pooling_matching(self, h1, h2, w):
        """Max pooling matching operation.
        # Arguments
            h1: (batch_size, h1_timesteps, embedding_size)
            h2: (batch_size, h2_timesteps, embedding_size)
            w: weights of one direction, (mp_dim, embedding_size)
        # Output shape
            (batch_size, h1_timesteps, mp_dim)
        """
        # h1 * weights, (batch_size, h1_timesteps, mp_dim, embedding_size)
        h1 = self._time_distributed_multiply(h1, w)
        # h2 * weights, (batch_size, h2_timesteps, mp_dim, embedding_size)
        h2 = self._time_distributed_multiply(h2, w)
        # reshape v1 to (batch_size, h1_timesteps, 1, mp_dim, embedding_size)
        h1 = K.expand_dims(h1, axis=2)
        # reshape v1 to (batch_size, 1, h2_timesteps, mp_dim, embedding_size)
        h2 = K.expand_dims(h2, axis=1)
        # cosine similarity, (batch_size, h1_timesteps, h2_timesteps, mp_dim)
        cos = self._cosine_similarity(h1, h2)
        # (batch_size, h1_timesteps, mp_dim)
        matching = K.max(cos, axis=2)
        return matching

    def _attentive_matching(self, h1, h2, cosine_matrix, w):
        """Attentive matching operation.
        # Arguments
            h1: (batch_size, h1_timesteps, embedding_size)
            h2: (batch_size, h2_timesteps, embedding_size)
            cosine_matrix: weights of hidden state h2,
                          (batch_size, h1_timesteps, h2_timesteps)
            w: weights of one direction, (mp_dim, embedding_size)
        # Output shape
            (batch_size, h1_timesteps, mp_dim)
        """
        # h1 * weights, (batch_size, h1_timesteps, mp_dim, embedding_size)
        h1 = self._time_distributed_multiply(h1, w)
        # attentive vector (batch_size, h1_timesteps, embedding_szie)
        attentive_vec = self._mean_attentive_vectors(h2, cosine_matrix)
        # attentive_vec * weights, (batch_size, h1_timesteps, mp_dim, embedding_size)
        attentive_vec = self._time_distributed_multiply(attentive_vec, w)
        # matching vector, (batch_size, h1_timesteps, mp_dim)
        matching = self._cosine_similarity(h1, attentive_vec)
        return matching

    def _max_attentive_matching(self, h1, h2, cosine_matrix, w):
        """Max attentive matching operation.
        # Arguments
            h1: (batch_size, h1_timesteps, embedding_size)
            h2: (batch_size, h2_timesteps, embedding_size)
            cosine_matrix: weights of hidden state h2,
                          (batch_size, h1_timesteps, h2_timesteps)
            w: weights of one direction, (mp_dim, embedding_size)
        # Output shape
            (batch_size, h1_timesteps, mp_dim)
        """
        # h1 * weights, (batch_size, h1_timesteps, mp_dim, embedding_size)
        h1 = self._time_distributed_multiply(h1, w)
        # max attentive vector (batch_size, h1_timesteps, embedding_szie)
        max_attentive_vec = self._max_attentive_vectors(h2, cosine_matrix)
        # max_attentive_vec * weights, (batch_size, h1_timesteps, mp_dim, embedding_size)
        max_attentive_vec = self._time_distributed_multiply(max_attentive_vec, w)
        # matching vector, (batch_size, h1_timesteps, mp_dim)
        matching = self._cosine_similarity(h1, max_attentive_vec)
        return matching
