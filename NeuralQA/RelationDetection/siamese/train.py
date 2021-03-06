import os
import random

import math
import numpy as np
import time
import torch
import torch.nn as nn
from tqdm import tqdm
from torch.autograd import Variable as Var
import util
from args import get_args
from datasets import Dataset
from model import RelationDetection


def pull_batch(question_list, relation_list, label_list, batch_idx):
    batch_size = config.batch_size
    if (batch_idx + 1) * batch_size < len(question_list):
        question_list = question_list[batch_idx * batch_size:(batch_idx + 1) * batch_size]
        relation_list = relation_list[batch_idx * batch_size:(batch_idx + 1) * batch_size]
        label_list = label_list[batch_idx * batch_size:(batch_idx + 1) * batch_size]
    else:  # last batch
        question_list = question_list[batch_idx * batch_size:]
        relation_list = relation_list[batch_idx * batch_size:]
        label_list = label_list[batch_idx * batch_size:]
    return torch.LongTensor(question_list), torch.LongTensor(relation_list), torch.LongTensor(label_list)


start = time.time()
np.set_printoptions(threshold=np.nan)
# Set default configuration in : args.py
args = get_args()

# Set random seed for reproducibility
torch.manual_seed(args.seed)
np.random.seed(args.seed)
random.seed(args.seed)
torch.backends.cudnn.deterministic = True

if not args.cuda:
    args.gpu = -1
if torch.cuda.is_available() and args.cuda:
    print("Note: You are using GPU for training")
    torch.cuda.set_device(args.gpu)
    torch.cuda.manual_seed(args.seed)
if torch.cuda.is_available() and not args.cuda:
    print("Warning: You have Cuda but not use it. You are using CPU for training.")

print ("loading word embedding...")
word_dict, embedding = util.get_pretrained_word_vector(args.vector_file, (288694, 100))
print ("vocabulary size: %d" % len(word_dict))

print ("loading train data...")
train_path = "../../data/processed_simplequestions_dataset/train.txt"
valid_path = "../../data/processed_simplequestions_dataset/valid.txt"
test_path = "../../data/processed_simplequestions_dataset/test.txt"
x_u, x_r, y_train, max_ques, max_rela = util.load_data(args.index_relation, train_path, args.neg_size)
train_set = Dataset(x_u, x_r, y_train, max_ques, word_dict)  # todo
print (np.array(train_set.ques_idx).shape, np.array(train_set.rela_idx).shape, np.array(
    train_set.label).shape)

print ("loading dev data...")
x_u, x_r, y_valid, max_ques, max_rela = util.load_data(args.index_relation, valid_path, args.neg_size)
dev_set = Dataset(x_u, x_r, y_valid, max_ques, word_dict)
print (np.array(dev_set.ques_idx).shape, np.array(dev_set.rela_idx).shape, np.array(dev_set.label).shape)

# print ("loading test data...")
# x_u, x_r, y_test, max_ques, max_rela = util.load_data(args.index_relation, valid_path)
# dev_dataset = Dataset(x_u, x_r, y_test, max_ques, word_dict)
# print (np.array(dev_dataset.ques_idx).shape, np.array(dev_dataset.rela_idx).shape, np.array(dev_dataset.label).shape)

config = args
config.words_num = 288694 + 1
print("text vocabulary size:", config.words_num)

if args.dataset == 'RelationDetection':
    config.rel_label = args.neg_size + 1  # num of classes
    model = RelationDetection(config)
else:
    print("Error Dataset")
    exit()

model.embed.weight.data.copy_(torch.FloatTensor(embedding))

if args.cuda:
    model.cuda()
    print("Shift model to GPU")

print(config)
print("VOCAB num", config.words_num)
print("Train instance", len(y_train))
print("Dev instance", len(y_valid))
# print("Test instance", len(y_test))
print(model)

parameter = filter(lambda p: p.requires_grad, model.parameters())
optimizer = torch.optim.Adam(parameter, lr=args.lr, weight_decay=args.weight_decay)

