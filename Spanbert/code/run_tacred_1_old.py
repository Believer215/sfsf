# Copyright (c) 2019, Facebook, Inc. and its affiliates. All Rights Reserved
"""
Run BERT on several relation extraction benchmarks.
Adding some special tokens instead of doing span pair prediction in this version.
"""

import argparse
import logging
import os
import random
import time
import json
import uuid
import json

import numpy as np
import torch
from torch.utils.data import DataLoader, TensorDataset
import torch.nn.functional as F
from collections import Counter

from torch.nn import CrossEntropyLoss

from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE, WEIGHTS_NAME, CONFIG_NAME
from pytorch_pretrained_bert.modeling import BertForSequenceClassification
from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.optimization import BertAdam, warmup_linear

CLS = "[CLS]"
SEP = "[SEP]"

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


class InputExample(object):
    """A single training/test example for span pair classification."""

    def __init__(self, guid, sentence, span1, span2, ner1, ner2, label):
        self.guid = guid
        self.sentence = sentence
        self.span1 = span1
        self.span2 = span2
        self.ner1 = ner1
        self.ner2 = ner2
        self.label = label

class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids, label_id):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id

class DataProcessor(object):
    """Processor for the TACRED data set."""

    @classmethod
    def _read_json(cls, input_file):
        with open(input_file, "r", encoding='utf-8') as reader:
            data = json.load(reader)
        return data

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_json(os.path.join(data_dir, "train.json")), "train")

    def get_dev_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_json(os.path.join(data_dir, "dev.json")), "dev")

    def get_test_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_json(os.path.join(data_dir, "preprocessing_data.json")), "test")

    def get_labels(self, data_dir, negative_label="no_relation"):
        """See base class."""
        dataset = self._read_json(os.path.join(data_dir, "train.json"))
        count = Counter()
        for example in dataset:
            count[example['relation']] += 1
#         logger.info("%d labels" % len(count))
#         # Make sure the negative label is alwyas 0
        labels = [negative_label]
        for label, count in count.most_common():
