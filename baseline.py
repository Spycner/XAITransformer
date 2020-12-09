import argparse
import datetime
import glob
import os
import random
import sys

import numpy as np
import torch
from sklearn.metrics import balanced_accuracy_score
from sklearn.model_selection import train_test_split
from tqdm import tqdm

try:
    from rtpt.rtpt import RTPT
except:
    sys.path.append('../rtpt')
    from rtpt import RTPT

from models import BaseNet
import data_loader

# Create RTPT object
rtpt = RTPT(name_initials='FF', experiment_name='Transformer_Prototype', max_iterations=100)
# Start the RTPT tracking
rtpt.start()

parser = argparse.ArgumentParser(description='Crazy Stuff')
parser.add_argument('-m', '--mode', default="normal", type=str,
                    help='What do you want to do? Select either normal, train, test,')
parser.add_argument('--lr', type=float, default=0.01,
                    help='Learning rate')
parser.add_argument('--cpu', action='store_true', default=False,
                    help='Whether to use cpu')
parser.add_argument('-e', '--num_epochs', default=100, type=int,
                    help='How many epochs?')
parser.add_argument('-bs', '--batch_size', default=128, type=int,
                    help='Batch size')
parser.add_argument('--val_epoch', default=10, type=int,
                    help='After how many epochs should the model be evaluated on the validation data?')
parser.add_argument('--data_dir', default='./data/rt-polarity',
                    help='Select data path')
parser.add_argument('--data_name', default='reviews', type=str, choices=['reviews', 'toxicity'],
                    help='Select data name')
parser.add_argument('--num_prototypes', default=10, type = int,
                    help='Total number of prototypes')
parser.add_argument('-l2','--lambda2', default=0.1, type=float,
                    help='Weight for prototype loss computation')
parser.add_argument('-l3','--lambda3', default=0.1, type=float,
                    help='Weight for prototype loss computation')
parser.add_argument('--num_classes', default=2, type=int,
                    help='How many classes are to be classified?')
parser.add_argument('--class_weights', default=[0.5,0.5],
                    help='Class weight for cross entropy loss')
parser.add_argument('-g','--gpu', type=int, default=0, help='GPU device number, -1  means CPU.')
parser.add_argument('--one_shot', type=bool, default=False,
                    help='Whether to use one-shot learning or not (i.e. only a few training examples)')
parser.add_argument('--trans_type', type=str, default='PCA', choices=['PCA', 'TSNE'],
                    help='Which transformation should be used to visualize the prototypes')
parser.add_argument('--discard', type=bool, default=False, help='Whether edge cases in the middle between completely '
                                                                'toxic (1) and not toxic at all (0) shall be omitted')

def get_batches(embedding, labels, batch_size=128):
    def divide_chunks(l, n):
        for i in range(0, len(l), n):
            yield l[i:i + n]
    tmp = list(zip(embedding, labels))
    random.shuffle(tmp)
    embedding, labels = zip(*tmp)
    embedding_batches = list(divide_chunks(torch.stack(embedding), batch_size))
    label_batches = list(divide_chunks(torch.stack(labels), batch_size))
    return embedding_batches, label_batches

def save_checkpoint(save_dir, state, time_stmp, best, filename='best_model.pth.tar'):
    if best:
        save_path_checkpoint = os.path.join(save_dir, time_stmp, filename)
        os.makedirs(os.path.dirname(save_path_checkpoint), exist_ok=True)
        torch.save(state, save_path_checkpoint)


