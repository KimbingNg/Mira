import argparse, os, sys, glob
import multiprocessing as mproc
from packaging import version
from omegaconf import OmegaConf
import logging
mainlogger = logging.getLogger('mainlogger')

import torch
import pytorch_lightning as pl
from utils.load_weigths import load_from_pretrainedSD_checkpoint
from collections import OrderedDict
os.chdir(os.path.join(os.getcwd(), '../..'))



def init_workspace(name, logdir, model_config, lightning_config, rank=0, ckpt_logdir=None):
    workdir = os.path.join(logdir, name)
    if ckpt_logdir is None:
        ckptdir = os.path.join(workdir, "checkpoints")
    else:
        ckpt_workdir = os.path.join(ckpt_logdir, name)
        ckptdir = os.path.join(ckpt_workdir, "checkpoints")
    cfgdir = os.path.join(workdir, "configs")
    loginfo = os.path.join(workdir, "loginfo")

    # Create logdirs and save configs (all ranks will do to avoid missing directory error if rank:0 is slower)
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(ckptdir, exist_ok=True)
    os.makedirs(cfgdir, exist_ok=True)
    os.makedirs(loginfo, exist_ok=True)

    if rank == 0:
        if "callbacks" in lightning_config and 'metrics_over_trainsteps_checkpoint' in lightning_config.callbacks:
            os.makedirs(os.path.join(ckptdir, 'trainstep_checkpoints'), exist_ok=True)
        OmegaConf.save(model_config, os.path.join(cfgdir, "model.yaml"))
        OmegaConf.save(OmegaConf.create({"lightning": lightning_config}), os.path.join(cfgdir, "lightning.yaml"))
    return workdir, ckptdir, cfgdir, loginfo


def check_config_attribute(config, name):
    if name in config:
        value = getattr(config, name)
        return value
    else:
        return None


def get_trainer_callbacks(lightning_config, config, logdir, ckptdir, logger):
    default_callbacks_cfg = {
        "model_checkpoint": {
            "target": "pytorch_lightning.callbacks.ModelCheckpoint",
            "params": {
                "dirpath": ckptdir,
                "filename": "{epoch}",
                "verbose": True,
                "save_last": False,
            }
        },
        "batch_logger": {
            "target": "callbacks.ImageLogger",
            "params": {
                "save_dir": logdir,
                "batch_frequency": 1000,
                "max_images": 4,
                "clamp": True,
            }
        },    
        "learning_rate_logger": {
            "target": "pytorch_lightning.callbacks.LearningRateMonitor",
            "params": {
                "logging_interval": "step",
                "log_momentum": False
            }
        },
        "cuda_callback": {
            "target": "callbacks.CUDACallback"
        },
    }

    ## optional setting for saving checkpoints
    monitor_metric = check_config_attribute(config.model.params, "monitor")
    if monitor_metric is not None:
        mainlogger.info(f"Monitoring {monitor_metric} as checkpoint metric.")
        default_callbacks_cfg["model_checkpoint"]["params"]["monitor"] = monitor_metric
        default_callbacks_cfg["model_checkpoint"]["params"]["save_top_k"] = 3
        default_callbacks_cfg["model_checkpoint"]["params"]["mode"] = "min"

    if 'metrics_over_trainsteps_checkpoint' in lightning_config.callbacks:
        mainlogger.info('Caution: Saving checkpoints every n train steps without deleting. This might require some free space.')
        default_metrics_over_trainsteps_ckpt_dict = {
            'metrics_over_trainsteps_checkpoint': {"target": 'pytorch_lightning.callbacks.ModelCheckpoint',
                                                   'params': {
                                                        "dirpath": os.path.join(ckptdir, 'trainstep_checkpoints'),
                                                        "filename": "{epoch}-{step}",
                                                        "verbose": True,
                                                        'save_top_k': -1,
                                                        'every_n_train_steps': 10000,
                                                        'save_weights_only': True
                                                    }
                                                }
        }
        default_callbacks_cfg.update(default_metrics_over_trainsteps_ckpt_dict)

    if "callbacks" in lightning_config:
        callbacks_cfg = lightning_config.callbacks
    else:
        callbacks_cfg = OmegaConf.create()
    callbacks_cfg = OmegaConf.merge(default_callbacks_cfg, callbacks_cfg)

    return callbacks_cfg