criterion = nn.CrossEntropyLoss()  # nn.NLLLoss()
early_stop = False
best_dev_P = 0
iterations = 0
iters_not_improved = 0
num_train_iter = int(math.ceil(train_set.size * 1.0 / args.batch_size))
num_dev_iter = int(math.ceil(dev_set.size * 1.0 / config.batch_size))

num_dev_in_epoch = (len(y_train) // args.batch_size // args.dev_every) + 1
patience = args.patience * num_dev_in_epoch  # for early stopping
epoch = 0
start = time.time()
header = '  Time Epoch Iteration Progress    (%Epoch)   Loss   Dev/Loss     Accuracy  Dev/Accuracy'
dev_log_template = ' '.join(
    '{:>6.0f},{:>5.0f},{:>9.0f},{:>5.0f}/{:<5.0f} {:>7.0f}%,{:>8.6f},{:8.6f},{:12.4f},{:12.4f}'.split(','))
log_template = ' '.join('{:>6.0f},{:>5.0f},{:>9.0f},{:>5.0f}/{:<5.0f} {:>7.0f}%,{:>8.6f},{},{},{}'.split(','))
save_path = os.path.join(args.save_path, args.relation_detection_mode.lower())
os.makedirs(save_path, exist_ok=True)
print(header)

while epoch <= args.epochs:
    if early_stop:
        print("Early Stopping. Epoch: {}, Best Dev Acc: {}".format(epoch, best_dev_P))
        break
    epoch += 1
    n_correct, n_total = 0, 0

    for train_step in tqdm(range(num_train_iter),
                           desc='Training epoch ' + str(epoch) + ''):
        ques_batch, rela_batch, label_batch = pull_batch(train_set.ques_idx, train_set.rela_idx,
                                                         train_set.label, train_step)  # tensor

        # Batch size : (Sentence Length, Batch_size)
        iterations += 1
        model.train()
        optimizer.zero_grad()
        scores = model(ques_batch, rela_batch)
        if args.dataset == 'RelationDetection':
            # print(torch.max(scores, 1)[1])
            n_correct += (torch.max(scores, 1)[1].data == label_batch).sum()
            loss = criterion(scores, Var(label_batch))  # volatile=True
        else:
            print("Wrong Dataset")
            exit()

        n_total += args.batch_size
        loss.backward()
        optimizer.step()

        # evaluate performance on validation set periodically
        if iterations % args.dev_every == 0:
            model.eval()
            n_dev_correct = 0
            for dev_step in range(num_dev_iter):
                ques_batch, rela_batch, label_batch = pull_batch(dev_set.ques_idx, dev_set.rela_idx,
                                                                 dev_set.label, dev_step)
                dev_score = model(ques_batch, rela_batch)
                # target = Var(torch.LongTensor([int(label)]), volatile=True)
                if args.dataset == 'RelationDetection':
                    n_dev_correct += (torch.max(dev_score, 1)[1].data == label_batch).sum()
                    loss = criterion(dev_score, Var(label_batch, volatile=True))
                else:
                    print("Wrong Dataset")
                    exit()

            if args.dataset == 'RelationDetection':
                P = 1. * n_dev_correct / dev_set.size
                print("{} Precision: {:10.6f}%".format("Dev", 100. * P))
            else:
                print("Wrong dataset")
                exit()

            # update model
            if args.dataset == 'RelationDetection':
                if P > best_dev_P:
                    best_dev_P = P
                    iters_not_improved = 0
                    snapshot_path = os.path.join(save_path, args.specify_prefix + '_dssm_best_model_cpu.pt')
                    torch.save(model, snapshot_path)
                else:
                    iters_not_improved += 1
                    if iters_not_improved > patience:
                        early_stop = True
                        break
            else:
                print("Wrong dataset")
                exit()

        if iterations % args.log_every == 1:
            # print progress message
            print(log_template.format(time.time() - start,
                                      epoch, iterations, 1 + train_step, num_train_iter,
                                      100. * (1 + train_step) / num_train_iter, loss.data[0], ' ' * 8,
                                      100. * n_correct / n_total, ' ' * 12))

print('Time of train model: %f' % (time.time() - start))
# TypeError: unsupported operand type(s) for -: 'datetime.datetime' and 'float'
