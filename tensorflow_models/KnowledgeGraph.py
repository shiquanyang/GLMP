import tensorflow as tf
from utils.config import *
import pdb
from tensorflow.python.ops import embedding_ops
from tensorflow_models.GraphAttentionLayer import GraphAttentionLayer


class KnowledgeGraph(tf.keras.Model):
    def __init__(self, vocab, embedding_dim, hop, nhid, nheads, alpha, dropout):
        super(KnowledgeGraph, self).__init__()
        self.max_hops = hop
        self.embedding_dim = embedding_dim
        self.vocab = vocab
        self.nhid = nhid
        self.nheads = nheads
        self.alpha = alpha
        self.dropout = dropout
        self.dropout_layer = tf.keras.layers.Dropout(self.dropout)
        # input embedding layer
        self.embeddings = tf.keras.layers.Embedding(self.vocab,
                                                    self.embedding_dim,
                                                    embeddings_initializer=tf.initializers.RandomNormal(0.0, 0.1))  # different: no masking for pad token, pad token embedding does not equal zero, only support one hop.
        # multi-head attention layer
        self.attentions = [GraphAttentionLayer(embedding_dim, nhid, dropout, alpha, concat=True) for _ in range(nheads)]

        # multi-head attention layer
        self.attentions_2 = [GraphAttentionLayer(nheads * nhid, nhid, dropout, alpha, concat=True) for _ in range(nheads)]

        # output layer
        self.out_layer = [GraphAttentionLayer(nheads * nhid, nhid, dropout, alpha, concat=False) for _ in range(nheads)]

        self.softmax = tf.keras.layers.Softmax(1)
        self.sigmoid = tf.keras.layers.Activation('sigmoid')
        self.elu = tf.keras.layers.ELU()

    def add_lm_embedding(self, full_memory, kb_len, conv_len, hiddens):
        output_list = []
        for bi in range(full_memory.shape[0]):
            stack_list = []
            kb_aligns = tf.zeros([kb_len[bi] - 1, full_memory.shape[2]])
            null_aligns = tf.zeros([1, full_memory.shape[2]])
            # print('kb_len:', kb_len[bi].numpy())
            if (kb_len[bi] - 1).numpy().item() != 0:
                kb_aligns_slices = tf.split(kb_aligns, num_or_size_splits=kb_aligns.shape[0], axis=0)
                for tensor in kb_aligns_slices:
                    stack_list.append(tensor)
            # pdb.set_trace()
            hiddens_slices = tf.split(hiddens[bi, :conv_len[bi], :], num_or_size_splits=conv_len[bi].numpy().item(),
                                      axis=0)
            # print('hiddens len:', hiddens[bi].shape[0])
            for tensor in hiddens_slices:
                stack_list.append(tensor)
            # pdb.set_trace()
            stack_list.append(null_aligns)
            pad_len = full_memory.shape[1] - kb_len[bi] - conv_len[bi]
            # pdb.set_trace()
            if pad_len.numpy().item() != 0:
                pad_aligns = tf.zeros([pad_len, full_memory.shape[2]])
                pad_aligns_slice = tf.split(pad_aligns, num_or_size_splits=pad_aligns.shape[0], axis=0)
                for tensor in pad_aligns_slice:
                    stack_list.append(tensor)
                # pdb.set_trace()
            # print('stack_list length:', len(stack_list))
            add_tensor = tf.squeeze(tf.stack(stack_list, axis=0), 1)
            updated_full_memory = full_memory[bi] + add_tensor
            output_list.append(updated_full_memory)
        output = tf.stack(output_list, axis=0)
        return output

    def gen_embedding_mask(self, input):
        raw_mask_array = [[1.0]] * PAD_token + [[0.0]] + [[1.0]] * (self.vocab - PAD_token - 1)
        mask = embedding_ops.embedding_lookup(raw_mask_array, tf.cast(input, dtype=tf.int32))
        ret_mask = tf.tile(tf.expand_dims(mask, 2), [1, 1, self.embedding_dim])
        return ret_mask

    def update_pad_token_adj(self, adj, kb_len, conv_len):
        batch_size = adj.shape[0]
        max_len = adj.shape[1]
        adj_array = adj.numpy()
        for i in range(batch_size):
            kb_len_i = kb_len[i] - 1
            conv_len_i = conv_len[i]
            context_len_i = kb_len_i + conv_len_i + 1
            for k in range(context_len_i, max_len):
                adj_array[i, k, k] = 1
        ret_adj = tf.convert_to_tensor(adj_array)
        return ret_adj

    def load_graph(self, story, kb_len, conv_len, hidden, dh_outputs, adj, training=True):
        u = [hidden]  # different: hidden without squeeze(0), hidden: batch_size * embedding_size.
        story_size = story.shape
        self.m_story = []

        # transform one-hot to embeddings
        # pdb.set_trace()
        embedding_A = self.embeddings(tf.reshape(story, [story_size[0], -1]))  # story: batch_size * seq_len * MEM_TOKEN_SIZE, embedding_A: batch_size * memory_size * MEM_TOKEN_SIZE * embedding_dim.
        # pad_mask = self.gen_embedding_mask(tf.reshape(story, [story_size[0], -1]))
        # mask pad token embeddings
        # embedding_A = tf.multiply(embedding_A, pad_mask)
        embedding_A = tf.reshape(embedding_A, [story_size[0], story_size[1], story_size[2], embedding_A.shape[-1]])  # embedding_A: batch_size * memory_size * MEM_TOKEN_SIZE * embedding_dim.
        embedding_A = tf.math.reduce_sum(embedding_A, 2)  # embedding_A: batch_size * memory_size * embedding_dim.
        if not args['ablationH']:
            embedding_A = self.add_lm_embedding(embedding_A, kb_len, conv_len, dh_outputs)
        if training:
            embedding_A = self.dropout_layer(embedding_A, training=training)

        # pdb.set_trace()
        adj = self.update_pad_token_adj(adj, kb_len, conv_len)
        # First Layer, GraphAttentionLayer to update word embeddings
        embedding_A = tf.concat([att(embedding_A, adj, training) for att in self.attentions], axis=2)  # embedding_A: batch_size * memory_size * (nhead * embedding_dim)
        # Second Layer
        embedding_A = tf.concat([att(embedding_A, adj, training) for att in self.attentions_2])
        # Output Layer
        # pdb.set_trace()
        embedding_A = [head(embedding_A, adj, training) for head in self.out_layer]
        # average multi-head embeddings
        # pdb.set_trace()
        embedding_A = tf.reduce_sum(tf.stack(embedding_A, axis=0), axis=0) / tf.cast(self.nheads, dtype=tf.float32)  # embedding_A: batch_size * memory_size * embedding_dim.
        # apply non-linearity
        embedding_A = self.sigmoid(embedding_A)  # embedding_A: batch_size * memory_size * embedding_dim.
        # add for mimic memory
        # embedding_A = tf.identity(embedding_A)  # embedding_A: batch_size * memory_size * embedding_dim.

        # if training:
        #     embedding_A = self.dropout_layer(embedding_A, training=training)

        u_temp = tf.tile(tf.expand_dims(u[-1], 1), [1, embedding_A.shape[1], 1])  # u_temp: batch_size * memory_size * embedding_dim.
        prob_logits = tf.math.reduce_sum((embedding_A * u_temp), 2)  # prob_logits: batch_size * memory_size

        self.m_story.append(embedding_A)

        return self.sigmoid(prob_logits), u[-1], prob_logits

    def call(self, query_vector, global_pointer, training=True):
        u = [query_vector]  # query_vector: batch_size * embedding_dim.

        embed_A = self.m_story[0]  # embed_A: batch_size * memory_size * embedding_dim.
        if not args['ablationG']:
            embed_A = embed_A * tf.tile(tf.expand_dims(global_pointer, 2), [1, 1, embed_A.shape[2]])

        u_temp = tf.tile(tf.expand_dims(u[-1], 1), [1, embed_A.shape[1], 1])  # u_temp: batch_size * memory_size * embedding_dim.
        prob_logits = tf.math.reduce_sum((embed_A * u_temp), 2)  # prob_logits: batch_size * memory_size.
        prob_soft = self.softmax(prob_logits)  # prob_soft: batch_size * memory_size.

        return prob_soft, prob_logits


class AttrProxy(object):
    def __init__(self, module, prefix):
        self.module = module
        self.prefix = prefix

    def __getitem__(self, i):
        return getattr(self.module, self.prefix + str(i))
