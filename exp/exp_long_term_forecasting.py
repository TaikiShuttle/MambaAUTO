'''
MIT License

Copyright (c) 2022 THUML @ Tsinghua University

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''
import tracemalloc

import sys
from data_provider.data_factory import data_provider
from exp.exp_basic import Exp_Basic
from utils.tools import EarlyStopping, adjust_learning_rate, visual
from utils.metrics import metric
import torch
import torch.nn as nn
from torch import optim
import os
import time
import warnings
import numpy as np
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
import pandas

# init tensorboard
writer = SummaryWriter()

warnings.filterwarnings('ignore')


class Exp_Long_Term_Forecast(Exp_Basic):
    def __init__(self, args):
        super(Exp_Long_Term_Forecast, self).__init__(args)
        
    def _build_model(self):
        # load model
        model = self.model_dict['MambaAUTO'].MambaAUTO(self.args)

        if self.args.use_multi_gpu:
            self.device = torch.device('cuda:{}'.format(self.args.local_rank))
            model = DDP(model.cuda(), device_ids = [self.args.local_rank])
        else:
            self.device = self.args.gpu
            model = model.to(self.device)
        return model

    def _get_data(self, flag):
        data_set, data_loader = data_provider(self.args, flag)
        return data_set, data_loader

    def _select_optimizer(self):
        # load parameters
        p_list = []
        for n, p in self.model.named_parameters():
            if not p.requires_grad:
                continue
            else:
                p_list.append(p)
                if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
                    print(n, p.dtype, p.shape)

        # use adamw as optimizer
        model_optim = optim.AdamW([{'params': p_list}], lr = self.args.learning_rate, weight_decay = self.args.weight_decay)
        if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
            print('next learning rate is {}'.format(self.args.learning_rate))
        return model_optim

    def _select_criterion(self):
        criterion = nn.MSELoss()
        return criterion

    def vali(self, vali_data, vali_loader, criterion, is_test=False):
        total_loss = []
        total_count = []
        time_now = time.time()
        test_steps = len(vali_loader)
        iter_count = 0
        self.model.eval()
        with torch.no_grad():
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(tqdm(vali_loader, desc = 'Validation')):
                iter_count += 1
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float()
                batch_x_mark = batch_x_mark.float().to(self.device)
                batch_y_mark = batch_y_mark.float().to(self.device)
                
                if self.args.use_amp:
                    with torch.cuda.amp.autocast():
                        outputs = self.model(batch_x, batch_x_mark, None, batch_y_mark)
                else:
                        outputs = self.model(batch_x, batch_x_mark, None, batch_y_mark)

                if is_test:
                    # the outputs is in shape [batch_size, seq_len, feature_dim], test set only needs the predicted token.
                    outputs = outputs[:, -self.args.token_len:, :]
                    batch_y = batch_y[:, -self.args.token_len:, :].to(self.device)
                else:
                    # the outputs is in shape [batch_size, seq_len, feature_dim].
                    outputs = outputs[:, :, :]
                    batch_y = batch_y[:, :, :].to(self.device)

                loss = criterion(outputs, batch_y)

                loss = loss.detach().cpu()
                total_loss.append(loss)
                total_count.append(batch_x.shape[0])
                # if (i + 1) % 100 == 0:
                    # if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
                    #     speed = (time.time() - time_now) / iter_count
                    #     left_time = speed * (test_steps - i)
                    #     print("\titers: {}, speed: {:.4f}s/iter, left time: {:.4f}s".format(i + 1, speed, left_time))
                    #     iter_count = 0
                    #     time_now = time.time()
        if self.args.use_multi_gpu:
            total_loss = torch.tensor(np.average(total_loss, weights=total_count)).to(self.device)
            dist.barrier()   
            dist.all_reduce(total_loss, op=dist.ReduceOp.SUM)
            total_loss = total_loss.item() / dist.get_world_size()
        else:
            total_loss = np.average(total_loss, weights=total_count)
        self.model.train()
        if is_test:
            print("Test Loss: {:.7f}".format(total_loss))
        else:
            print("Validation Loss: {:.7f}".format(total_loss))
        return total_loss

    def train(self, setting):
        train_data, train_loader = self._get_data(flag='train')
        vali_data, vali_loader = self._get_data(flag='val')
        test_data, test_loader = self._get_data(flag='test')

        path = os.path.join(self.args.checkpoints, setting)
        if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
            if not os.path.exists(path):
                os.makedirs(path)

        time_now = time.time()

        train_steps = len(train_loader)
        early_stopping = EarlyStopping(self.args, verbose=True)
        
        model_optim = self._select_optimizer()
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(model_optim, T_max=self.args.tmax, eta_min=1e-8)
        criterion = self._select_criterion()
        if self.args.use_amp:
            scaler = torch.cuda.amp.GradScaler()

        for epoch in tqdm(range(self.args.train_epochs), desc = 'Training'):
            iter_count = 0

            loss_val = torch.tensor(0., device="cuda")
            count = torch.tensor(0., device="cuda")
            
            self.model.train()
            epoch_time = time.time()
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(tqdm(train_loader, desc = 'Epoch: {}'.format(epoch + 1), leave = False)):
                iter_count += 1
                model_optim.zero_grad()
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float().to(self.device)
                batch_x_mark = batch_x_mark.float().to(self.device)
                batch_y_mark = batch_y_mark.float().to(self.device)

                if self.args.use_amp:
                    with torch.cuda.amp.autocast():
                        outputs = self.model(batch_x, batch_x_mark, None, batch_y_mark)
                        loss = criterion(outputs, batch_y)                        
                        loss_val += loss.item()
                        count += 1
                else:
                    outputs = self.model(batch_x, batch_x_mark, None, batch_y_mark)
                    loss = criterion(outputs, batch_y)
                    loss_val += loss.item()
                    count += 1
                
                if (i + 1) % 100 == 0:
                    if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
                        # report loss to tensorboard
                        writer.add_scalar('Training Loss', loss_val / count, epoch * train_steps + i)

                        # print("\titers: {0}, epoch: {1} | loss: {2:.7f}".format(i + 1, epoch + 1, loss.item()))
                        # speed = (time.time() - time_now) / iter_count
                        # left_time = speed * ((self.args.train_epochs - epoch) * train_steps - i)
                        # print('\tspeed: {:.4f}s/iter; left time: {:.4f}s'.format(speed, left_time))
                        # iter_count = 0
                        # time_now = time.time()

                if self.args.use_amp:
                    scaler.scale(loss).backward()
                    scaler.step(model_optim)
                    scaler.update()
                else:
                    loss.backward()
                    model_optim.step()

                # if (i + 1) % 1000 == 0:
                #     snapshot = tracemalloc.take_snapshot()
                #     top_stats = snapshot.statistics('lineno')
                #     print("[ Top 10 ]")
                #     for stat in top_stats[:10]:
                #         print(stat)
                #         sys.stdout.flush()
                #     stat = top_stats[0]
                #     print("%s memory blocks: %.1f KiB" % (stat.count, stat.size / 1024))
                #     for line in stat.traceback.format():
                #         print(line)
            
            # if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
            #     print("Epoch: {} cost time: {}".format(epoch + 1, time.time() - epoch_time))   
            if self.args.use_multi_gpu:
                dist.barrier()   
                dist.all_reduce(loss_val, op=dist.ReduceOp.SUM)
                dist.all_reduce(count, op=dist.ReduceOp.SUM)      

            train_loss = loss_val.item() / count.item()

            vali_loss = self.vali(vali_data, vali_loader, criterion)
            writer.add_scalar('Validation Loss', vali_loss, epoch)
            test_loss = self.vali(test_data, test_loader, criterion, is_test=True)
            writer.add_scalar('Test Loss', test_loss, epoch)

            # report losses each epoch
            if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
                print("Epoch: {}, Steps: {} | Train Loss: {:.7f} Vali Loss: {:.7f} Test Loss: {:.7f}".format(
                    epoch + 1, train_steps, train_loss, vali_loss, test_loss))
            early_stopping(vali_loss, self.model, path)
            if early_stopping.early_stop:
                if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
                    print("Early stopping")
                break

            # print("lr = {:.10f}".format(model_optim.param_groups[0]['lr']))

            # the learning rate is adjusted manually.
            if self.args.cosine:
                scheduler.step()
                if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
                    print("lr = {:.10f}".format(model_optim.param_groups[0]['lr']))
            else:
                adjust_learning_rate(model_optim, epoch + 1, self.args)
            if self.args.use_multi_gpu:
                train_loader.sampler.set_epoch(epoch + 1)
                
        best_model_path = path + '/' + 'checkpoint.pth'
        if self.args.use_multi_gpu:
            dist.barrier()
            self.model.load_state_dict(torch.load(best_model_path), strict=False)
        else:
            self.model.load_state_dict(torch.load(best_model_path), strict=False)
        return self.model

    def test(self, setting, test=0):
        test_data, test_loader = self._get_data(flag='test')

        print("info:", self.args.test_seq_len, self.args.test_label_len, self.args.token_len, self.args.test_pred_len)
        if test:
            print('loading model')
            setting = self.args.test_dir
            best_model_path = self.args.test_file_name

            print("loading model from {}".format(os.path.join(self.args.checkpoints, setting, best_model_path)))
            load_item = torch.load(os.path.join(self.args.checkpoints, setting, best_model_path))
            self.model.load_state_dict({k.replace('module.', ''): v for k, v in load_item.items()}, strict=False)

        preds = []
        trues = []
        folder_path = './test_results/' + setting + '/'
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
        time_now = time.time()
        test_steps = len(test_loader)
        iter_count = 0
        self.model.eval()
        with torch.no_grad():
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(tqdm(test_loader, desc = 'Test')):
                iter_count += 1
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float().to(self.device)
                batch_x_mark = batch_x_mark.float().to(self.device)
                batch_y_mark = batch_y_mark.float().to(self.device)

                inference_steps = self.args.test_pred_len // self.args.token_len
                dis = self.args.test_pred_len - inference_steps * self.args.token_len
                if dis != 0:
                    inference_steps += 1
                pred_y = []
                for j in range(inference_steps):
                    if len(pred_y) != 0:
                        batch_x = torch.cat([batch_x[:, self.args.token_len:, :], pred_y[-1]], dim=1)
                        tmp = batch_y_mark[:, j-1:j, :]
                        batch_x_mark = torch.cat([batch_x_mark[:, 1:, :], tmp], dim=1)
                        
                    if self.args.use_amp:
                        with torch.cuda.amp.autocast():
                            outputs = self.model(batch_x, batch_x_mark, None, batch_y_mark)
                    else:
                        outputs = self.model(batch_x, batch_x_mark, None, batch_y_mark)
                    pred_y.append(outputs[:, -self.args.token_len:, :])
                pred_y = torch.cat(pred_y, dim=1)
                if dis != 0:
                    pred_y = pred_y[:, :-dis, :]
                batch_y = batch_y[:, -self.args.test_pred_len:, :].to(self.device)
                outputs = pred_y.detach().cpu()
                batch_y = batch_y.detach().cpu()

                pred = outputs
                true = batch_y

                preds.append(pred)
                trues.append(true)
                # if (i + 1) % 100 == 0:
                #     if (self.args.use_multi_gpu and self.args.local_rank == 0) or not self.args.use_multi_gpu:
                #         speed = (time.time() - time_now) / iter_count
                #         left_time = speed * (test_steps - i)
                #         print("\titers: {}, speed: {:.4f}s/iter, left time: {:.4f}s".format(i + 1, speed, left_time))
                #         iter_count = 0
                #         time_now = time.time()
                    
                if self.args.visualize and i % 2 == 0:
                    gt = np.array(true[0, :, -1])
                    pd = np.array(pred[0, :, -1])
                    dir_path = folder_path + f'{self.args.test_pred_len}/'
                    if not os.path.exists(dir_path):
                        os.makedirs(dir_path)
                    visual(gt, pd, os.path.join(dir_path, f'{i}.pdf'))
                
                if i == 0 or i == self.args.test_pred_len:
                    gt = np.array(true[0, :, -1])
                    pd = np.array(pred[0, :, -1])
                    dir_path = folder_path
                    if not os.path.exists(dir_path):
                        os.makedirs(dir_path)
                    df = pandas.DataFrame({"Ground Truth": gt, "Prediction": pd})
                    df.to_csv(os.path.join(dir_path, f'{i}.csv'), index=False)
        
        preds = torch.cat(preds, dim=0).numpy()
        trues = torch.cat(trues, dim=0).numpy()
        
        mae, mse, rmse, mape, mspe = metric(preds, trues)
        print('mse:{}, mae:{}'.format(mse, mae))
        f = open("result_long_term_forecast.txt", 'a')
        f.write(setting + "  \n")
        f.write('mse:{}, mae:{}'.format(mse, mae))
        f.write('\n')
        f.write('\n')
        f.close()
        return