#             logger.info("%s: %.2f%%" % (label, count * 100.0 / len(dataset)))
             if label not in labels:
                labels.append(label)
        return labels

    def _create_examples(self, dataset, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for example in dataset:
            sentence = [convert_token(token) for token in example['token']]
#             print(sentence)
            assert example['subj_start'] >= 0 and example['subj_start'] <= example['subj_end'] \
                and example['subj_end'] < len(sentence)
            assert example['obj_start'] >= 0 and example['obj_start'] <= example['obj_end'] \
                and example['obj_end'] < len(sentence)
            examples.append(InputExample(guid=example['id'],
                             sentence=sentence,
                             span1=(example['subj_start'], example['subj_end']),
                             span2=(example['obj_start'], example['obj_end']),
                             ner1=example['subj_type'],
                             ner2=example['obj_type'],
                             label=example['relation']))
        return examples


def convert_examples_to_features(examples, label2id, max_seq_length, tokenizer, special_tokens, mode='text'):
    """Loads a data file into a list of `InputBatch`s."""


    def get_special_token(w):
        if w not in special_tokens:
            special_tokens[w] = "[unused%d]" % (len(special_tokens) + 1)
        return special_tokens[w]

    num_tokens = 0
    num_fit_examples = 0
    num_shown_examples = 0
    features = []
    for (ex_index, example) in enumerate(examples):
#         if ex_index % 10000 == 0:
#             logger.info("Writing example %d of %d" % (ex_index, len(examples)))

        tokens = [CLS]
        SUBJECT_START = get_special_token("SUBJ_START")
        SUBJECT_END = get_special_token("SUBJ_END")
        OBJECT_START = get_special_token("OBJ_START")
        OBJECT_END = get_special_token("OBJ_END")
        SUBJECT_NER = get_special_token("SUBJ=%s" % example.ner1)
        OBJECT_NER = get_special_token("OBJ=%s" % example.ner2)

        if mode.startswith("text"):
            for i, token in enumerate(example.sentence):
                if i == example.span1[0]:
                    tokens.append(SUBJECT_START)
                if i == example.span2[0]:
                    tokens.append(OBJECT_START)
                for sub_token in tokenizer.tokenize(token):
                    tokens.append(sub_token)
                if i == example.span1[1]:
                    tokens.append(SUBJECT_END)
                if i == example.span2[1]:
                    tokens.append(OBJECT_END)
            if mode == "text_ner":
                tokens = tokens + [SEP, SUBJECT_NER, SEP, OBJECT_NER, SEP]
            else:
                tokens.append(SEP)
        else:
            subj_tokens = []
            obj_tokens = []
            for i, token in enumerate(example.sentence):
                if i == example.span1[0]:
                    tokens.append(SUBJECT_NER)
                if i == example.span2[0]:
                    tokens.append(OBJECT_NER)
                if (i >= example.span1[0]) and (i <= example.span1[1]):
                    for sub_token in tokenizer.tokenize(token):
                        subj_tokens.append(sub_token)
                elif (i >= example.span2[0]) and (i <= example.span2[1]):
                    for sub_token in tokenizer.tokenize(token):
                        obj_tokens.append(sub_token)
                else:
                    for sub_token in tokenizer.tokenize(token):
                        tokens.append(sub_token)
            if mode == "ner_text":
                tokens.append(SEP)
                for sub_token in subj_tokens:
                    tokens.append(sub_token)
                tokens.append(SEP)
                for sub_token in obj_tokens:
                    tokens.append(sub_token)
            tokens.append(SEP)
        num_tokens += len(tokens)

        if len(tokens) > max_seq_length:
            tokens = tokens[:max_seq_length]
        else:
            num_fit_examples += 1

        segment_ids = [0] * len(tokens)
        input_ids = tokenizer.convert_tokens_to_ids(tokens)
        input_mask = [1] * len(input_ids)
        padding = [0] * (max_seq_length - len(input_ids))
        input_ids += padding
        input_mask += padding
        segment_ids += padding
        label_id = label2id[example.label]
        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        if num_shown_examples < 20:
            if (ex_index < 5) or (label_id > 0):
                num_shown_examples += 1
                # logger.info("*** Example ***")
                # logger.info("guid: %s" % (example.guid))
                # logger.info("tokens: %s" % " ".join(
                #         [str(x) for x in tokens]))
                # logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
                # logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
                # logger.info("segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
                # logger.info("label: %s (id = %d)" % (example.label, label_id))

        features.append(
                InputFeatures(input_ids=input_ids,
                              input_mask=input_mask,
                              segment_ids=segment_ids,
                              label_id=label_id))
    # logger.info("Average #tokens: %.2f" % (num_tokens * 1.0 / len(examples)))
    # logger.info("%d (%.2f %%) examples can fit max_seq_length = %d" % (num_fit_examples,
    #             num_fit_examples * 100.0 / len(examples), max_seq_length))
    return features


def convert_token(token):
    """ Convert PTB tokens to normal tokens """
    if (token.lower() == '-lrb-'):
            return '('
    elif (token.lower() == '-rrb-'):
        return ')'
    elif (token.lower() == '-lsb-'):
        return '['
    elif (token.lower() == '-rsb-'):
        return ']'
    elif (token.lower() == '-lcb-'):
        return '{'
    elif (token.lower() == '-rcb-'):
        return '}'
    return token


def simple_accuracy(preds, labels):
    return (preds == labels).mean()


def compute_f1(preds, labels):
    n_gold = n_pred = n_correct = 0
    for pred, label in zip(preds, labels):
        if pred != 0:
            n_pred += 1
        if label != 0:
            n_gold += 1
        if (pred != 0) and (label != 0) and (pred == label):
            n_correct += 1
    if n_correct == 0:
        return {'precision': 0.0, 'recall': 0.0, 'f1': 0.0}
    else:
        prec = n_correct * 1.0 / n_pred
        recall = n_correct * 1.0 / n_gold
        if prec + recall > 0:
            f1 = 2.0 * prec * recall / (prec + recall)
        else:
            f1 = 0.0
        return {'precision': prec, 'recall': recall, 'f1': f1}


def evaluate(model, device, eval_dataloader, eval_label_ids, num_labels, verbose=True):
    model.eval()
    eval_loss = 0
    nb_eval_steps = 0
    preds = []
    probs = []
    probs_total = []
    final_probs = []
    for input_ids, input_mask, segment_ids, label_ids in eval_dataloader:
        input_ids = input_ids.to(device)
        input_mask = input_mask.to(device)
        segment_ids = segment_ids.to(device)
        label_ids = label_ids.to(device)
        with torch.no_grad():
            logits = model(input_ids, segment_ids, input_mask, labels=None)
        loss_fct = CrossEntropyLoss()
        tmp_eval_loss = loss_fct(logits.view(-1, num_labels), label_ids.view(-1))
        eval_loss += tmp_eval_loss.mean().item()
        nb_eval_steps += 1
        if len(preds) == 0:
            preds.append(logits.detach().cpu().numpy())
            probs.append(F.softmax(logits, 1).detach().cpu().numpy().tolist())
            
        else:
            preds[0] = np.append(
                preds[0], logits.detach().cpu().numpy(), axis=0)
            probs.append(F.softmax(logits, 1).detach().cpu().numpy().tolist())
            #print(len(probs))
            

    eval_loss = eval_loss / nb_eval_steps
    preds = np.argmax(preds[0], axis=1)
    for i in range(len(probs)):
        probs_total += probs[i]
    
    for i in range(len(preds)):
        final_probs.append(probs_total[i][preds[i]])
    
#     for i in range(len(preds)):
#         print(round(final_probs[i], 3), preds[i])
    result = compute_f1(preds, eval_label_ids.numpy())
    result['accuracy'] = simple_accuracy(preds, eval_label_ids.numpy())
    result['eval_loss'] = eval_loss
    # if verbose:
    #     logger.info("***** Eval results *****")
    #     for key in sorted(result.keys()):
    #         logger.info("  %s = %s", key, str(result[key]))
    return preds, result, final_probs


def main(path):
    model = "/home/jupyter/Spanbert/output" 
    no_cuda = False
    do_eval = True
    eval_test = True
    data_dir = path
    eval_batch_size = 32
    learning_rate = 2e-5
    max_seq_length = 128
    output_dir = "/home/jupyter/Spanbert/output"
    gradient_accumulation_steps = 1
    seed = 42
    negative_label = "no_relation"
    do_lower_case = False
    feature_mode = "ner"
    fp16 = True


    # device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    device = torch.device("cuda" if torch.cuda.is_available() and not no_cuda else "cpu")
    n_gpu = torch.cuda.device_count()

    # if args.gradient_accumulation_steps < 1:
    #     raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
    #                         args.gradient_accumulation_steps))
    if gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            gradient_accumulation_steps))
    # args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    # random.seed(args.seed)
    # np.random.seed(args.seed)
    # torch.manual_seed(args.seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    # if n_gpu > 0:
    #     torch.cuda.manual_seed_all(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(seed)

    # if not args.do_train and not args.do_eval:
    #     raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    # if not os.path.exists(args.output_dir):
    #     os.makedirs(args.output_dir)


    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    processor = DataProcessor()
    label_list = processor.get_labels(data_dir, negative_label)
    label2id = {label: i for i, label in enumerate(label_list)}
    id2label = {i: label for i, label in enumerate(label_list)}
    num_labels = len(label_list)
    tokenizer = BertTokenizer.from_pretrained(model, do_lower_case=do_lower_case)

    special_tokens = {}
    if do_eval:
        eval_examples = processor.get_dev_examples(data_dir)
        eval_features = convert_examples_to_features(
            eval_examples, label2id, max_seq_length, tokenizer, special_tokens,feature_mode)
        all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_id for f in eval_features], dtype=torch.long)
        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
        eval_dataloader = DataLoader(eval_data, batch_size=eval_batch_size)
        eval_label_ids = all_label_ids

    if do_eval:
        if eval_test:
            eval_examples = processor.get_test_examples(data_dir)
            eval_features = convert_examples_to_features(
                eval_examples, label2id, max_seq_length, tokenizer, special_tokens, feature_mode)
            # logger.info("***** Test *****")
            # logger.info("  Num examples = %d", len(eval_examples))
            # logger.info("  Batch size = %d", args.eval_batch_size)
            all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
            all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
            all_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)
            all_label_ids = torch.tensor([f.label_id for f in eval_features], dtype=torch.long)
            eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
            eval_dataloader = DataLoader(eval_data, batch_size=eval_batch_size)
            eval_label_ids = all_label_ids
            golds = [t.label for t in eval_examples]
        model = BertForSequenceClassification.from_pretrained(output_dir, num_labels=num_labels)
        if fp16:
            model.half()
        model.to(device)
        preds, result, probs= evaluate(model, device, eval_dataloader, eval_label_ids, num_labels)
        
        result_final = []
        result_output = []
        eachsentence_dict = {}
        entity_dict_noindex = {}
        entity_list = []
        entity_dict = {}
        relation_list = []
        relation_dict = {}
        sentence_list = []
        sentence_dict = {}
        last_sentence = ''
        
        c = 0
        entity_count = 0
        with open(os.path.join(output_dir, "scores.txt"), "w") as f:
            for ex, pred, prob, gold in zip(eval_examples, preds, probs, golds):
                count = 0
                entity1 = ''
                entity2 = ''               
                for entity in ex.sentence:
                    if ex.span1[0] == count:
                        entity1 = entity
                    elif count <= ex.span1[1]:
                        entity1 = entity1 + ' ' + entity
                    if ex.span2[0] == count:
                        entity2 = entity
                    elif count <= ex.span2[1]:
                        entity2 = entity2 + ' ' + entity
                    count += 1            
                
                if last_sentence != ex.sentence:
                    if c != 0:
                        #entity_list.append(entity_dict)
                        entity_list.append(entity_dict_noindex)
                        result_output.append(entity_list)
                        result_output.append(relation_list)
                        sentence_list.append(sentence_dict)
                        result_output.append(sentence_list)
                        result_final.append(result_output)
                        # print(result_output)
                    
                    last_sentence = ex.sentence
                    entity_list = []
                    entity_dict = {}
                    entity_dict_noindex = {}
                    relation_list = []
                    relation_dict = {}
                    sentence_list = []
                    sentence_dict = {}
                    result_output = []  
                    c += 1
                    
                if ex.ner1 in entity_dict:
                    if ex.span1 not in entity_dict[ex.ner1][1]:
                        entity_dict[ex.ner1][0].append(entity1)
                        entity_dict[ex.ner1][1].append(ex.span1)
                        entity_dict_noindex[ex.ner1].append(entity1)
                else:
                    entity_dict[ex.ner1] = [[entity1], [ex.span1]]
                    entity_dict_noindex[ex.ner1] = [entity1]       
                    
                if ex.ner2 in entity_dict:
                    if ex.span2 not in entity_dict[ex.ner2][1]:
                        entity_dict[ex.ner2][0].append(entity2)
                        entity_dict[ex.ner2][1].append(ex.span2)
                        entity_dict_noindex[ex.ner2].append(entity2)
                else:
                    entity_dict[ex.ner2] = [[entity2], [ex.span2]]
                    entity_dict_noindex[ex.ner2] = [entity2]
                
                
                if id2label[pred] != 'no_relation':
                    relation_dict['entity1'] = entity1
                    relation_dict['entity2'] = entity2
                    relation_dict['relation'] = id2label[pred]
                    relation_dict['score'] = prob
                    relation_list.append(relation_dict)
                    relation_dict = {}
                
                sentence_dict['sentence'] = (' '.join(ex.sentence)).strip()
                f.write("%s\t%s\t%0.3f\n" % (gold, id2label[pred], prob))
                
                entity_count += 1
            #entity_list.append(entity_dict)
            entity_list.append(entity_dict_noindex)
            result_output.append(entity_list)
            result_output.append(relation_list)
            sentence_list.append(sentence_dict)
            result_output.append(sentence_list)
            result_final.append(result_output)
            print(result_final)
            
        with open(os.path.join(output_dir, "predictions.json"), "w") as f:
            json.dump(result_final, f)

