import argparse
import datetime
import glob
import sys
import os
import random

import torch
import numpy as np
from sklearn.metrics import balanced_accuracy_score
from sklearn.model_selection import train_test_split
from tqdm import tqdm

try:
    from rtpt.rtpt import RTPT
except:
    sys.path.append('../rtpt')
    from rtpt import RTPT

from models import ProtoPNetConv, ProtoPNetDist, ProtoNet
import utils

# Create RTPT object
rtpt = RTPT(name_initials='FF', experiment_name='Transformer_Prototype', max_iterations=100)
# Start the RTPT tracking
rtpt.start()

parser = argparse.ArgumentParser(description='Crazy Stuff')
parser.add_argument('-m', '--mode', default="both", type=str,
                    help='What do you want to do? Select either only train, test or both')
parser.add_argument('--lr', type=float, default=0.001,
                    help='Learning rate')
parser.add_argument('-e', '--num_epochs', default=100, type=int,
                    help='How many epochs?')
parser.add_argument('-bs', '--batch_size', default=256, type=int,
                    help='Batch size')
parser.add_argument('--val_epoch', default=10, type=int,
                    help='After how many epochs should the model be evaluated on the validation data?')
parser.add_argument('--data_dir', default='./data/rt-polarity',
                    help='Select data path')
parser.add_argument('--data_name', default='reviews', type=str, choices=['reviews', 'toxicity'],
                    help='Select data name')
parser.add_argument('--num_prototypes', default=10, type=int,
                    help='Total number of prototypes')
parser.add_argument('-l1','--lambda1', default=0.1, type=float,
                    help='Weight for prototype loss computation')
parser.add_argument('-l2','--lambda2', default=0.1, type=float,
                    help='Weight for prototype loss computation')
parser.add_argument('--num_classes', default=2, type=int,
                    help='How many classes are to be classified?')
parser.add_argument('--class_weights', default=[0.5,0.5],
                    help='Class weight for cross entropy loss')
parser.add_argument('-g','--gpu', type=int, default=0, nargs='+', help='GPU device number, -1  means CPU.')
parser.add_argument('--one_shot', type=bool, default=False,
                    help='Whether to use one-shot learning or not (i.e. only a few training examples)')
parser.add_argument('--trans_type', type=str, default='PCA', choices=['PCA', 'TSNE'],
                    help='Which transformation should be used to visualize the prototypes')
parser.add_argument('--discard', type=bool, default=False, help='Whether edge cases in the middle between completely '
                                                                'toxic (1) and not toxic at all (0) shall be omitted')
parser.add_argument('--proto_size', type=int, default=4,
                    help='Define how many words should be used to define a prototype')
parser.add_argument('--model', type=str, default='dist', choices=['p_conv','p_dist','dist'],
                    help='Define which model to use')

def train(args, text_train, labels_train, text_val, labels_val):
    save_dir = "./experiments/train_results/"
    time_stmp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    model = []
    if args.model=='p_dist':
        model = ProtoPNetDist(args)
    elif args.model=='p_conv':
        model = ProtoPNetConv(args)
    elif args.model=='dist':
        model = ProtoNet(args)

    print("Running on gpu {}".format(args.gpu))
    model = torch.nn.DataParallel(model, device_ids=args.gpu)
    # model = torch.nn.parallel.DistributedDataParallel(model, device_ids=args.gpu)
    model.to(f'cuda:{args.gpu[0]}')
    embedding = model.module.compute_embedding(text_train, args.gpu[0])
    embedding_val = model.module.compute_embedding(text_val, args.gpu[0])

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    ce_crit = torch.nn.CrossEntropyLoss(weight=torch.tensor(args.class_weights).float().cuda(args.gpu[0]))
    interp_criteria = utils.ProtoLoss()

    model.train()
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
        emb_batches, label_batches = utils.get_batches(embedding, labels_train, args.batch_size)

        # Update the RTPT
        rtpt.step(subtitle=f"epoch={epoch+1}")

        for i,(emb_batch, label_batch) in enumerate(zip(emb_batches, label_batches)):
            emb_batch = emb_batch.to(f'cuda:{args.gpu[0]}')
            label_batch = label_batch.to(f'cuda:{args.gpu[0]}')

            optimizer.zero_grad()
            prototype_distances, feature_vector_distances, predicted_label = model.forward(emb_batch)

            # compute individual losses and backward step
            ce_loss = ce_crit(predicted_label, label_batch)
            r1_loss, r2_loss = interp_criteria(feature_vector_distances, prototype_distances)
            loss = ce_loss + \
                   args.lambda1 * r1_loss + \
                   args.lambda2 * r2_loss

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
                embedding_val = embedding_val.to(f'cuda:{args.gpu[0]}')
                outputs = model.forward(embedding_val)
                prototype_distances, feature_vector_distances, predicted_label = outputs

                # compute individual losses and backward step
                ce_loss = ce_crit(predicted_label, labels_val)
                r1_loss, r2_loss = interp_criteria(feature_vector_distances, prototype_distances)
                loss_val = ce_loss + \
                       args.lambda1 * r1_loss + \
                       args.lambda2 * r2_loss

                _, predicted_val = torch.max(predicted_label.data, 1)
                acc_val = balanced_accuracy_score(labels_val.cpu().numpy(), predicted_val.cpu().numpy())
                print("Validation: mean loss {:.4f}, acc_val {:.4f}".format(loss_val, 100 * acc_val))

            utils.save_checkpoint(save_dir, {
                'epoch': epoch + 1,
                'state_dict': model.module.state_dict(),
                'optimizer': optimizer.state_dict(),
                'hyper_params': args,
                'acc_val': acc_val,
            }, time_stmp, best=acc_val >= best_acc)
            if acc_val >= best_acc:
                best_acc = acc_val


