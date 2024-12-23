import sys
import os
import shutil
import numpy as np
import pandas as pd
import time
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
from torchsummary import summary
import argparse
import logging
from utils import *
from MGGLTN import MGGLTN


def print_model(model):
    param_count = 0
    logger.info('Trainable parameter list:')
    for name, param in model.named_parameters():
        if param.requires_grad:
            print(name, param.shape, param.numel())
            param_count += param.numel()
    logger.info(f'In total: {param_count} trainable parameters.')
    return


def get_model(mode):
    model = MGGLTN(num_nodes=args.num_nodes, input_dim=args.input_dim, output_dim=args.output_dim, seq_len=args.seq_len,
                    out_len=args.out_len, b_layers = args.b_layers, top_k = args.top_k,
                    d_model = args.d_model, d_ff = args.d_ff, num_kernels = args.num_kernels, mem_num=args.mem_num, mem_dim=args.mem_dim,
                    cheb_k=args.max_diffusion_step, cl_decay_steps=args.cl_decay_steps).to(device)
    if mode == 'train':
        summary(model, [(args.seq_len, args.num_nodes, args.input_dim), (args.out_len, args.num_nodes, args.output_dim)], batch_dim=0, device=device)
        print_params(model)
        for p in model.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
            else:
                nn.init.uniform_(p)
    return model


def prepare_x_y(x, y):
    """
    :param x: shape (batch_size, seq_len, num_sensor, input_dim)
    :param y: shape (batch_size, out_len, num_sensor, input_dim)
    :return1: x shape (seq_len, batch_size, num_sensor, input_dim)
              y shape (out_len, batch_size, num_sensor, input_dim)
    :return2: x: shape (seq_len, batch_size, num_sensor * input_dim)
              y: shape (out_len, batch_size, num_sensor * output_dim)
    """
    x0 = x[..., :args.input_dim]
    y0 = y[..., :args.output_dim]
    y1 = y[..., args.output_dim:]
    x0 = torch.from_numpy(x0).float()
    y0 = torch.from_numpy(y0).float()
    y1 = torch.from_numpy(y1).float()
    return x0.to(device), y0.to(device), y1.to(device) # x, y, y_cov