def get_trainer_logger(lightning_config, logdir, on_debug):
    default_logger_cfgs = {
        "tensorboard": {
            "target": "pytorch_lightning.loggers.TensorBoardLogger",
            "params": {
                "save_dir": logdir,
                "name": "tensorboard",
            }
        },
        "testtube": {
            "target": "pytorch_lightning.loggers.CSVLogger",
            "params": {
                    "name": "testtube",
                    "save_dir": logdir,
                }
            },
    }
    os.makedirs(os.path.join(logdir, "tensorboard"), exist_ok=True)
    default_logger_cfg = default_logger_cfgs["tensorboard"]
    if "logger" in lightning_config:
        logger_cfg = lightning_config.logger
    else:
        logger_cfg = OmegaConf.create()
    logger_cfg = OmegaConf.merge(default_logger_cfg, logger_cfg)
    return logger_cfg


def get_trainer_strategy(lightning_config):
    default_strategy_dict = {
        "target": "pytorch_lightning.strategies.DDPShardedStrategy"
    }
    if "strategy" in lightning_config:
        strategy_cfg = lightning_config.strategy
        return strategy_cfg
    else:
        strategy_cfg = OmegaConf.create()

    strategy_cfg = OmegaConf.merge(default_strategy_dict, strategy_cfg)
    return strategy_cfg


def load_checkpoints(model, model_cfg):
    ## special load setting for adapter training
    if check_config_attribute(model_cfg, "adapter_only"):
        pretrained_ckpt = model_cfg.pretrained_checkpoint
        assert os.path.exists(pretrained_ckpt), "Error: Pre-trained checkpoint NOT found at:%s"%pretrained_ckpt
        mainlogger.info(">>> Load weights from pretrained checkpoint (training adapter only)")
        ## only load weight for the backbone model (e.g. latent diffusion model)
        state_dict = torch.load(pretrained_ckpt, map_location=f"cpu")
        if "state_dict" in list(state_dict.keys()):
            state_dict = state_dict["state_dict"]
        model.load_state_dict(state_dict, strict=False)
        model.empty_paras = None
        return model
    empty_paras = None
    if check_config_attribute(model_cfg, 'train_temporal'):
        mainlogger.info(">>> Load weights from [Stable Diffusion] checkpoint")
        model_pretrained, empty_paras = get_empty_params_comparedwith_sd(model, model_cfg)
        model = model_pretrained


    if check_config_attribute(model_cfg, "pretrained_checkpoint"):
        pretrained_ckpt = model_cfg.pretrained_checkpoint
        assert os.path.exists(pretrained_ckpt), "Error: Pre-trained checkpoint NOT found at:%s"%pretrained_ckpt
        mainlogger.info(">>> Load weights from pretrained checkpoint")
        # print(f"Loading model from {ckpt}")
        pl_sd = torch.load(pretrained_ckpt, map_location="cpu")
        try:
            if 'state_dict' in pl_sd.keys():
                if 'scale_arr' in pl_sd['state_dict'].keys():
                    del pl_sd['state_dict']['scale_arr']
                for key in list(pl_sd['state_dict'].keys()):
                    if 'ori' in key:
                        del pl_sd['state_dict'][key]
                if check_config_attribute(model_cfg, "lora_spatial_lora"):
                    ## adapting to standard 2d network: modify the key name because of the add of temporal-attention
                    mapping_dict = {
                        'middle_block.2': 'middle_block.3',
                        'output_blocks.5.2': 'output_blocks.5.3',
                        'output_blocks.8.2': 'output_blocks.8.3',
                    }
                    cnt = 0
                    for k in list(pl_sd["state_dict"].keys()):
                        for src_word, dst_word in mapping_dict.items():
                            if src_word in k:
                                new_key = k.replace(src_word, dst_word)
                                pl_sd["state_dict"][new_key] = pl_sd["state_dict"][k]
                                del pl_sd["state_dict"][k]
                                cnt += 1
                    mainlogger.info(f'[load checkpoint: renamed {cnt} source keys to match target model]')

                model.load_state_dict(pl_sd["state_dict"],strict=True)
            else:       
                # deepspeed
                new_pl_sd = OrderedDict()
                for key in pl_sd['module'].keys():
                    new_pl_sd[key[16:]]=pl_sd['module'][key]
                # if 'scale_arr' in new_pl_sd.keys():
                #     del new_pl_sd['scale_arr']
                # for key in list(new_pl_sd.keys()):
                #     if 'ori' in key:
                #         del new_pl_sd[key]
                model.load_state_dict(new_pl_sd, strict=True)

        except:
            mainlogger.info(f'[!!!!!!################ Strict loading failed. Try to load compatible ONLY ############### !!!!!!!!!!!! ]')

            model.load_state_dict(pl_sd, strict=False)
        '''
        try:
            model = model.load_from_checkpoint(pretrained_ckpt, **model_cfg.params)
        except:
            mainlogger.info("[Warning] checkpoint NOT complete matched. To adapt by skipping ...")
            state_dict = torch.load(pretrained_ckpt, map_location=f"cpu")
            if "state_dict" in list(state_dict.keys()):
                state_dict = state_dict["state_dict"]
            model_state_dict = model.state_dict()
            ## for layer with channel changed (e.g. GEN 1's conditon-concatenating setting)
            for n, p in model_state_dict.items():
                if p.shape != state_dict[n].shape:
                    mainlogger.info(f"Skipped parameter [{n}] from pretrained! ")
                    state_dict.pop(n)
            model_state_dict.update(state_dict)
            model.load_state_dict(model_state_dict)    
        empty_paras = None
        '''
    # else:
    #     mainlogger.info(">>> Load weights from [Stable Diffusion] checkpoint")
    #     model_pretrained, empty_paras = get_empty_params_comparedwith_sd(model, model_cfg)
    #     model = model_pretrained

    ## record empty params
    model.empty_paras = empty_paras
    return model

