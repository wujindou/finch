from config import args
from attention_gru_cell import AttentionGRUCell

import tensorflow as tf
import numpy as np
import pprint


class MemoryNetwork:
    def __init__(self, params):
        self.params = params
        self.build_graph()


    def build_graph(self):
        self.build_placeholders()
        logits = self.forward()
        self.backward(logits)


    def build_placeholders(self):
        self.placeholders = {
            'inputs': tf.placeholder(
                tf.int64, (None, self.params['max_input_len'], self.params['max_sent_len'])),
            'questions': tf.placeholder(tf.int64, (None, self.params['max_quest_len'])),
            'answers': tf.placeholder(tf.int64, (None, self.params['max_answer_len'])),
            'inputs_len': tf.placeholder(tf.int32, (None)),
            'inputs_sent_len': tf.placeholder(tf.int32, (None, self.params['max_input_len'])),
            'questions_len': tf.placeholder(tf.int32, (None)),
            'answers_len': tf.placeholder(tf.int32, (None)),
            'is_training': tf.placeholder(tf.bool)}


    def forward(self):
        embedding = tf.get_variable('lookup_table', [self.params['vocab_size'], args.embed_dim], tf.float32)
        embedding = self.zero_index_pad(embedding)

        fact_vecs = self.input_module(embedding)
        q_vec = self.question_module(embedding)
        memory = self.memory_module(fact_vecs, q_vec)
        logits = self.answer_module(memory, q_vec, embedding)

        return logits


    def backward(self, logits):
        targets = self.placeholders['answers']
        masks = tf.ones_like(targets, tf.float32)
        self.loss_op = tf.reduce_mean(tf.contrib.seq2seq.sequence_loss(
            logits=logits, targets=targets, weights=masks))
        self.acc_op = tf.reduce_mean(tf.to_float(tf.equal(tf.argmax(logits, -1), targets)))

        params = tf.trainable_variables()
        grads = tf.gradients(self.loss_op, params)
        clipped_grads, _ = tf.clip_by_global_norm(grads, args.clip_norm)
        self.train_op = tf.train.AdamOptimizer().apply_gradients(zip(clipped_grads, params))


    def input_module(self, embedding):
        with tf.variable_scope('input_module'):
            inputs = tf.nn.embedding_lookup(embedding, self.placeholders['inputs'])        # (B, I, S, D)
            position = self.position_encoding(self.params['max_sent_len'], args.embed_dim)
            inputs = tf.reduce_sum(inputs * position, 2)                                  # (B, I, D)
            fact_vecs, _ = tf.nn.dynamic_rnn(                                             
                self.GRU(), inputs, self.placeholders['inputs_len'], dtype=np.float32)    # (B, I, D)
            fact_vecs = tf.layers.dropout(
                fact_vecs, args.dropout_rate, training=self.placeholders['is_training'])
            return fact_vecs


    def question_module(self, embedding):
        with tf.variable_scope('question_module'):
            questions = tf.nn.embedding_lookup(embedding, self.placeholders['questions'])
            _, q_vec = tf.nn.dynamic_rnn(
                self.GRU(), questions, self.placeholders['questions_len'], dtype=np.float32)
            return q_vec


    def memory_module(self, fact_vecs, q_vec):
        memory = q_vec
        for i in range(args.n_episodes):
            print('==> Building Episode', i)
            episode = self.episode(memory, q_vec, fact_vecs, i)
            with tf.variable_scope('memory_%d' % i):
                memory = tf.layers.dense(
                    tf.concat([memory, episode, q_vec], 1), args.hidden_size, tf.nn.relu)
        return memory  # (B, D)


    def episode(self, memory, q_vec, fact_vecs, i):
        attentions = [self.attention(memory, q_vec, fact_vec, bool(i) or bool(n_fact))
            for n_fact, fact_vec in enumerate(tf.unstack(fact_vecs, axis=1))]    # T list of (B,)
        attentions = tf.transpose(tf.stack(attentions))                          # (B, n_fact)
        attentions = tf.nn.softmax(attentions)                                   # (B, n_fact)
        attentions = tf.expand_dims(attentions, -1)
        attention_cell = AttentionGRUCell(args.hidden_size)         
        with tf.variable_scope('attention_gru', reuse=(i>0)):
            _, episode = tf.nn.dynamic_rnn(
                attention_cell,
                tf.concat([fact_vecs, attentions], 2),                           # (B, n_fact, D+1)
                self.placeholders['inputs_len'],
                dtype=np.float32)
        return episode                                                           # (B, D)


    def attention(self, prev_memory, q_vec, fact_vec, reuse):
        with tf.variable_scope('attention', reuse=reuse):
            features = [fact_vec * q_vec,
                        fact_vec * prev_memory,
                        tf.abs(fact_vec - q_vec),
                        tf.abs(fact_vec - prev_memory)]
            feature_vec = tf.concat(features, 1)
            attention = tf.layers.dense(feature_vec, args.embed_dim, tf.nn.tanh, reuse=reuse, name='fc1')
            attention = tf.layers.dense(attention, 1, reuse=reuse, name='fc2')
        return tf.squeeze(attention, 1)


    def answer_module(self, memory, q_vec, embedding):
        memory = tf.layers.dropout(
            memory, args.dropout_rate, training=self.placeholders['is_training'])
        answer_inputs = self.shift_right(self.placeholders['answers'])
        
        with tf.variable_scope('answer_module'):
            helper = tf.contrib.seq2seq.TrainingHelper(
                inputs = tf.nn.embedding_lookup(embedding, answer_inputs),
                sequence_length = tf.fill([self.batch_size], 2))
            decoder = tf.contrib.seq2seq.BasicDecoder(
                cell = self.GRU(),
                helper = helper,
                initial_state = memory,
                output_layer = tf.layers.Dense(self.params['vocab_size']))
            decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
                decoder = decoder)
        logits = decoder_output.rnn_output

        with tf.variable_scope('answer_module', reuse=True):
            helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
                embedding = embedding,
                start_tokens = tf.tile(
                    tf.constant([self.params['<start>']], dtype=tf.int32), [self.batch_size]),
                end_token = self.params['<end>'])
            decoder = tf.contrib.seq2seq.BasicDecoder(
                cell = self.GRU(reuse=True),
                helper = helper,
                initial_state = memory,
                output_layer = tf.layers.Dense(self.params['vocab_size'], _reuse=True))
            decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
                decoder = decoder,
                maximum_iterations = self.params['max_answer_len'])
        self.predicted_ids = decoder_output.sample_id

        return logits


    def shift_right(self, x):
        self.batch_size = tf.shape(self.placeholders['inputs'])[0]
        start = tf.to_int64(tf.fill([self.batch_size, 1], self.params['<start>']))
        return tf.concat([start, x[:, :-1]], 1)


    def GRU(self, reuse=None):
        return tf.nn.rnn_cell.GRUCell(
            args.hidden_size, kernel_initializer=tf.orthogonal_initializer(), reuse=reuse)


    def zero_index_pad(self, embedding):
        return tf.concat((tf.zeros([1, args.embed_dim]), embedding[1:, :]), axis=0)


    def position_encoding(self, sentence_size, embedding_size):
        encoding = np.ones((embedding_size, sentence_size), dtype=np.float32)
        ls = sentence_size + 1
        le = embedding_size + 1
        for i in range(1, le):
            for j in range(1, ls):
                encoding[i-1, j-1] = (i - (le-1)/2) * (j - (ls-1)/2)
        encoding = 1 + 4 * encoding / embedding_size / sentence_size
        return np.transpose(encoding)


    def train_session(self, sess, batch):
        (i, q, a, i_len, i_sent_len, q_len, a_len) = batch
        feed_dict = {
            self.placeholders['inputs']: i,
            self.placeholders['questions']: q,
            self.placeholders['answers']: a,
            self.placeholders['inputs_len']: i_len,
            self.placeholders['inputs_sent_len']: i_sent_len,
            self.placeholders['questions_len']: q_len,
            self.placeholders['answers_len']:a_len,
            self.placeholders['is_training']: True}
        _, loss, acc = sess.run([self.train_op, self.loss_op, self.acc_op], feed_dict)
        return loss, acc


    def predict_session(self, sess, batch):
        (i, q, _, i_len, i_sent_len, q_len, _) = batch
        feed_dict = {
            self.placeholders['inputs']: i,
            self.placeholders['questions']: q,
            self.placeholders['inputs_len']: i_len,
            self.placeholders['inputs_sent_len']: i_sent_len,
            self.placeholders['questions_len']: q_len,
            self.placeholders['is_training']: False}
        ids = sess.run(self.predicted_ids, feed_dict)
        return ids


    def demo_session(self, sess, i, q, i_len, i_sent_len, q_len, idx2word, demo, demo_idx):
        feed_dict = {
            self.placeholders['inputs']: np.expand_dims(i, 0),
            self.placeholders['questions']: np.atleast_2d(q),
            self.placeholders['inputs_len']: np.atleast_1d(i_len),
            self.placeholders['inputs_sent_len']: np.atleast_2d(i_sent_len),
            self.placeholders['questions_len']: np.atleast_1d(q_len),
            self.placeholders['is_training']: False}
        ids = sess.run(self.predicted_ids, feed_dict)[0]

        demo_i, demo_q, demo_a = demo
        pprint.PrettyPrinter(indent=4).pprint(demo_i[demo_idx])
        print(demo_q[demo_idx])
        print(demo_a[demo_idx])
        print('- '*12)
        print([idx2word[id] for id in ids])
