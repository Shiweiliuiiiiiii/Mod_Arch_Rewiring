# Trainer file

import argparse
import torch
import torch.nn as nn
import random
import numpy as np
import os
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
import math
import copy
from data import data_v1, data_v2
from model import Monolithic, Modular, GT_Modular

parser = argparse.ArgumentParser(description='Rule MLP')
parser.add_argument('--gt-rules', type=int, default=2)
parser.add_argument('--data-seed', type=int, default=0)
parser.add_argument('--batch-size', type=int, default=256)
parser.add_argument('--lr', type=float, default=0.0001)
parser.add_argument('--iterations', type=int, default=100000)

parser.add_argument('--encoder-dim', type=int, default=32)
parser.add_argument('--dim', type=int, default=64)
parser.add_argument('--model', type=str, default='Monolithic', choices=('Monolithic', 'Modular', 'GT_Modular'))
parser.add_argument('--num-rules', type=int, default=2)
parser.add_argument('--joint', action='store_true', default=False)
parser.add_argument('--op', action='store_true', default=False)

parser.add_argument('--scheduler', action='store_true', default=False)
parser.add_argument('--seed', type=int, default=0)
args = parser.parse_args()

sns.color_palette("dark", as_cmap=True)
sns.set(style="darkgrid")

def get_model_params(model):
    params = {}
    for name in model.state_dict():
        params[name] = copy.deepcopy(model.state_dict()[name])
    return params

def set_model_params(model, model_parameters):
    model.load_state_dict(model_parameters)

def get_prob_online(model, data_call, args):
    model.eval()
    prob = np.zeros([args.gt_rules, args.gt_rules])
    total = np.zeros([args.gt_rules, 1])

    with torch.no_grad():
        for _ in range(1000):
            data, label = data_call(1000, args.gt_rules, args.data_seed)
            data = torch.Tensor(data).to(device)
            label = torch.Tensor(label).to(device)
            out, score = model(data)

            op = data[:, 2:].detach().cpu().numpy()
            score = score.detach().cpu().numpy()

            for gt in range(args.gt_rules):
                idx = op[:, gt] == 1
                idx = np.reshape(idx, [-1])
                total[gt, 0] += np.sum(idx)
                prob[gt] += np.sum(score[idx,:], axis=0)

    prob /= total
    prob *= (1./ args.gt_rules)

    return prob

def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)

set_seed(args.seed)

config = vars(args)
device = torch.device('cuda')
op_dim = args.gt_rules

if args.op:
    extras = f'_operation-only_'
elif args.joint:
    extras = f'_joint_'
else:
    extras = '_'

if args.scheduler:
    ext='_scheduler'
else:
    ext=''

name = f'Logs{ext}/Data-Seed_{args.data_seed}/GT_Rules_{args.gt_rules}/{args.model}{extras}{args.encoder_dim}_{args.dim}_{args.num_rules}_{args.seed}'

if not os.path.exists(name):
    os.makedirs(name)
else:
    if os.path.exists(f'{name}/loss.png'):
        print('Model Exists')
        exit()

if args.model == 'Monolithic':
    model = Monolithic(op_dim, args.encoder_dim, args.dim).to(device)
elif args.model == 'Modular':
    model = Modular(op_dim, args.encoder_dim, args.dim, args.num_rules, args.op, args.joint).to(device)
elif args.model == 'GT_Modular':
    model = GT_Modular(op_dim, args.encoder_dim, args.dim, args.gt_rules).to(device)
else:
    print("Model Not Implemented")
    exit()

gt_ticks = [f'Ground Truth Rule {i}' for i in range(1, args.gt_rules+1)]
data_call = data_v2

num_params = sum(p.numel() for p in model.parameters())
print(model)
print(f"Number of Parameters: {num_params}")
with open(os.path.join(name, 'log.txt'), 'w') as f:
    f.write(f"Number of Parameters: {num_params}\n")

optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
if args.scheduler:
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[5,10,15], gamma=0.5)
criterion = nn.BCEWithLogitsLoss()

df = pd.DataFrame(columns=["Iterations", "Loss", "Accuracy"])