def evaluate(model, mode):
    with torch.no_grad():
        model = model.eval()
        data_iter = data[f'{mode}_loader'].get_iterator()
        losses, ys_true, ys_pred = [], [], []
        for x, y in data_iter:
            x, y, ycov = prepare_x_y(x, y)
            output, h_att, query, pos, neg = model(x, ycov)
            y_pred = scaler.inverse_transform(output)
            y_true = scaler.inverse_transform(y)
            loss1 = masked_mae_loss(y_pred, y_true) # masked_mae_loss(y_pred, y_true)
            separate_loss = nn.TripletMarginLoss(margin=1.0)
            compact_loss = nn.MSELoss()
            loss2 = separate_loss(query, pos.detach(), neg.detach())
            loss3 = compact_loss(query, pos.detach())
            loss = args.lamb1 * loss1 + args.lamb2 * loss2 + args.lamb3 * loss3
            losses.append(loss.item())
            ys_true.append(y_true)
            ys_pred.append(y_pred)
        mean_loss = np.mean(losses)
        y_size = data[f'y_{mode}'].shape[0]
        ys_true, ys_pred = torch.cat(ys_true, dim=0)[:y_size], torch.cat(ys_pred, dim=0)[:y_size]

        if mode == 'test':
            ys_true, ys_pred = ys_true.permute(1, 0, 2, 3), ys_pred.permute(1, 0, 2, 3)
            mae = masked_mae_loss(ys_pred, ys_true).item()
            mape = masked_mape_loss(ys_pred, ys_true).item()
            rmse = masked_rmse_loss(ys_pred, ys_true).item()
            mae_3 = masked_mae_loss(ys_pred[2:3], ys_true[2:3]).item()
            mape_3 = masked_mape_loss(ys_pred[2:3], ys_true[2:3]).item()
            rmse_3 = masked_rmse_loss(ys_pred[2:3], ys_true[2:3]).item()
            mae_6 = masked_mae_loss(ys_pred[5:6], ys_true[5:6]).item()
            mape_6 = masked_mape_loss(ys_pred[5:6], ys_true[5:6]).item()
            rmse_6 = masked_rmse_loss(ys_pred[5:6], ys_true[5:6]).item()
            mae_12 = masked_mae_loss(ys_pred[11:12], ys_true[11:12]).item()
            mape_12 = masked_mape_loss(ys_pred[11:12], ys_true[11:12]).item()
            rmse_12 = masked_rmse_loss(ys_pred[11:12], ys_true[11:12]).item()
            logger.info('out_len overall: mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae, mape, rmse))
            logger.info('out_len 15mins: mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae_3, mape_3, rmse_3))
            logger.info('out_len 30mins: mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae_6, mape_6, rmse_6))
            logger.info('out_len 60mins: mae: {:.4f}, mape: {:.4f}, rmse: {:.4f}'.format(mae_12, mape_12, rmse_12))
            ys_true, ys_pred = ys_true.permute(1, 0, 2, 3), ys_pred.permute(1, 0, 2, 3)
            
        return mean_loss, ys_true, ys_pred
        
def traintest_model(name, mode):
    model = get_model(mode)
    print_model(model)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, eps=args.epsilon)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=args.steps, gamma=args.lr_decay_ratio)
    min_val_loss = float('inf')
    wait = 0
    batches_seen = 0
    for epoch_num in range(args.epochs):
        start_time = time.time()
        model = model.train()
        data_iter = data['train_loader'].get_iterator()
        losses = []
        for x, y in data_iter:
            optimizer.zero_grad()
            x, y, ycov = prepare_x_y(x, y)
            output, h_att, query, pos, neg = model(x, ycov, y, batches_seen)
            y_pred = scaler.inverse_transform(output)
            y_true = scaler.inverse_transform(y)
            loss1 = masked_mae_loss(y_pred, y_true)
            separate_loss = nn.TripletMarginLoss(margin=1.0)
            compact_loss = nn.MSELoss()
            loss2 = separate_loss(query, pos.detach(), neg.detach())
            loss3 = compact_loss(query, pos.detach())
            loss = args.lamb1 * loss1 + args.lamb2 * loss2 + args.lamb3 * loss3
            losses.append(loss.item())
            batches_seen += 1
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
            optimizer.step()
        train_loss = np.mean(losses)
        lr_scheduler.step()
        val_loss, _, _ = evaluate(model, 'val')
        end_time2 = time.time()
        message = 'Epoch [{}/{}] ({}) train_loss: {:.4f}, val_loss: {:.4f}, lr: {:.6f}, {:.1f}s'.format(epoch_num + 1,
                   args.epochs, batches_seen, train_loss, val_loss, optimizer.param_groups[0]['lr'], (end_time2 - start_time))
        logger.info(message)
        test_loss, _, _ = evaluate(model, 'test')

        if val_loss < min_val_loss:
            wait = 0
            min_val_loss = val_loss
            torch.save(model.state_dict(), modelpt_path)
        elif val_loss >= min_val_loss:
            wait += 1
            if wait == args.patience:
                logger.info('Early stopping at epoch: %d' % epoch_num)
                break

    logger.info('=' * 35 + 'Best model performance' + '=' * 35)
    model = get_model(mode)
    model.load_state_dict(torch.load(modelpt_path))
    test_loss, _, _ = evaluate(model, 'test')

#########################################################################################
parser = argparse.ArgumentParser()
parser.add_argument('--dataset', type=str, choices=['METRLA', 'PEMSBAY'], default='PEMSBAY', help='which dataset to run')
parser.add_argument('--trainval_ratio', type=float, default=0.8, help='the ratio of training and validation data among the total')
parser.add_argument('--val_ratio', type=float, default=0.125, help='the ratio of validation data among the trainval ratio')
parser.add_argument('--top_k', type=int, default=2, help='for TimesBlock')
parser.add_argument('--num_kernels', type=int, default=1, help='for Inception')
parser.add_argument('--d_ff', type=int, default=32, help='dimension of fcn')
parser.add_argument('--seq_len', type=int, default=96, help='input sequence length')
parser.add_argument('--out_len', type=int, default=12, help='output sequence length')
parser.add_argument('--input_dim', type=int, default=1, help='number of input channel')
parser.add_argument('--output_dim', type=int, default=1, help='number of output channel')
parser.add_argument('--b_layers', type=int, default=1, help='num of block layers')
parser.add_argument('--d_model', type=int, default=64, help='dimension of model')
parser.add_argument('--max_diffusion_step', type=int, default=3, help='max diffusion step or Cheb K')
parser.add_argument('--mem_num', type=int, default=20, help='number of meta-nodes/prototypes')
parser.add_argument('--mem_dim', type=int, default=64, help='dimension of meta-nodes/prototypes')
parser.add_argument("--loss", type=str, default='MAE', help="MAE, MSE, MaskMAE,MAPE")
parser.add_argument('--lamb1', type=float, default=1.4, help='lamb1 value for MAE loss')
parser.add_argument('--lamb2', type=float, default=0.1, help='lamb2 value for separate loss')
parser.add_argument('--lamb3', type=float, default=0.1, help='lamb3 value for compact loss')
parser.add_argument("--epochs", type=int, default=200, help="number of epochs of training")
parser.add_argument("--batch_size", type=int, default=8, help="size of the batches")
parser.add_argument("--max_grad_norm", type=int, default=5, help="max_grad_norm")
parser.add_argument("--lr", type=float, default=0.001, help="base learning rate")
parser.add_argument("--epsilon", type=float, default=1e-3, help="optimizer epsilon")
parser.add_argument("--steps", type=eval, default=[90], help="steps")
parser.add_argument("--patience", type=int, default=10, help="patience used for early stop")
parser.add_argument("--lr_decay_ratio", type=float, default=0.3, help="lr_decay_ratio")
parser.add_argument("--cl_decay_steps", type=int, default=2000, help="cl_decay_steps")
parser.add_argument('--gpu', type=int, default=0, help='which gpu to use')
parser.add_argument('--seed', type=int, default=1000, help='random seed')
args = parser.parse_args()
        
if args.dataset == 'METRLA':
    data_path = f'../{args.dataset}/metr-la.h5'
    args.num_nodes = 207
elif args.dataset == 'PEMSBAY':
    data_path = f'../{args.dataset}/pems-bay.h5'
    args.num_nodes = 325
else:
    pass   # including more datasets in the future

model_name = 'MGGLTN'
timestring = time.strftime('%Y%m%d%H%M%S', time.localtime())
path = f'../save/{args.dataset}_{model_name}_{timestring}'
logging_path = f'{path}/{model_name}_{timestring}_logging.txt'
score_path = f'{path}/{model_name}_{timestring}_scores.txt'
epochlog_path = f'{path}/{model_name}_{timestring}_epochlog.txt'
modelpt_path = f'{path}/{model_name}_{timestring}.pt'
if not os.path.exists(path): os.makedirs(path)
shutil.copy2(sys.argv[0], path)
shutil.copy2(f'{model_name}.py', path)
shutil.copy2('utils.py', path)
    
logger = logging.getLogger(__name__)
logger.setLevel(level = logging.INFO)
class MyFormatter(logging.Formatter):
    def format(self, record):
        spliter = ' '
        record.msg = str(record.msg) + spliter + spliter.join(map(str, record.args))
        record.args = tuple() # set empty to args
        return super().format(record)
formatter = MyFormatter()
handler = logging.FileHandler(logging_path, mode='a')
handler.setLevel(logging.INFO)
handler.setFormatter(formatter)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(formatter)
logger.addHandler(handler)
logger.addHandler(console)

logger.info('model', model_name)
logger.info('dataset', args.dataset)
logger.info('val_ratio', args.val_ratio)
logger.info('num_nodes', args.num_nodes)
logger.info('seq_len', args.seq_len)
logger.info('out_len', args.out_len)
logger.info('input_dim', args.input_dim)
logger.info('output_dim', args.output_dim)
logger.info('max_diffusion_step', args.max_diffusion_step)
logger.info('mem_num', args.mem_num)
logger.info('mem_dim', args.mem_dim)
logger.info('loss', args.loss)
logger.info('MAE loss lamb1', args.lamb1)
logger.info('separate loss lamb2', args.lamb2)
logger.info('compact loss lamb3', args.lamb3)
logger.info('batch_size', args.batch_size)
logger.info('epochs', args.epochs)
logger.info('patience', args.patience)
logger.info('lr', args.lr)
logger.info('epsilon', args.epsilon)
logger.info('steps', args.steps)
logger.info('lr_decay_ratio', args.lr_decay_ratio)

cpu_num = 1
os.environ ['OMP_NUM_THREADS'] = str(cpu_num)
os.environ ['OPENBLAS_NUM_THREADS'] = str(cpu_num)
os.environ ['MKL_NUM_THREADS'] = str(cpu_num)
os.environ ['VECLIB_MAXIMUM_THREADS'] = str(cpu_num)
os.environ ['NUMEXPR_NUM_THREADS'] = str(cpu_num)
torch.set_num_threads(cpu_num)
device = torch.device("cuda:{}".format(args.gpu)) if torch.cuda.is_available() else torch.device("cpu")
np.random.seed(args.seed)
torch.manual_seed(args.seed)
if torch.cuda.is_available(): torch.cuda.manual_seed(args.seed)
#####################################################################################################

data = {}
for category in ['train', 'val', 'test']:
    cat_data = np.load(os.path.join(f'../{args.dataset}', category + '.npz'))
    data['x_' + category] = cat_data['x']
    data['y_' + category] = cat_data['y']
scaler = StandardScaler(mean=data['x_train'][..., 0].mean(), std=data['x_train'][..., 0].std())
for category in ['train', 'val', 'test']:
    data['x_' + category][..., 0] = scaler.transform(data['x_' + category][..., 0])
    data['y_' + category][..., 0] = scaler.transform(data['y_' + category][..., 0])
data['train_loader'] = DataLoader(data['x_train'], data['y_train'], args.batch_size, shuffle=True)
data['val_loader'] = DataLoader(data['x_val'], data['y_val'], args.batch_size, shuffle=False)
data['test_loader'] = DataLoader(data['x_test'], data['y_test'], args.batch_size, shuffle=False)

def main():
    logger.info(args.dataset, 'training and testing started', time.ctime())
    logger.info('train xs.shape, ys.shape', data['x_train'].shape, data['y_train'].shape)
    logger.info('val xs.shape, ys.shape', data['x_val'].shape, data['y_val'].shape)
    logger.info('test xs.shape, ys.shape', data['x_test'].shape, data['y_test'].shape)
    traintest_model(model_name, 'train')
    logger.info(args.dataset, 'training and testing ended', time.ctime())
    
if __name__ == '__main__':
    main()