def vm():
    parser = argparse.ArgumentParser()

    # parser.add_argument("--model", default=None, type=str, required=True)
    parser.add_argument("--model", default= "output", type=str)
    # parser.add_argument("--data_dir", default=None, type=str, required=True,
    #                     help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--data_dir", default="tacred", type=str,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    # parser.add_argument("--output_dir", default=None, type=str, required=True,
    #                     help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument("--output_dir", default="output", type=str,
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument("--eval_per_epoch", default=10, type=int,
                        help="How many times it evaluates on dev set per epoch")
    parser.add_argument("--max_seq_length", default=128, type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--negative_label", default="no_relation", type=str)
    # parser.add_argument("--do_train", action='store_true', help="Whether to run training.")
    parser.add_argument("--do_train", default=False, help="Whether to run training.")
    parser.add_argument("--train_mode", type=str, default='random_sorted', choices=['random', 'sorted', 'random_sorted'])
    # parser.add_argument("--do_eval", action='store_true', help="Whether to run eval on the dev set.")
    parser.add_argument("--do_eval", default=True, help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case", action='store_true', help="Set this flag if you are using an uncased model.")
    # parser.add_argument("--eval_test", action="store_true", help="Whether to evaluate on final test set.")
    parser.add_argument("--eval_test", default=True, help="Whether to evaluate on final test set.")
    # parser.add_argument("eval_test", default=True, help="Whether to evaluate on final test set.")
    parser.add_argument("--feature_mode", type=str, default="ner", choices=["text", "ner", "text_ner", "ner_text"])
    # parser.add_argument("--train_batch_size", default=32, type=int,
                        # help="Total batch size for training.")
    parser.add_argument("--eval_batch_size", default=32, type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--eval_metric", default="f1", type=str)
    # parser.add_argument("--learning_rate", default=None, type=float,
    #                     help="The initial learning rate for Adam.")
    parser.add_argument("--learning_rate", default=2e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion", default=0.1, type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    # parser.add_argument("--no_cuda", action='store_true',
                        # help="Whether not to use CUDA when available")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale', type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")

    args = parser.parse_args()
    main(args)
