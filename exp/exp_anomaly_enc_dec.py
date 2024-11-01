from data_provider.data_factory import data_provider, get_events
from exp.exp_basic import Exp_Basic
from utils.tools import EarlyStopping, adjust_learning_rate, write_to_csv, get_dynamic_scores, get_gaussian_kernel_scores, compute_metrics, plot_loss, plot_memory, fit_distributions, get_scores_channelwise
import torch
import torch.nn as nn
from torch import optim
import os
import time
import numpy as np

class Exp_Anomaly_Enc_Dec(Exp_Basic):
    def __init__(self, args):
        super(Exp_Anomaly_Enc_Dec, self).__init__(args)

    def _build_model(self):
        model = self.model_dict[self.args.model].Model(self.args).float()

        if self.args.use_multi_gpu and self.args.use_gpu:
            model = nn.DataParallel(model, device_ids=self.args.device_ids)
        return model

    def _get_data(self, flag):
        data_set, data_loader = data_provider(self.args, flag)
        return data_set, data_loader

    def _select_optimizer(self):
        model_optim = optim.Adam(self.model.parameters(), lr=self.args.learning_rate)
        return model_optim

    def _select_criterion(self):
        criterion = nn.MSELoss()
        return criterion

    def get_events(self, y_test):
        events = get_events(y_test)
        return events

    def vali(self, vali_data, vali_loader, criterion):
        total_loss = []
        self.model.eval()
        with torch.no_grad():
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(vali_loader):
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float()

                batch_x_mark = batch_x_mark.float().to(self.device)
                batch_y_mark = batch_y_mark.float().to(self.device)

                # decoder input
                dec_inp = torch.zeros_like(batch_y[:, -self.args.pred_len:, :]).float()
                dec_inp = torch.cat([batch_y[:, :self.args.label_len, :], dec_inp], dim=1).float().to(self.device)
                # encoder - decoder
                if self.args.use_amp:
                    with torch.cuda.amp.autocast():
                        if self.args.output_attention:
                            outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)[0]
                        else:
                            outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)
                else:
                    if self.args.output_attention:
                        outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)[0]
                    else:
                        outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)
                f_dim = -1 if self.args.features == 'MS' else 0
                outputs = outputs[:, -self.args.pred_len:, f_dim:]
                batch_y = batch_y[:, -self.args.pred_len:, f_dim:].to(self.device)
                pred = outputs.detach().cpu()
                true = batch_y.detach().cpu()
                loss = criterion(pred, true)
                total_loss.append(loss.item())
        self.model.train()
        return total_loss

    def train(self, setting):
        train_data, train_loader = self._get_data(flag='train')
        vali_data, vali_loader = self._get_data(flag='val')
        train_energy = []
        path = os.path.join(self.args.checkpoints, setting)
        if not os.path.exists(path):
            os.makedirs(path)
        time_now = time.time()
        training_start_time=time.time()
        
        metrics = {'epoch': [],'iters': [], 'loss': [], 'allocated_memory': [], 'reserved_memory': []}
        
        train_steps = len(train_loader)
        early_stopping = EarlyStopping(patience=self.args.patience, verbose=True)

        model_optim = self._select_optimizer()
        criterion = self._select_criterion()
        self.anomaly_criterion = nn.MSELoss(reduce=False)
        vali_loss_array = []
        
        if self.args.use_amp:
            scaler = torch.cuda.amp.GradScaler()
        total_iters=0
        
        for epoch in range(self.args.train_epochs):
            iter_count = 0
            train_loss = []

            self.model.train()
            epoch_time = time.time()
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(train_loader):
                torch.cuda.reset_peak_memory_stats()
                iter_count +=1
                total_iters +=1
                
                model_optim.zero_grad()
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float().to(self.device)
                batch_x_mark = batch_x_mark.float().to(self.device)
                batch_y_mark = batch_y_mark.float().to(self.device)
                
                # decoder input
                dec_inp = torch.zeros_like(batch_y[:, -self.args.pred_len:, :]).float()
                dec_inp = torch.cat([batch_y[:, :self.args.label_len, :], dec_inp], dim=1).float().to(self.device)

                # encoder - decoder
                if self.args.use_amp:
                    with torch.cuda.amp.autocast():
                        if self.args.output_attention:
                            outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)[0]
                        else:
                            outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)

                        f_dim = -1 if self.args.features == 'MS' else 0
                        outputs = outputs[:, -self.args.pred_len:, f_dim:]
                        batch_y = batch_y[:, -self.args.pred_len:, f_dim:].to(self.device)
                        loss = criterion(outputs, batch_y)
                        train_loss.append(loss.item())
                else:
                    if self.args.output_attention:
                        outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)[0]
                    else:
                        outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)

                    f_dim = -1 if self.args.features == 'MS' else 0
                    outputs = outputs[:, -self.args.pred_len:, f_dim:]
                    batch_y = batch_y[:, -self.args.pred_len:, f_dim:].to(self.device)
                    loss = criterion(outputs, batch_y)                    
                    train_loss.append(loss.item())
                    score = self.anomaly_criterion(outputs, batch_y)
                
                    score = score.detach().cpu().numpy()
                    train_energy.append(score)
                    
                allocated_memory = torch.cuda.max_memory_allocated(self.device) / (1024 ** 2)  # In MB
                reserved_memory = torch.cuda.max_memory_reserved(self.device) / (1024 ** 2)  # In MB
                
                if (i + 1) % 100 == 0:
                    print("\titers: {0}, epoch: {1} | loss: {2:.7f}".format(i + 1, epoch + 1, loss.item()))
                    speed = (time.time() - time_now) / iter_count
                    left_time = speed * ((self.args.train_epochs - epoch) * train_steps - i)
                    print('\tspeed: {:.4f}s/iter; left time: {:.4f}s'.format(speed, left_time))
                    iter_count = 0
                    time_now = time.time()
                    
                metrics['epoch'].append(epoch + 1)
                metrics['iters'].append(total_iters)
                metrics['loss'].append(loss.item())
                metrics['allocated_memory'].append(allocated_memory)
                metrics['reserved_memory'].append(reserved_memory)

                if self.args.use_amp:
                    scaler.scale(loss).backward()
                    scaler.step(model_optim)
                    scaler.update()
                else:
                    loss.backward()
                    model_optim.step()

            print("Epoch: {} cost time: {}".format(epoch + 1, time.time() - epoch_time))
            vali_loss = self.vali(vali_data, vali_loader, criterion)
            vali_loss_array.append(vali_loss)

            print("Epoch: {0}, Steps: {1} | Train Loss: {2:.7f} Vali Loss: {3:.7f}".format(epoch + 1, train_steps, np.mean(train_loss), np.mean(vali_loss)))
            early_stopping(np.mean(vali_loss), self.model, path)
            if early_stopping.early_stop:
                print("Early stopping")
                break

            adjust_learning_rate(model_optim, epoch + 1, self.args)

        train_energy = np.array(train_energy)
        
        # Get the shape dynamically
        shape = train_energy.shape
        dim1, dim2, dim3, dim4 = shape
        train_scores_tc = train_energy.reshape(dim1 * dim2 * dim3, dim4)
        
        best_model_path = path + '/' + 'checkpoint.pth'
        self.model.load_state_dict(torch.load(best_model_path))

        plot_loss(metrics, os.path.join('./test_results/', setting), 'training_loss_plot.png', "Train")
        plot_loss(np.array(vali_loss_array).flatten(), os.path.join('./test_results/', setting), 'vali_loss_plot.png', 'Validation')
        plot_memory(metrics, os.path.join('./test_results/', setting), 'training_memory_plot.png', 'Train')

        avg_train_loss = np.mean(metrics['loss'])
        avg_vali_loss = np.mean(vali_loss)
        
        train_end_time = time.time()
        train_duration = train_end_time - training_start_time        
        return self.model, avg_train_loss, avg_vali_loss, train_duration, train_scores_tc, metrics['allocated_memory'],  metrics['reserved_memory']

    def test(self, setting,avg_train_loss, avg_vali_loss,train_duration, train_scores_tc, avg_allocated_memory_train, avg_reserved_memory_train, test=0):
        test_data, test_loader = self._get_data(flag='test')

        test_start_time = time.time()
        if test:
            print('loading model')
            self.model.load_state_dict(torch.load(os.path.join('./checkpoints/' + setting, 'checkpoint.pth')))
        criterion = self._select_criterion()
        anomaly_criterion = nn.MSELoss(reduce=False)
        preds = []
        trues = []
        anomaly_preds=[]
        labels=[]
        
        test_metrics = {'iters': [], 'loss': [], 'allocated_memory': [], 'reserved_memory': []}
        
        test_scores = []
        self.model.eval()
        with torch.no_grad():
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark,seq_labels) in enumerate(test_loader):
                torch.cuda.reset_peak_memory_stats()
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float().to(self.device)
                batch_x_mark = batch_x_mark.float().to(self.device)
                batch_y_mark = batch_y_mark.float().to(self.device)

                # decoder input
                dec_inp = torch.zeros_like(batch_y[:, -self.args.pred_len:, :]).float()
                dec_inp = torch.cat([batch_y[:, :self.args.label_len, :], dec_inp], dim=1).float().to(self.device)
                # encoder - decoder
                if self.args.use_amp:
                    with torch.cuda.amp.autocast():
                        if self.args.output_attention:
                            outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)[0]
                        else:
                            outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)
                else:
                    if self.args.output_attention:
                        outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)[0]

                    else:
                        outputs = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)

                f_dim = -1 if self.args.features == 'MS' else 0
                outputs = outputs[:, -self.args.pred_len:, :]
                batch_y = batch_y[:, -self.args.pred_len:, :].to(self.device)

                loss = anomaly_criterion(outputs, batch_y)
                
                test_metrics['iters'].append(i)
                test_metrics['loss'].append(criterion(outputs, batch_y).item())

                test_scores.append(loss.cpu().numpy())
                pred = outputs.detach().cpu()
                outputs = outputs.detach().cpu().numpy()
                batch_y = batch_y.detach().cpu().numpy()
                
                if test_data.scale and self.args.inverse:
                    shape = outputs.shape
                    outputs = test_data.inverse_transform(outputs.squeeze(0)).reshape(shape)
                    batch_y = test_data.inverse_transform(batch_y.squeeze(0)).reshape(shape)

                outputs = outputs[:, :, f_dim:]
                batch_y = batch_y[:, :, f_dim:]
                pred = outputs

                true = batch_y
                preds.append(pred)
                trues.append(true)
                labels.append(seq_labels)
                labels_ = torch.stack(labels)
                
                allocated_memory = torch.cuda.max_memory_allocated(self.device) / (1024 ** 2)  # In MB
                reserved_memory = torch.cuda.max_memory_reserved(self.device) / (1024 ** 2)  # In MB

                test_metrics['allocated_memory'].append(allocated_memory)
                test_metrics['reserved_memory'].append(reserved_memory)

                if i % 20 == 0:
                    input = batch_x.detach().cpu().numpy()
                    if test_data.scale and self.args.inverse:
                        shape = input.shape
                        input = test_data.inverse_transform(input.squeeze(0)).reshape(shape)
                    gt = np.concatenate((input[0, :, -1], true[0, :, -1]), axis=0)
                    pd = np.concatenate((input[0, :, -1], pred[0, :, -1]), axis=0)

        plot_loss(test_metrics, os.path.join('./test_results/', setting), 'test_loss_plot.png','Test')
        plot_memory(test_metrics, os.path.join('./test_results/', setting), 'test_memory_plot.png','Test')
        
        preds=np.array(preds)
        #(total_batches, batch_size, pred_len, num_channels)
        test_scores = np.array(test_scores)
        shape = test_scores.shape 
        dim1, dim2, dim3, dim4 = shape
        test_scores_tc = test_scores.reshape(dim1 * dim2 * dim3, dim4)
        
        preds = np.array(preds)
        trues = np.array(trues)

        preds = preds.reshape(-1, preds.shape[-2], preds.shape[-1])
        trues = trues.reshape(-1, trues.shape[-2], trues.shape[-1])

        anomaly_preds = torch.tensor(anomaly_preds).flatten()

        # dtw calculation
        if self.args.use_dtw:
            dtw_list = []
            manhattan_distance = lambda x, y: np.abs(x - y)
            for i in range(preds.shape[0]):
                x = preds[i].reshape(-1,1)
                y = trues[i].reshape(-1,1)
                if i % 100 == 0:
                    print("calculating dtw iter:", i)
                d, _, _, _ = accelerated_dtw(x, y, dist=manhattan_distance)
                dtw_list.append(d)
            dtw = np.array(dtw_list).mean()
        else:
            dtw = -999

        test_end_time = time.time()
        test_duration = test_end_time - test_start_time
        total_time = train_duration + test_duration

        gt = torch.stack(labels, dim=0)
        gt = gt.flatten()
        true_events=self.get_events(gt)

        if isinstance(gt, torch.Tensor):
            gt = gt.cpu().numpy()

        distributions_dic = fit_distributions(
            distr_par_file="distr_params.pkl",
            distr_names=["univar_gaussian"],
            predictions_dic={
                "train_raw_scores": train_scores_tc
            }
        )

        # Compute anomaly scores
        train_ano_scores, val_ano_scores, test_ano_scores, train_prob_scores, val_prob_scores, test_prob_scores = get_scores_channelwise(
            distr_params=distributions_dic['univar_gaussian'],
            train_raw_scores=train_scores_tc,
            test_raw_scores=test_scores_tc,
            val_raw_scores=None,
            logcdf=False
        )

        score_t_test_dyn, score_tc_test_dyn, score_t_train_dyn, score_tc_train_dyn =  get_dynamic_scores(train_scores_tc, test_scores_tc, None,None, long_window=self.args.d_score_long_window, short_window=self.args.d_score_short_window)
        
        score_t_test_dyn_gauss_conv, score_tc_test_dyn_gauss_conv = get_gaussian_kernel_scores(None,score_tc_test_dyn,self.args.kernel_sigma)

        score_t_train_dyn_gauss_conv, score_tc_train_dyn_gauss_conv = get_gaussian_kernel_scores(None,score_tc_train_dyn,self.args.kernel_sigma)
        
        eval_metrics, anomaly_ratio = compute_metrics(test_ano_scores, gt, true_events,score_t_test_dyn, score_t_test_dyn_gauss_conv, self.args.seq_len , train_ano_scores, score_t_train_dyn, score_t_train_dyn_gauss_conv)

        #transform loss & memory usage into scalar
        avg_train_loss = np.mean(avg_train_loss)
        avg_test_loss= np.mean(test_metrics['loss'])

        total_allocated_memory_usage = np.concatenate([test_metrics['allocated_memory'],avg_allocated_memory_train])
        total_reserved_memory_usage = np.concatenate([test_metrics['reserved_memory'],avg_reserved_memory_train])

        total_allocated_memory_usage=np.mean(total_allocated_memory_usage)
        total_reserved_memory_usage=np.mean(total_reserved_memory_usage)

        test_results_path = os.path.join('./test_results/', setting)
        if not os.path.exists(test_results_path):
            os.makedirs(test_results_path)
        export_memory_usage = False
        write_to_csv(
            model_name=self.args.model,
            model_id=self.args.model_id,
            avg_train_loss=avg_train_loss,
            avg_vali_loss=avg_vali_loss,
            avg_test_loss=avg_test_loss,
            seq_len=self.args.seq_len,
            d_model=self.args.d_model,
            enc_in=self.args.enc_in,
            e_layers=self.args.e_layers,
            dec_in=self.args.dec_in,
            d_layers=self.args.d_layers,
            c_out=self.args.c_out,
            d_ff=self.args.d_ff,
            n_heads=self.args.n_heads,
            long_window=self.args.d_score_long_window, 
            short_window=self.args.d_score_short_window,
            kernel_sigma=self.args.kernel_sigma,
            train_epochs=self.args.train_epochs,
            learning_rate=self.args.learning_rate,
            batch_size=self.args.batch_size,
            anomaly_ratio=anomaly_ratio,
            embed=self.args.embed,
            total_time=total_time,
            train_duration=train_duration,
            test_duration=test_duration,
            metrics=eval_metrics,
            test_results_path=test_results_path,
            setting=setting,
            benchmark_id = self.args.benchmark_id,
            total_allocated_memory_usage=total_allocated_memory_usage,
            total_reserved_memory_usage=total_reserved_memory_usage
        )

        return