def test(args, text_train, labels_train, text_test, labels_test):
    load_path = "./experiments/train_results/*"
    model_paths = glob.glob(os.path.join(load_path, 'best_model.pth.tar'))
    model_paths.sort()
    model_path = model_paths[-1]
    print("\nStarting evaluation, loading model:", model_path)
    # test_dir = "./experiments/test_results/"

    model = []
    if args.model=='p_dist':
        model = ProtoPNetDist(args)
    elif args.model=='p_conv':
        model = ProtoPNetConv(args)
    elif args.model=='dist':
        model = ProtoNet(args)

    checkpoint = torch.load(model_path)
    model.load_state_dict(checkpoint['state_dict'])
    model.to(f'cuda:{args.gpu[0]}')
    model.eval()
    ce_crit = torch.nn.CrossEntropyLoss(weight=torch.tensor(args.class_weights).float().to(f'cuda:{args.gpu[0]}'))
    interp_criteria = utils.ProtoLoss()

    embedding = model.compute_embedding(text_train, args.gpu[0])
    embedding_test = model.compute_embedding(text_test, args.gpu[0])

    with torch.no_grad():
        embedding_test = embedding_test.to(f'cuda:{args.gpu[0]}')
        outputs = model.forward(embedding_test)
        prototype_distances, feature_vector_distances, predicted_label = outputs

        # compute individual losses and backward step
        ce_loss = ce_crit(predicted_label, labels_test)
        r1_loss, r2_loss = interp_criteria(feature_vector_distances, prototype_distances)
        loss = ce_loss + \
               args.lambda1 * r1_loss + \
               args.lambda2 * r2_loss

        _, predicted = torch.max(predicted_label.data, 1)
        acc_test = balanced_accuracy_score(labels_test.cpu().numpy(), predicted.cpu().numpy())
        print(f"test evaluation on best model: loss {loss:.4f}, acc_test {100 * acc_test:.4f}")

        # "convert" prototype embedding to text (take text of nearest training sample)
        proto_texts = []
        if args.model.startswith("p_"):
            nearest_sent_ids, nearest_word_ids = model.nearest_neighbors(prototype_distances)
            text_tknzd = model.tokenizer(text_train, truncation=True, padding=True).input_ids
            for (s_index, w_index) in zip(nearest_sent_ids, nearest_word_ids):
                token2text = model.tokenizer.decode(np.array(text_tknzd[s_index])[w_index].tolist())
                proto_texts.append([s_index, token2text])
        else:
            nearest_ids = model.nearest_neighbors(prototype_distances)
            proto_texts = [[index, text_train[index]] for index in nearest_ids]

        weights = model.get_proto_weights()
        save_path = os.path.join(os.path.dirname(model_path), "prototypes.txt")
        #os.makedirs(os.path.dirname(save_path), exist_ok=True)
        txt_file = open(save_path, "w+")
        for line in proto_texts:
            txt_file.write(str(line))
            txt_file.write("\n")
        for line in weights:
            txt_file.write(str(line))
            txt_file.write("\n")
        txt_file.close()

        # get prototypes
        prototypes = model.get_protos()

        embedding = embedding.cpu().numpy()
        prototypes = prototypes.cpu().numpy()
        labels_train = labels_train.cpu().numpy()
        utils.visualize_protos(embedding, labels_train, prototypes, n_components=2, trans_type=args.trans_type, save_path=os.path.dirname(save_path))
        # utils.visualize_protos(embedding, labels_train, prototypes, n_components=3, trans_type=args.trans_type, save_path=os.path.dirname(save_path))


if __name__ == '__main__':
    torch.manual_seed(0)
    np.random.seed(0)
    args = parser.parse_args()

    text, labels = utils.load_data(args)
    # split data, and split test set again to get validation and test set
    text_train, text_test, labels_train, labels_test = train_test_split(text, labels, test_size=0.3, stratify=labels)
    text_val, text_test, labels_val, labels_test = train_test_split(text_test, labels_test, test_size=0.5,
                                                                         stratify=labels_test)
    # num_examples must be divisible by num_gpus for data parallelization
    num_gpus = len(args.gpu)
    if num_gpus > 1:
        overhead = len(labels_val) % num_gpus
        labels_val = labels_val[:len(labels_val) - overhead]
        text_val = text_val[:len(text_val) - overhead]
        overhead = len(labels_test) % num_gpus
        labels_test = labels_test[:len(labels_test) - overhead]
        text_test = text_test[:len(text_test) - overhead]

    labels_train = torch.LongTensor(labels_train)#.to(f'cuda:{args.gpu[0]}')
    labels_val = torch.LongTensor(labels_val)#.to(f'cuda:{args.gpu[0]}')
    labels_test = torch.LongTensor(labels_test)#.to(f'cuda:{args.gpu[0]}')

    # set class weights for balanced cross entropy computation
    balance = labels.count(0) / len(labels)
    args.class_weights = [1-balance, balance]

    if args.one_shot:
        idx = random.sample(range(len(text_train)), 100)
        text_train = list(text_train[i] for i in idx)
        labels_train = torch.LongTensor([labels_train[i] for i in idx])#.to(f'cuda:{args.gpu[0]}')

    if args.mode == 'both':
        train(args, text_train, labels_train, text_val, labels_val)
        torch.cuda.empty_cache() # is required since BERT encoding is only possible on 1 GPU (memory limitation)
        test(args, text_train, labels_train, text_test, labels_test)
    elif args.mode == 'train':
        train(args, text_train, labels_train, text_val, labels_val)
    elif args.mode == 'test':
        test(args, text_train, labels_train, text_test, labels_test)
