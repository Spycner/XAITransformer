import argparse
import random
import sys
from itertools import product

import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from sklearn.metrics import balanced_accuracy_score
from sklearn.model_selection import train_test_split

try:
    from rtpt.rtpt import RTPT
except:
    sys.path.append('../rtpt')
    from rtpt import RTPT

from models import ProtoNetNLP
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

class ProtoLoss:
    def __init__(self):
        pass

    def __call__(self, feature_vector_distances, prototype_distances):
        """
        Computes the interpretability losses (R1 and R2 from the paper (Li et al. 2018)) for the prototype nets.

        :param feature_vector_distances: tensor of size [n_prototypes, n_batches], distance between the data encodings
                                          of the autoencoder and the prototypes
        :param prototype_distances: tensor of size [n_batches, n_prototypes], distance between the prototypes and
                                    data encodings of the autoencoder
        :return:
        """
        #assert prototype_distances.shape == feature_vector_distances.T.shape
        r1_loss = torch.mean(torch.min(feature_vector_distances, dim=1)[0])
        r2_loss = torch.mean(torch.min(prototype_distances, dim=1)[0])
        return r1_loss, r2_loss

def train(args, text_train, labels_train, text_val, labels_val):
    global acc, mean_loss
    parameters = dict(
        lr=[0.01, 0.001],
        batch_size=[128,256],
        lambda2=[0.1,0.4,0.9],
        lambda3=[0.1,0.4,0.9]
    )
    param_values = [v for v in parameters.values()]
    print(param_values)

    for lr, batch_size, lambda2, lambda3 in product(*param_values):
        print(lr, batch_size, lambda2, lambda3)

    model = ProtoNetNLP(args)
    print("Running on gpu {}".format(args.gpu))
    model.cuda(args.gpu)

    for run_id, (lr, batch_size, lambda2, lambda3) in enumerate(product(*param_values)):
        comment = f' batch_size = {batch_size} lr = {lr} lambda2 = {lambda2} lambda3 ={lambda3}'
        tb = SummaryWriter(comment=comment)

        optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
        ce_crit = torch.nn.CrossEntropyLoss(weight=torch.tensor(args.class_weights).float().cuda(args.gpu))
        interp_criteria = ProtoLoss()

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
            ce_loss_per_batch = []
            r1_loss_per_batch = []
            r2_loss_per_batch = []
            emb_batches, label_batches = get_batches(embedding, labels_train, args.batch_size)

            # Update the RTPT
            rtpt.step(subtitle=f"epoch={epoch+1}")

            for i,(emb_batch, label_batch) in enumerate(zip(emb_batches, label_batches)):
                optimizer.zero_grad()

                outputs = model.forward(emb_batch)
                prototype_distances, feature_vector_distances, predicted_label = outputs

                # compute individual losses and backward step
                ce_loss = ce_crit(predicted_label, label_batch)
                r1_loss, r2_loss = interp_criteria(feature_vector_distances, prototype_distances)
                loss = ce_loss + \
                       args.lambda2 * r1_loss + \
                       args.lambda3 * r2_loss

                _, predicted = torch.max(predicted_label.data, 1)
                all_preds += predicted.cpu().numpy().tolist()
                all_labels += label_batch.cpu().numpy().tolist()

                loss.backward()
                optimizer.step()
                # store losses
                losses_per_batch.append(float(loss))
                ce_loss_per_batch.append(float(ce_loss))
                r1_loss_per_batch.append(float(r1_loss))
                r2_loss_per_batch.append(float(r2_loss))

            mean_loss = np.mean(losses_per_batch)
            ce_mean_loss = np.mean(ce_loss_per_batch)
            r1_mean_loss = np.mean(r1_loss_per_batch)
            r2_mean_loss = np.mean(r2_loss_per_batch)
            acc = balanced_accuracy_score(all_labels, all_preds)
            print("Epoch {}, mean loss {:.4f}, ce loss {:.4f}, r1 loss {:.4f}, "
                  "r2 loss {:.4f}, train acc {:.4f}".format(epoch+1,
                                                            mean_loss,
                                                            ce_mean_loss,
                                                            r1_mean_loss,
                                                            r2_mean_loss,
                                                            100 * acc))

            if (epoch + 1) % args.val_epoch == 0 or epoch + 1 == num_epochs:
                model.eval()
                with torch.no_grad():
                    outputs = model.forward(embedding_val)
                    prototype_distances, feature_vector_distances, predicted_label = outputs

                    # compute individual losses and backward step
                    ce_loss = ce_crit(predicted_label, labels_val)
                    r1_loss, r2_loss = interp_criteria(feature_vector_distances, prototype_distances)
                    loss_val = ce_loss + \
                           args.lambda2 * r1_loss + \
                           args.lambda3 * r2_loss

                    _, predicted_val = torch.max(predicted_label.data, 1)
                    acc_val = balanced_accuracy_score(labels_val.cpu().numpy(), predicted_val.cpu().numpy())
                    print("Validation: mean loss {:.4f}, acc_val {:.4f}".format(loss_val, 100 * acc_val))

                    tb.add_scalar("Loss_val", loss_val, epoch+1)
                    tb.add_scalar("Accuracy_val", acc_val, epoch+1)
                if acc_val >= best_acc:
                    best_acc = acc_val

        tb.add_hparams({"lr": lr, "bsize": batch_size, "lambda2": lambda2, "lambda3": lambda3},
                        dict(best_accuracy=best_acc))

    tb.close()


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

    train(args, text_train, labels_train, text_val, labels_val)