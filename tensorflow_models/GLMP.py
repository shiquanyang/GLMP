import tensorflow as tf
from utils.config import *
from tensorflow_models.encoder import ContextRNN
from tensorflow_models.ExternalKnowledge import ExternalKnowledge
from tensorflow_models.decoder import LocalMemoryDecoder
import random
import numpy as np
from tensorflow.python.framework import ops
import json
from utils.measures import wer, moses_multi_bleu
from utils.tensorflow_masked_cross_entropy import *


class GLMP(tf.keras.Model):
    def __init__(self, hidden_size, lang, max_resp_len, path, task, lr, n_layers, dropout):
        super(GLMP, self).__init__()
        self.name = 'GLMP'
        self.task = task
        self.input_size = lang.n_words
        self.output_size = lang.n_words
        self.hidden_size = hidden_size
        self.lang = lang
        self.lr = lr
        self.n_layers = n_layers
        self.dropout = dropout
        self.max_resp_len = max_resp_len
        self.decoder_hop = n_layers
        self.softmax = tf.keras.layers.Softmax(0)
        self.encoder = ContextRNN(lang.n_words, hidden_size, dropout)
        self.extKnow = ExternalKnowledge(lang.n_words, hidden_size, n_layers, dropout)
        self.decoder = LocalMemoryDecoder(self.encoder.embedding, lang,
                                          hidden_size, self.decoder_hop, dropout)
        self.checkpoint = tf.train.Checkpoint(encoder=self.encoder,
                                              extKnow=self.extKnow,
                                              decoder=self.decoder)
        if path:
            self.checkpoint.restore(path)  # path include: directory + prefix + id.

        self.encoder_optimizer = tf.keras.optimizers.Adam(lr)
        self.extKnow_optimizer = tf.keras.optimizers.Adam(lr)
        self.decoder_optimizer = tf.keras.optimizers.Adam(lr)
        # TODO: lr scheduler.

        self.criterion_bce = tf.nn.sigmoid_cross_entropy_with_logits()  # need to check if this loss function actually equals pytorch criterion_bce.

        self.reset()

    def print_loss(self):
        print_loss_avg = self.loss / self.print_every
        print_loss_g = self.loss_g / self.print_every
        print_loss_v = self.loss_v / self.print_every
        print_loss_l = self.loss_l / self.print_every
        self.print_every += 1
        return 'L:{:.2f}, LE:{:.2f}, LG:{:.2f}, LP:{:.2f}'.format(
            print_loss_avg, print_loss_g, print_loss_v, print_loss_l)

    def reset(self):
        self.loss, self.print_every, self.loss_g, self.loss_v, self.loss_l = 0.0, 1.0, 0.0, 0.0, 0.0

    def save_model(self, dec_type):
        name_data = "KVR/" if self.task=='' else "BABI/"
        layer_info = str(self.n_layers)
        directory = 'save/GLMP-'+args["addName"]+name_data+str(self.task)+'HDD'+\
                    str(self.hidden_size)+'BSZ'+str(args['batch'])+'DR'+str(self.dropout)+\
                    'L'+layer_info+'lr'+str(self.lr)+str(dec_type)
        if not os.path.exists(directory):
            os.makedirs(directory)
        checkpoint_prefix = directory + '/ckpt'
        self.checkpoint.save(file_prefix=checkpoint_prefix)

    def encode_and_decode(self, data, max_target_length, use_teacher_forcing,
                          get_decoded_words, training):
        # build unknown mask for memory if training mode
        if args['unk_mask'] and training:  # different: training flag need to be fed from outside explicitly.
            story_size = data['context_arr'].size()
            rand_mask = np.ones(story_size)
            bi_mask = np.random.binomial([np.ones((story_size[0], story_size[1]))],
                                         1 - self.dropout)[0]
            rand_mask[:, :, 0] = rand_mask[:, :, 0] * bi_mask
            conv_rand_mask = np.ones(data['conv_arr'].size())
            for bi in range(story_size[0]):
                start, end = data['kb_arr_lengths'][bi], data['kb_arr_lengths'][bi] + data['conv_arr_lengths'][bi]
                conv_rand_mask[:end-start, bi, :] = rand_mask[bi, start:end, :]  # necessary to explictly move data to cuda ?
            conv_story = data['conv_arr'] * conv_rand_mask.long()
            story = data['context_arr'] * rand_mask.long()
        else:
            story, conv_story = data['context_arr'], data['conv_arr']

        # encode dialogue history and KB to vectors
        # TODO: need to check the shape and meaning of each tensor.
        dh_outputs, dh_hidden = self.encoder(conv_story, data['conv_arr_lengths'], training=training)
        global_pointer, kb_readout = self.extKnow.load_memory(story,
                                                              data['kb_arr_lengths'],
                                                              data['conv_arr_lengths'],
                                                              dh_hidden,
                                                              dh_outputs,
                                                              training=training)
        encoded_hidden = tf.concat([tf.squeeze(dh_hidden, 0), kb_readout], 1)

        # get the words that can be copy from the memory
        batch_size = len(data['context_arr_lengths'])
        self.copy_list = []
        for elm in data['context_arr_plain']:
            elm_temp = [word_arr[0] for word_arr in elm]
            self.copy_list.append(elm_temp)

        outputs_vocab, outputs_ptr, decoded_fine, decoded_coarse = self.decoder(self.extKnow,
                                                                                story.size(),
                                                                                data['context_arr_lengths'],
                                                                                self.copy_list,
                                                                                encoded_hidden,
                                                                                data['sketch_response'],
                                                                                max_target_length,
                                                                                batch_size,
                                                                                use_teacher_forcing,
                                                                                get_decoded_words,
                                                                                global_pointer,
                                                                                training=training)

        return outputs_vocab, outputs_ptr, decoded_fine, decoded_coarse, global_pointer

    @tf.function
    def train_batch(self, data, clip, reset=0):
        # model training process
        # no need to zero gradients of optimizers in tensorflow
        # encode and decode
        with tf.GradientTape() as tape:
            use_teacher_forcing = random.random() < args['teacher_forcing_ratio']
            max_target_length = max(data['response_lengths'])
            all_decoder_outputs_vocab, all_decoder_outputs_ptr, _, _, global_pointer = self.encode_and_decode(data,
                                                                                                              max_target_length,
                                                                                                              use_teacher_forcing,
                                                                                                              False,
                                                                                                              True)
            # loss calculation and backpropagation
            loss_g = self.criterion_bce(global_pointer, data['selector_index'])
            loss_v = masked_cross_entropy(tf.transpose(all_decoder_outputs_vocab, [1, 0, 2]),  # need to transpose ?
                                          data['sketch_response'],
                                          data['respose_lengths'])
            loss_l = masked_cross_entropy(tf.transpose(all_decoder_outputs_ptr, [1, 0, 2]),  # need to transpose ?
                                          data['ptr_index'],
                                          data['response_lengths'])
            loss = loss_g + loss_v + loss_l

        # compute gradients for encoder, decoder and external knowledge
        encoder_variables = self.encoder.trainable_variables
        extKnow_variables = self.extKnow.trainable_variables
        decoder_variables = self.decoder.trainable_variables
        encoder_gradients = tape.gradient(loss, encoder_variables)
        extKnow_gradients = tape.gradient(loss, extKnow_variables)
        decoder_gradients = tape.gradient(loss, decoder_variables)

        # clip gradients
        clipped_encoder_gradients = [elem if isinstance(elem, ops.IndexedSlices) else tf.clip_by_norm(elem, clip) for elem in encoder_gradients]
        clipped_extKnow_gradients = [elem if isinstance(elem, ops.IndexedSlices) else tf.clip_by_norm(elem, clip) for elem in extKnow_gradients]
        clipped_decoder_gradients = [elem if isinstance(elem, ops.IndexedSlices) else tf.clip_by_norm(elem, clip) for elem in decoder_gradients]

        # apply update
        self.encoder_optimizer.apply_gradients(
            zip(clipped_encoder_gradients, self.encoder.trainable_variables))
        self.extKnow_optimizer.apply_gradients(
            zip(clipped_extKnow_gradients, self.extKnow.trainable_variables))
        self.decoder_optimizer.apply_gradients(
            zip(clipped_decoder_gradients, self.decoder.trainable_variables))

        self.loss += loss.numpy()
        self.loss_g += loss_g.numpy()
        self.loss_v += loss_v.numpy()
        self.loss_l += loss_l.numpy()

    def evaluate(self, dev, matric_best, early_stop=None):
        print('STARTING EVALUATION:')

        ref, hyp = [], []
        acc, total = 0, 0
        dialog_acc_dict = {}
        F1_pred, F1_cal_pred, F1_nav_pred, F1_wet_pred = 0, 0, 0, 0
        F1_count, F1_cal_count, F1_nav_count, F1_wet_count = 0, 0, 0, 0
        pbar = tqdm(enumerate(dev), total=len(dev))
        new_precision, new_recall, new_f1_score = 0, 0, 0

        if args['dataset'] == 'kvr':
            with open('data/KVR/kvret_entities.json') as f:
                global_entity = json.load(f)
                global_entity_list = []
                for key in global_entity.keys():
                    if key != 'poi':
                        global_entity_list += [item.lower().replace(' ', '_') for item in global_entity[key]]
                    else:
                        for item in global_entity['poi']:
                            global_entity_list += [item[k].lower().replace(' ', '_') for k in item.keys()]
                global_entity_list = list(set(global_entity_list))

        for j, data_dev in pbar:
            # Encode and Decode
            _, _, decoded_fine, decoded_coarse, global_pointer = self.encode_and_decode(data_dev,
                                                                                        self.max_resp_len,
                                                                                        False,
                                                                                        True,
                                                                                        False)
            decoded_coarse = np.transpose(decoded_coarse)
            decoded_fine = np.transpose(decoded_fine)
            for bi, row in enumerate(decoded_fine):
                st = ''
                for e in row:
                    if e == 'EOS':
                        break
                    else:
                        st += e + ' '
                st_c = ''
                for e in decoded_coarse[bi]:
                    if e == 'EOS':
                        break
                    else:
                        st_c += e + ' '
                pred_sent = st.lstrip().rstrip()
                pred_sent_coarse = st_c.lstrip().rstrip()
                gold_sent = data_dev['response_plain'][bi].lstrip().rstrip()
                ref.append(gold_sent)
                hyp.append(pred_sent)

                if args['dataset'] == 'kvr':
                    # compute F1 SCORE
                    single_f1, count = self.compute_prf(data_dev['ent_index'][bi], pred_sent.split(),
                                                        global_entity_list, data_dev['kb_arr_plain'][bi])
                    F1_pred += single_f1
                    F1_count += count
                    single_f1, count = self.compute_prf(data_dev['ent_idx_cal'][bi], pred_sent.split(),
                                                        global_entity_list, data_dev['kb_arr_plain'][bi])
                    F1_cal_pred += single_f1
                    F1_cal_count += count
                    single_f1, count = self.compute_prf(data_dev['ent_idx_nav'][bi], pred_sent.split(),
                                                        global_entity_list, data_dev['kb_arr_plain'][bi])
                    F1_nav_pred += single_f1
                    F1_nav_count += count
                    single_f1, count = self.compute_prf(data_dev['ent_idx_wet'][bi], pred_sent.split(),
                                                        global_entity_list, data_dev['kb_arr_plain'][bi])
                    F1_wet_pred += single_f1
                    F1_wet_count += count
                else:
                    # compute Dialogue Accuracy Score
                    current_id = data_dev['ID'][bi]
                    if current_id not in dialog_acc_dict.keys():
                        dialog_acc_dict[current_id] = []
                    if gold_sent == pred_sent:
                        dialog_acc_dict[current_id].append(1)
                    else:
                        dialog_acc_dict[current_id].append(0)

                # compute Per-response Accuracy Score
                total += 1
                if (gold_sent == pred_sent):
                    acc += 1

                if args['genSample']:
                    self.print_examples(bi, data_dev, pred_sent, pred_sent_coarse, gold_sent)

        bleu_score = moses_multi_bleu(np.array(hyp), np.array(ref), lowercase=True)
        acc_score = acc / float(total)
        print("ACC SCORE:\t" + str(acc_score))

        if args['dataset'] == 'kvr':
            F1_score = F1_pred / float(F1_count)
            print("F1 SCORE:\t{}".format(F1_pred / float(F1_count)))
            print("\tCAL F1:\t{}".format(F1_cal_pred / float(F1_cal_count)))
            print("\tWET F1:\t{}".format(F1_wet_pred / float(F1_wet_count)))
            print("\tNAV F1:\t{}".format(F1_nav_pred / float(F1_nav_count)))
            print("BLEU SCORE:\t" + str(bleu_score))
        else:
            dia_acc = 0
            for k in dialog_acc_dict.keys():
                if len(dialog_acc_dict[k]) == sum(dialog_acc_dict[k]):
                    dia_acc += 1
            print("Dialog Accuracy:\t" + str(dia_acc * 1.0 / len(dialog_acc_dict.keys())))

        if (early_stop == 'BLEU'):
            if (bleu_score >= matric_best):
                self.save_model('BLEU-' + str(bleu_score))
                print("MODEL SAVED")
            return bleu_score
        elif (early_stop == 'ENTF1'):
            if (F1_score >= matric_best):
                self.save_model('ENTF1-{:.4f}'.format(F1_score))
                print("MODEL SAVED")
            return F1_score
        else:
            if (acc_score >= matric_best):
                self.save_model('ACC-{:.4f}'.format(acc_score))
                print("MODEL SAVED")
            return acc_score

    def compute_prf(self, gold, pred, global_entity_list, kb_plain):
        local_kb_word = [k[0] for k in kb_plain]
        TP, FP, FN = 0, 0, 0
        if len(gold) != 0:
            count = 1
            for g in gold:
                if g in pred:
                    TP += 1
                else:
                    FN += 1
            for p in set(pred):
                if p in global_entity_list or p in local_kb_word:
                    if p not in gold:
                        FP += 1
            precision = TP / float(TP + FP) if (TP + FP) != 0 else 0
            recall = TP / float(TP + FN) if (TP + FN) != 0 else 0
            F1 = 2 * precision * recall / float(precision + recall) if (precision + recall) != 0 else 0
        else:
            precision, recall, F1, count = 0, 0, 0, 0
        return F1, count

    def print_examples(self, batch_idx, data, pred_sent, pred_sent_coarse, gold_sent):
        kb_len = len(data['context_arr_plain'][batch_idx]) - data['conv_arr_lengths'][batch_idx] - 1
        print("{}: ID{} id{} ".format(data['domain'][batch_idx], data['ID'][batch_idx], data['id'][batch_idx]))
        for i in range(kb_len):
            kb_temp = [w for w in data['context_arr_plain'][batch_idx][i] if w != 'PAD']
            kb_temp = kb_temp[::-1]
            if 'poi' not in kb_temp:
                print(kb_temp)
        flag_uttr, uttr = '$u', []
        for word_idx, word_arr in enumerate(data['context_arr_plain'][batch_idx][kb_len:]):
            if word_arr[1] == flag_uttr:
                uttr.append(word_arr[0])
            else:
                print(flag_uttr, ': ', " ".join(uttr))
                flag_uttr = word_arr[1]
                uttr = [word_arr[0]]
        print('Sketch System Response : ', pred_sent_coarse)
        print('Final System Response : ', pred_sent)
        print('Gold System Response : ', gold_sent)
        print('\n')