import sys


# Path to Pix2Pix directory
local_path = '/drive/MelGan-Imputation/'

# Path to libraries directory
sys.path.append('/drive/MelGan-Imputation/libraries')

import os
import numpy as np
import math
import itertools
import time
import datetime
import sys
import pickle
from multiprocessing import Pool
import resource
from tqdm import tqdm



import torchvision.transforms as transforms
from torchvision.utils import save_image

from torch.utils.data import DataLoader
from torchvision import datasets
from torch.autograd import Variable

import torch.nn as nn
import torch.nn.functional as F
import torch


#from models.pix2pix import *
from models.MelGAN import *
from models.unet import *
from mlp import audio
from mlp import normalization
from mlp import utils as mlp
from mlp.WaveDatasetRaw import MusicDataset


np.random.seed(40)
torch.manual_seed(40)

start_epoch = 0 # epoch to start training from
n_epochs = 3000 # number of epochs of training
dataset_name = 'MUSDB-18' # name of the dataset
batch_size = 4 # size of the batches
lr = 0.0001 # adam: learning rate
b1 = 0.5 # adam: decay of first order momentum of gradient
b2 = 0.9 # adam: decay of first order momentum of gradient
decay_epoch = 100 # epoch from which to start lr decay
n_cpu = 4 # number of cpu threads to use during batch generation
img_height = 128 # size of image height
img_width = 128 # size of image width
channels = 1 # number of image channels
sample_interval = 100 # interval between sampling of images from generators
checkpoint_interval = 100 # interval between model checkpoints
n_layers_D = 4
num_D = 4

cuda = True if torch.cuda.is_available() else False
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
#resource.setrlimit(resource.RLIMIT_NOFILE, (4096, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))

n_mel_channels = 80
ngf = 32
n_residual_layers = 6

num_D = 3
ndf = 16
n_layers_D = 6
downsamp_factor = 4
lambda_feat = 10
save_interval = 20
log_interval = 100
experiment_dir = 'saves_623'

netG = GeneratorMel(n_mel_channels, ngf, n_residual_layers).cuda()
netD = DiscriminatorMel(
        num_D, ndf, n_layers_D, downsamp_factor
    ).cuda()
fft = Audio2Mel(n_mel_channels=n_mel_channels).cuda()

optG = torch.optim.Adam(netG.parameters(), lr=1e-4, betas=(0.5, 0.9))
optD = torch.optim.Adam(netD.parameters(), lr=1e-4, betas=(0.5, 0.9))

dirty_path ='/drive/MelGan-Imputation/datasets/demucs_train_flattened_raw'
clean_path ='/drive/MelGan-Imputation/datasets/original_train_sources_raw'

train_dirty = []
train_clean = []
val_dirty = []
val_clean = []
dirty_data = [elem for elem in os.listdir(dirty_path) if "drum" in elem]

for s in dirty_data:
  if np.random.rand() < .9:
    train_dirty.append(dirty_path + '/' + s)
    train_clean.append(clean_path +'/' + s)
  else:
    val_dirty.append(dirty_path +'/' + s)
    val_clean.append(clean_path + '/' + s)

fs = 48000
bs = batch_size
stroke_width = 32
patch_width = img_width
patch_height = img_height
nperseg = 256

# ds_valid = MusicDataset(val_clean,val_dirty,44100,44100)
# ds_train = MusicDataset(train_clean,train_dirty,44100,44100)
ds_valid = MusicDataset(val_dirty, val_clean, 44100,44100)
ds_train = MusicDataset(train_dirty, train_clean, 44100, 44100)


valid_loader = DataLoader(ds_valid, batch_size=bs, num_workers=4, shuffle=False)
train_loader = DataLoader(ds_train, batch_size=bs, num_workers=4, shuffle=True)

from torch.utils.tensorboard import SummaryWriter

writer = SummaryWriter()
costs = []
start = time.time()

if start_epoch > 0:
    netG.load_state_dict(torch.load(local_path + experiment_dir +  str(start_epoch) + "netG.pt"))
    netD.load_state_dict(torch.load(local_path + experiment_dir +  str(start_epoch) + "netD.pt"))
    optG.load_state_dict(torch.load(local_path + experiment_dir +  str(start_epoch) + "optG.pt").state_dict())
    optD.load_state_dict(torch.load(local_path + experiment_dir +  str(start_epoch) + "optD.pt").state_dict())


results = []
netG.train()
netD.train()
dis_train = 0
steps = 0
for epoch in range(start_epoch, n_epochs):
    if epoch % 100 == 0 and epoch != start_epoch:
      torch.save(netG.state_dict(), local_path + experiment_dir +  str(epoch) + "netG.pt")
      torch.save(netD.state_dict(), local_path + experiment_dir +  str(epoch) + "netD.pt")
      torch.save(optG, local_path + experiment_dir +  str(epoch) + "optG.pt")
      torch.save(optD, local_path + experiment_dir +  str(epoch) + "optD.pt")
      #torch.save(writer, local_path +'saves2/' +  str(epoch) + "writer")
    for iterno, x_t in enumerate(train_loader):
        x_t_0 = x_t[0].unsqueeze(1).float().cuda()
        x_t_1 = x_t[1].unsqueeze(1).float().cuda()
        s_t = fft(x_t_0).detach()
        x_pred_t = netG(s_t.cuda())
        with torch.no_grad():
            s_pred_t = fft(x_pred_t.detach())
            s_error = F.l1_loss(s_t, s_pred_t).item()
        #######################
        # Train Discriminator #
        #######################

        x_t_1 = x_t_1[:, :, :44032]
        D_fake_det = netD(x_pred_t.cuda().detach())
        D_real = netD(x_t_1.cuda())

        loss_D = 0
        for scale in D_fake_det:
            loss_D += F.relu(1 + scale[-1]).mean()
        for scale in D_real:
            loss_D += F.relu(1 - scale[-1]).mean()
        netD.zero_grad()
        loss_D.backward()
        optD.step()
        ###################
        # Train Generator #
        ###################
        D_fake = netD(x_pred_t.cuda())
        loss_G = 0
        for scale in D_fake:
            loss_G += -scale[-1].mean()
        loss_feat = 0
        feat_weights = 4.0 / (n_layers_D + 1)
        D_weights = 1.0 / num_D
        wt = D_weights * feat_weights
        for i in range(num_D):
            for j in range(len(D_fake[i]) - 1):
                loss_feat += wt * F.l1_loss(D_fake[i][j], D_real[i][j].detach())
        netG.zero_grad()
        (loss_G + lambda_feat * loss_feat).backward()
        optG.step()
        ######################
        # Update tensorboard #
        ######################
        costs = [[loss_D.item(), loss_G.item(), loss_feat.item(), s_error]]
        writer.add_scalar("loss/discriminator", costs[-1][0], steps)
        writer.add_scalar("loss/generator", costs[-1][1], steps)
        writer.add_scalar("loss/feature_matching", costs[-1][2], steps)
        writer.add_scalar("loss/mel_reconstruction", costs[-1][3], steps)
        steps += 1

        sys.stdout.write(f'\r[Epoch {epoch}, Batch {iterno}]: [Generator Loss: {costs[-1][1]:.4f}] [Discriminator Loss: {costs[-1][0]:.4f}] [Feature Loss: {costs[-1][2]:.4f}] [Reconstruction Loss: {costs[-1][3]:.4f}] ')