def eval_step(ood=False, n_evals=10):
    model.eval()
    total_loss = 0.
    total_acc = 0.

    with torch.no_grad():
        for _ in range(n_evals):
            data, label = data_call(args.batch_size, args.gt_rules, args.data_seed, ood)

            data = torch.Tensor(data).to(device)
            label = torch.Tensor(label).to(device)

            out, score = model(data)

            loss = criterion(out, label)
            acc = torch.eq(out >= 0., label).double().mean()

            total_loss += loss.item()
            total_acc += acc.item()

        return total_loss / float(n_evals), total_acc * 100. / float(n_evals)


def train_step():
    model.train()
    model.zero_grad()

    data, label = data_call(args.batch_size, args.gt_rules, args.data_seed)

    data = torch.Tensor(data).to(device)
    label = torch.Tensor(label).to(device)

    out, score = model(data)

    loss = criterion(out, label)
    acc = torch.eq(out >= 0., label).double().mean()

    loss.backward()
    optimizer.step()

    return loss.item(), acc.item() * 100.

eval_loss, eval_acc = eval_step()
eval_ood_loss, eval_ood_acc = eval_step(True)

log = f'Iteration: 0 | Eval OoD Loss: {eval_ood_loss} - Eval OoD Acc: {eval_ood_acc} | \n' \
      f'Iteration: 0 | Train Loss: {eval_loss} - Train Acc: {eval_acc} | \n' \
      f'Iteration: 0 | Eval Loss: {eval_loss} - Eval Acc: {eval_acc} | \n'

print(log)

with open(os.path.join(name, 'log.txt'), 'a') as f:
    f.write(log)

best_val = 0.
df.loc[-1] = [0, eval_loss, eval_acc]
df.index = df.index + 1

for i in range(1, args.iterations+1):
    if i % 5000 == 0:
        eval_loss, eval_acc = eval_step()
        val_loss, val_acc = eval_step()

        df.loc[-1] = [i, eval_loss, eval_acc]
        df.index = df.index + 1

    train_loss, train_acc = train_step()


    if i % 1000 == 0:


        prob = get_prob_online(model, data_call, args)
        p = np.sum(prob, axis=0)
        # reinitialize/rewire weights of collapsed experts
        bound = math.sqrt(1.0/ (model.encoder_dim * 3))

        # model_para = get_model_params(model)
        #
        # for name1 in model_para:
        #     if 'MLP.2.w' in name1:
        #         new_weights = torch.zeros_like(model_para[name1][p < 1 / args.num_rules]).to(model_para[name1].device)
        #         nn.init.uniform_(new_weights, -bound, bound)
        #         print([p < 1 / args.num_rules])
        #         print(f'before {model_para[name1][p < 1 / args.num_rules]}')
        #         model_para[name1][p < 1 / args.num_rules].data = new_weights.data
        #         print(f'after {model_para[name1][p < 1 / args.num_rules]}')
        #
        # set_model_params(model, model_para)

        with torch.no_grad():
            for name1, para in model.named_parameters():
                if 'MLP.2.w' in name1:
                    # size of expert parameters - (num_modelars, dims/num_modelars, dims+1)
                    new_weights = torch.zeros_like(para[p < 1/args.num_rules]).to(para.device)
                    nn.init.uniform_(new_weights, -bound, bound)
                    print(new_weights)
                    print(f'before {para[p < 1/args.num_rules]}')
                    para[p < 1/args.num_rules].copy(new_weights)
                    print(f'after {para[p < 1/args.num_rules]}')


        # metrics_online(prob, args.num_rules)

    if i % 5000 == 0:
        if args.scheduler:
            scheduler.step()

        log = f'Iteration: {i} | Train Loss: {train_loss} - Train Acc: {train_acc} | \n' \
              f'Iteration: {i} | Eval Loss: {eval_loss} - Eval Acc: {eval_acc} | \n'
        print(log)

        if val_acc > best_val:
            best_val = val_acc
            torch.save(model.state_dict(), os.path.join(name, 'model_best.pt'))

        torch.save(model.state_dict(), os.path.join(name, 'model_last.pt'))

        with open(os.path.join(name, 'log.txt'), 'a') as f:
            f.write(log)

sns.lineplot(data=df, x="Iterations", y="Loss")
plt.savefig(os.path.join(name, 'loss.png'), bbox_inches='tight')
plt.close()

sns.lineplot(data=df, x="Iterations", y="Accuracy")
plt.savefig(os.path.join(name, 'acc.png'), bbox_inches='tight')
plt.close()