def train(args, text_train, labels_train, text_val, labels_val):
    save_dir = "./experiments/train_results/"
    time_stmp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    model = BaseNet(args)
    print("Running on gpu {}".format(args.gpu))
    model.cuda(args.gpu)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    ce_crit = torch.nn.CrossEntropyLoss(weight=torch.tensor(args.class_weights).float().cuda(args.gpu))

    model.train()
    embedding = model.compute_embedding(text_train, args.gpu)
    embedding_val = model.compute_embedding(text_val, args.gpu)
    num_epochs = args.num_epochs
    print("\nStarting training for {} epochs\n".format(num_epochs))
    best_acc = 0
    for epoch in tqdm(range(num_epochs)):
        all_preds = []
        all_labels = []
        losses_per_batch = []
        emb_batches, label_batches = get_batches(embedding, labels_train, args.batch_size)

        # Update the RTPT
        rtpt.step(subtitle=f"epoch={epoch+1}")

        for i,(emb_batch, label_batch) in enumerate(zip(emb_batches, label_batches)):
            optimizer.zero_grad()

            outputs = model.forward(emb_batch)
            predicted_label = outputs

            # compute individual losses and backward step
            loss = ce_crit(predicted_label, label_batch)
            _, predicted = torch.max(predicted_label.data, 1)
            all_preds += predicted.cpu().numpy().tolist()
            all_labels += label_batch.cpu().numpy().tolist()

            loss.backward()
            optimizer.step()
            # store losses
            losses_per_batch.append(float(loss))

        mean_loss = np.mean(losses_per_batch)

        acc = balanced_accuracy_score(all_labels, all_preds)
        print("Epoch {}, mean loss {:.4f}, train acc {:.4f}".format(epoch+1,
                                                        mean_loss,
                                                        100 * acc))

        if (epoch + 1) % args.val_epoch == 0 or epoch + 1 == num_epochs:
            model.eval()
            with torch.no_grad():
                outputs = model.forward(embedding_val)
                predicted_label = outputs

                # compute individual losses and backward step
                loss = ce_crit(predicted_label, labels_val)

                _, predicted_val = torch.max(predicted_label.data, 1)
                acc_val = balanced_accuracy_score(labels_val.cpu().numpy(), predicted_val.cpu().numpy())
                print("Validation: mean loss {:.4f}, acc_val {:.4f}".format(loss, 100 * acc_val))

            save_checkpoint(save_dir, {
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'hyper_params': args,
                'acc_val': acc_val,
            }, time_stmp, best=acc_val >= best_acc)
            if acc_val >= best_acc:
                best_acc = acc_val


def test(args, text_test, labels_test):
    load_path = "./experiments/train_results/*"
    model_paths = glob.glob(os.path.join(load_path, 'best_model.pth.tar'))
    model_paths.sort()
    model_path = model_paths[-1]
    print("\nStarting evaluation, loading model:", model_path)
    # test_dir = "./experiments/test_results/"

    model = BaseNet(args)
    checkpoint = torch.load(model_path)
    model.load_state_dict(checkpoint['state_dict'])
    model.cuda(args.gpu)
    model.eval()
    ce_crit = torch.nn.CrossEntropyLoss(weight=torch.tensor(args.class_weights).float().cuda(args.gpu))

    embedding_test = model.compute_embedding(text_test, args.gpu)

    with torch.no_grad():
        outputs = model.forward(embedding_test)
        predicted_label = outputs

        # compute individual losses and backward step
        loss = ce_crit(predicted_label, labels_test)

        _, predicted = torch.max(predicted_label.data, 1)
        acc_test = balanced_accuracy_score(labels_test.cpu().numpy(), predicted.cpu().numpy())
        print(f"test evaluation on best model: loss {loss:.4f}, acc_test {100 * acc_test:.4f}")


if __name__ == '__main__':
    torch.manual_seed(0)
    np.random.seed(0)
    args = parser.parse_args()

    if args.gpu >= 0:
        torch.cuda.set_device(args.gpu)

    text, labels = data_loader.load_data(args)
    # split data, and split test set again to get validation and test set
    text_train, text_test, labels_train, labels_test = train_test_split(text, labels, test_size=0.3, stratify=labels)
    text_val, text_test, labels_val, labels_test = train_test_split(text_test, labels_test, test_size=0.5,
                                                                         stratify=labels_test)
    labels_train = torch.LongTensor(labels_train).cuda(args.gpu)
    labels_val = torch.LongTensor(labels_val).cuda(args.gpu)
    labels_test = torch.LongTensor(labels_test).cuda(args.gpu)
    # set class weights for balanced cross entropy computation
    balance = labels.count(0) / len(labels)
    args.class_weights = [1-balance, balance]

    if args.one_shot:
        idx = random.sample(range(len(text_train)),100)
        text_train = list(text_train[i] for i in idx)
        labels_train = torch.LongTensor([labels_train[i] for i in idx]).cuda(args.gpu)

    if args.mode == 'normal':
        train(args, text_train, labels_train, text_val, labels_val)
        test(args, text_test, labels_test)
    elif args.mode == 'train':
        train(args, text_train, labels_train, text_val, labels_val)
    elif args.mode == 'test':
        test(args, text_test, labels_test)