def get_empty_params_comparedwith_sd(model, model_cfg):
    sd_ckpt = model_cfg.sd_checkpoint if hasattr(model_cfg, 'sd_checkpoint') else None
    if sd_ckpt and os.path.exists(sd_ckpt):
        expand_to_3d = model_cfg.expand_to_3d if "expand_to_3d" in model_cfg else False
        adapt_keyname = True if not expand_to_3d and getattr(model_cfg.params.unet_config.params, "temporal_attention", True)  else False # for lat
        model_pretrained, empty_paras = load_from_pretrainedSD_checkpoint(model,
                expand_to_3d=expand_to_3d, adapt_keyname=adapt_keyname, pretained_ckpt=sd_ckpt)
        empty_paras = [n.lstrip("model").lstrip(".") for n in empty_paras]
        return model_pretrained, empty_paras
    else:
        print("Skip loadding SD pretrain")
        return  model, []


def get_autoresume_path(logdir):
    print(os.getcwd())
    ckpts = sorted([f for f in os.listdir(os.path.join(logdir, "checkpoints"))])
    if "trainstep_checkpoints" in ckpts:
        ckpts.remove("trainstep_checkpoints")
    if "last.ckpt" in ckpts:
        ckpts.remove("last.ckpt")
    if len(ckpts) >0:
        mainlogger.info(f"all avaible checkpoints: {ckpts}")
        ckpt_path = ckpts[-1]
        ckpt = os.path.join(logdir, "checkpoints", ckpt_path)
        mainlogger.info(f"Select resuming ckpt: {ckpt}")
        resume_checkpt_path = ckpt
        mainlogger.info(f"[INFO] resume from: {ckpt}")
    else:
        resume_checkpt_path = None
        mainlogger.info(f"[INFO] no checkpoint found in current workspace: {os.path.join(logdir, 'checkpoints')}")
    
    return resume_checkpt_path


def set_logger(logfile, name='mainlogger'):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(logfile, mode='w')
    fh.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s-%(levelname)s: %(message)s"))
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger
