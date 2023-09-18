
import os
from pathlib import Path
import GPUtil
import argparse
import collections
import numpy as np
import time
from sklearn import metrics
import torch
import copy

from data_loaders import Dataloaders
from utils import Utils
from logger import Logger
from checkpoint import Checkpoint
from dataset_preparation import DatasetPreparation
from utils_wandb import Wandb
from eval import Eval
from config import Config
from create_samples import create_samples


FILE_NAME_TEST_RESULTS = 'test_results.log'
FILE_NAME_TRAINING = 'training.log'
FILE_NAME_TEST_RESULTS_AVERAGE_BEST = 'test_results_average_best.log'

def test_model(description, save_name, class_labels, save_path_cv:Path, loss_function, config) -> None:
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    checkpoint = Checkpoint(save_name, save_path_cv, device, config)
    if checkpoint is not None:
        eval_test = Eval(Dataloaders.testInd, device, checkpoint.model, loss_function, config['num_out'])
        Logger.printer(description, config['early_stop_accuracy'], eval_test.metrics, eval_test.mean_loss)
        if config['send_final_results']:
            Wandb.wandb_log(eval_test.mean_loss, eval_test.metrics, class_labels, 0, checkpoint.optimizer, save_name, config)

def train_and_eval(config:Config):
    cust_data = DatasetPreparation(config)
        
    Logger.print_section_line()
    Dataloaders.setup_data_loader_testset(
        cust_data,
        config
        )
    
    device = Utils.config_torch_and_cuda(config)
    
    loss_function = Utils.get_new_lossfunction(cust_data.class_weights, device, config['loss_function'])

    for cv in range(config['num_cv']):
        #if cv != 4:
        #    continue
        #cv = 4
        
        early_stop = False
        es_counter = 0
        last_best_epoch_current_cv = -1
        validation_best_metrics_current_cv = [0.0] * 9
        
        Logger.print_section_line()
        
        cv_done, save_path_cv = Utils.check_cv_status(cv, config.save_path)
        if cv_done:
            print('Skipped CV number:', cv+1, '(already trained)')
        else:
            with Logger(save_path_cv / FILE_NAME_TRAINING, 'a'):
                print('Start CV number:', cv+1, '\n')
                
                valid_ind_for_cv, train_ind_for_cv = cust_data.get_train_valid_ind(cv)
                train_eval_ind_for_cv = train_ind_for_cv[::2]
                
                Dataloaders.setup_data_loaders_training(train_ind_for_cv,
                                                        train_eval_ind_for_cv,
                                                        valid_ind_for_cv,
                                                        cust_data,
                                                        config
                                                        )
                print("\nTrain iterations / batches per epoch: ", int(len(Dataloaders.trainInd)))
                
                checkpoint = Checkpoint('checkpoint_latest', save_path_cv, device, config)
                
                pytorch_total_params = sum(p.numel() for p in checkpoint.model.parameters() if p.requires_grad)
                print('Number of Training Parameters', pytorch_total_params)
                
                Wandb.init(cv, checkpoint.wandb_id, config)
                
                start_time = time.time()
                
                Logger.print_section_line()
                print("Training...")
                early_stop = False
                es_counter = 0
                
                for epoch in range(checkpoint.start_epoch, config['epochs'] + 1):
                    if early_stop:
                        break
                    
                    Logger.print_section_line()
                    duration_epoch = Utils.train_one_epoch(checkpoint.model, device, loss_function, checkpoint.scaler, checkpoint.optimizer, config)

                    print('Evaluating epoch...')
                    duration_cv = time.time() - start_time
                    
                    eval_valid = Eval(Dataloaders.valInd, device, checkpoint.model, loss_function, config['num_out'])
                    checkpoint.update_eval_valid(eval_valid)
                    
                    Wandb.wandb_log(eval_valid.mean_loss, eval_valid.metrics, cust_data.label_classes, epoch, checkpoint.optimizer, 'Validation', config)

                    if config['calc_train_error']:
                        eval_train = Eval(Dataloaders.trainInd_eval, device, checkpoint.model, loss_function, config['num_out'])
                        Wandb.wandb_log(eval_train.mean_loss, eval_train.metrics, None, 0, None, 'Trainset Error', config)

                    # Save epoch state and update metrics, es_counter, etc.
                    improvement_identified = round(checkpoint.eval_valid.metrics[8], config['early_stop_accuracy']) > round(validation_best_metrics_current_cv[8], config['early_stop_accuracy'])
                    if config['calc_train_error']:
                        no_overfitting = round(checkpoint.eval_valid.metrics[8], config['early_stop_accuracy']) <= round(eval_train.metrics[8], config['early_stop_accuracy'])
                    else:
                        no_overfitting = True
                        
                    if improvement_identified and no_overfitting or epoch == 1:
                        checkpoint.save_checkpoint('checkpoint_best', epoch, save_path_cv)
                        
                        Checkpoint.delete_checkpoint('checkpoint_best', last_best_epoch_current_cv, save_path_cv)
                        
                        es_counter = 0
                        validation_best_metrics_current_cv = copy.deepcopy(checkpoint.eval_valid.metrics)
                        last_best_epoch_current_cv = epoch
                        
                        if config['b_logging_active']:
                            Wandb.wandb_log(checkpoint.eval_valid.mean_loss,
                                            checkpoint.eval_valid.metrics,
                                            cust_data.label_classes,
                                            epoch, 'b-Validation',
                                            config
                                            )

                        if config['calc_and_peak_test_error']:
                            eval_test = Eval(Dataloaders.testInd, device, checkpoint.model, loss_function, config['num_out'])
                            Wandb.wandb_log(eval_test.mean_loss, eval_test.metrics, None, 0, None, 'Testset Error', config)
                            
                    else:
                        es_counter += 1
                        print('improvement_identified:', improvement_identified)
                        print('no_overfitting:', no_overfitting)

                    if config['early_stop_patience']:
                        if es_counter >= config['early_stop_patience']:
                            early_stop = True

                    checkpoint.save_checkpoint('checkpoint_latest', epoch, save_path_cv)
                    if epoch != 1:
                        Checkpoint.delete_checkpoint('checkpoint_latest', epoch - 1, save_path_cv)
                    
                    
                    # Print epoch results
                    np.set_printoptions(precision=4)
                    print('')
                    print('Name:          ', config['name'])
                    print('Fold:          ', cv+1, '/', config['num_cv'])
                    print('Epoch:         ', epoch, '/', config['epochs'])
                    print('Duration Epoch:', str(round(duration_epoch, 2)) + 's')
                    
                    print('\nCross-fold Validation information:')
                    print('Training time:  %dh %dm %ds' % (int(duration_cv / 3600), int(np.mod(duration_cv, 3600) / 60), int(np.mod(np.mod(duration_cv, 3600), 60))))
                    print(time.strftime('Current Time:   %d.%m. %H:%M:%S', time.localtime()))
                    print('Loss ValInd:   ', round(checkpoint.eval_valid.mean_loss, config['early_stop_accuracy']))
                    print('Best MCC: ', round(validation_best_metrics_current_cv[8], config['early_stop_accuracy']), 'at Epoch', last_best_epoch_current_cv)
                    Logger.printer('Validation Metrics (tests on validation set):', config['early_stop_accuracy'], checkpoint.eval_valid.metrics, checkpoint.eval_valid.mean_loss)
                    if config['peak_train_error']:
                        Logger.printer('Training Metrics (tests on training set mod 2):', config['early_stop_accuracy'], eval_train.metrics, eval_train.mean_loss)
                    if config['calc_and_peak_test_error']:
                        Logger.printer('Testing Metrics (tests on testing set):', config['early_stop_accuracy'], eval_test.metrics, eval_test.mean_loss)
                        
                    
            if epoch == config['epochs']:
                checkpoint_num = epoch
            else:
                checkpoint_num = epoch - 1
                
            Checkpoint.finalize_latest_checkpoint(checkpoint_num, save_path_cv)
        
        Logger.print_section_line()
        file_log_test_results = save_path_cv / FILE_NAME_TEST_RESULTS
        if not os.path.isfile(file_log_test_results):
            with Logger(file_log_test_results, 'a'):
                print('Testing performance with testset on...')
                test_model('test LAST model in CV ' + str(cv+1), 'checkpoint_last', cust_data.label_classes, save_path_cv, loss_function, config)
                test_model('test BEST model in CV ' + str(cv+1), 'checkpoint_best', cust_data.label_classes, save_path_cv, loss_function, config)
            
                Logger.print_section_line()
                if cv == config['num_cv'] - 1:
                    print('Testing done.')
                else:
                    print('Next Fold...')
                    
        else:
            print('Tests already logged in file:', file_log_test_results)
    
    Logger.print_section_line()
    file_log_test_results_average_best = config.save_path / FILE_NAME_TEST_RESULTS_AVERAGE_BEST
    if not os.path.isfile(file_log_test_results_average_best):
        with Logger(file_log_test_results_average_best, 'a'):
            mean_loss_sum = 0.0
            metrics_sum = [0.0] * 10
            
            for cv in range(config['num_cv']):
                checkpoint = Checkpoint('checkpoint_best', config.save_path / ('cv_' + str(cv + 1)), device, config)
                metrics_sum = [sum(x) for x in zip(metrics_sum, checkpoint.eval_valid_best.metrics)]
                mean_loss_sum += checkpoint.eval_valid.mean_loss
            
            metrics_average = [m / config['num_cv'] for m in metrics_sum]
            mean_loss_average = mean_loss_sum / config['num_cv']
            
            Logger.printer('Testing Metrics Average Best:', config['early_stop_accuracy'], metrics_average, mean_loss_average)
            
            Wandb.init(-1, checkpoint.wandb_id, config)
            Wandb.wandb_log(mean_loss_average, metrics_average, None, 0, None, 'Average Best', config)
            
    else:
        print('Averages already logged in file:', file_log_test_results_average_best)
        
    print('')
        
if __name__ == '__main__':
    args = argparse.ArgumentParser(description='IDDATDLOCT')
    args.add_argument('-cfg', '--config', default=None, type=str, help='config file path (default: None)')
    args.add_argument('-gpu', '--gpus', default='0', type=str, help='indices of GPUs to enable (default: all)')
    args.add_argument('-wb', '--wandb', default=None, type=str, help='Wandb API key (default: None)')
    args.add_argument('-ntt', '--no_trainandtest', dest='trainandtest', action='store_false', help='Deactivation of Training and Testing (default: Activated)')
    args.add_argument('-smp', '--show_samples', dest='show_samples', action='store_true', help='Activate creation of Sample from Data Augmentation (default: Deactivated)')
    args.add_argument('-ycf', '--overwrite_configurations', dest='overwrite_configurations', action='store_false', help='Overwrite Configurations, if config file in this directory already exists. (default: False)')
    args.add_argument('-da', '--data_augmentation', default=None, type=str, help='indices of Data Augmentation techniques to enable (default: None)')
    
    CustomArgs = collections.namedtuple('CustomArgs', 'flags type target')
    options = [
        CustomArgs(['--lr', '--learning_rate'], type=float, target='learning_rate'),
        CustomArgs(['--nm', '--name'], type=str, target='name'),
        CustomArgs(['--gr', '--group'], type=str, target='group'),
        
        # Add more arguments here
    ]
    
    config = Config(args, options)
    
    if config['show_samples']:
        create_samples(config)
    
    if config['trainandtest']:
        num_gpus = len(GPUtil.getAvailable())
        if num_gpus == 0:
            print(f'There is no GPU available.\n')
        else:
            train_and_eval(config)
            
        
        
        
        
        
            
        