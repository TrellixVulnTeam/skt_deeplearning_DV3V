import numpy as np
import torch
from torch.optim import Adam
from torch.utils.data import DataLoader
import tensorboardX
import sacred
from sacred.observers import FileStorageObserver

import pprint
import collections
import re
import os

from torchvision import transforms
from PIL import Image

from sktdl_utils import weight_init
from sktdl_cells.iou import calc_iou as calc_iou_vanilla
from sktdl_cells import trainable_params as param_selection
from sktdl_cells.data_cells import CellsSegmentation, CellsTransform
from sktdl_cells.trainloop_segmentation import train
from sktdl_cells.losses import dice_loss as kevinzakka_diceloss
from sktdl_cells.diceloss_rogertrullo import dice_loss as rogertrullo_diceloss
from sktdl_cells.diceloss_issamlaradji import dice_loss as issamlaradji_diceloss
from sktdl_cells.model import UnetAsExtractor
from pytorch_unet.unet.unet_model import UNet
from pytorch_unet.dice_loss import dice_coeff as pytorch_unet_dicecoeff


MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(MODULE_DIR, 'unet_as_extractor_runs')


ex = sacred.Experiment('sktdl_cells_unet_as_extractor')
ex.observers.append(FileStorageObserver.create(RUNS_DIR))


@ex.capture
def get_trainable_named_params(net, trainable_params):
    METHODS = dict(
            fixed=param_selection.fixed,
            headtail=param_selection.headtail
            )
    paramsets = (METHODS[tp[0]](*tp[1:])(net) for tp in trainable_params)
    params = ((n, p) for pset in paramsets for n, p in pset)
    params = collections.OrderedDict(params)
    return params.items()

def get_trainable_params(net):
    return (p for n, p in get_trainable_named_params(net))

@ex.capture
def make_loss(loss_impl):
    LOSSES = dict(
            rogertrullo=lambda yhat, y: rogetrullo_diceloss(yhat, y.float()),
            issamlaradji=lambda yhat, y: issamlaradji_diceloss(yhat, y.float()),
            pytorch_unet=lambda yhat, y: 1. - pytorch_unet_dicecoeff(yhat, y.float()),
            )
    return LOSSES[loss_impl]

@ex.capture
def make_iou(iou_impl, threshold):
    IMPL = dict(
            vanilla=lambda y_pred, y: (
                calc_iou_vanilla(
                    (y_pred > threshold).to('cpu').numpy(),
                    y.to('cpu').numpy())),
            custom=lambda y_pred, y: (
                float(
                    ((y_pred > threshold) * (y > 0))
                    .sum())
                / float(
                    ((y > 0) | (y_pred > threshold))
                    .sum())
            ))
    return IMPL[iou_impl]

@ex.capture
def make_model(unet_weights, full_weights, device, trainable_params, random_init):
    net = UnetAsExtractor()
    net.apply(weight_init) # in case all weight paths are None
    net_random = UnetAsExtractor()
    net_random.apply(weight_init) # so we can reset trainable params later
    if unet_weights is not None:
        print(f'Loading {unet_weights}')
        state = torch.load(unet_weights, map_location='cpu')
        net.unet.load_state_dict(state)
    if full_weights is not None:
        print(f'Loading {unet_weights}')
        full_state = torch.load(full_weights, map_location='cpu')
        net.load_state_dict(state)
    for p in net.parameters():
        p.requires_grad_(False)
    for p, p_rnd in zip(get_trainable_params(net), get_trainable_params(net_random)):
        if random_init:
            p.data.copy_(p_rnd.data)
        p.requires_grad_(True)
    net.to(torch.device(device))
    return net

@ex.capture
def make_data(subset, batch_size, clone_times, train_transform):
    train = subset == 'train'
    transform = CellsTransform(**train_transform) if train else None
    cells = CellsSegmentation(
            subset,
            clone_times=clone_times if train else 1,
            xy_transform=transform)
    return DataLoader(
            cells,
            batch_size=batch_size)

@ex.capture
def make_optimizer(model, adam_params):
    params = get_trainable_params(model)
    optimizer = Adam(params, **adam_params)
    return optimizer

@ex.config
def cfg0():
    clone_times=500
    unet_weights = os.path.join(MODULE_DIR, 'pytorch_unet.pth')
    full_weights = None
    batch_size=50
    num_input_channels = 11
    device = "cuda" if torch.cuda.is_available() else "cpu"
    trainable_params = [('headtail', 4, 2)]
    adam_params = dict(
            lr=1e-3,
            betas=(.9, .99))
    num_epochs = 5
    train_transform = dict(
            degrees=180.,
            translate=(0.25, 0.25),
            scale=(2/3, 3/2),
            crop_size=(150, 150))
    epochs_per_checkpoint = 2
    loss_impl = 'issamlaradji'
    iou_impl = 'custom'
    threshold = .49
    random_init = True

@ex.command(unobserved=True)
def print_parameternames():
    net = make_model()
    output = (
            '\n'.join(
                '{:40}: {}'.format(
                    parname,
                    'trainable' if p.requires_grad else 'frozen')
                for parname, p in net.named_parameters()))
    print(output)

@ex.command(unobserved=True)
def segment_dir(path, batch_size, device):
    IMAGE_EXT = re.compile(r'^.*(?<!segmented)(\.jpg|\.png|\.bmp)$')
    device = torch.device(device)
    cpu = torch.device('cpu')
    model = make_model(trainable_params=[])
    model.eval()
    cells = [
            os.path.join(dirname, filename)
            for dirname, _, filenames in os.walk(path)
            for filename in filenames
            if IMAGE_EXT.match(filename)
            ]
    with torch.no_grad():
        for filepath in cells:
            X = transforms.functional.to_tensor(Image.open(filepath).convert('RGB'))
            X = X.to(device)
            X = X.unsqueeze(0)
            y = model(X).squeeze(0).squeeze(0).to(cpu).numpy()
            y = (y * 255).astype(np.uint8)
            y = Image.fromarray(y)
            y.save(filepath + '.segmented.bmp')



@ex.automain
def main(device, num_epochs, epochs_per_checkpoint, _run):
    model = make_model()
    dataloader_train = make_data('train')
    dataloader_val = make_data('val')
    optimizer = make_optimizer(model)
    device = torch.device(device)
    loss = make_loss()
    iou = make_iou()
    EXPERIMENT_DIR = os.path.join(RUNS_DIR, str(_run._id))
    tensorboard = tensorboardX.SummaryWriter(EXPERIMENT_DIR)
    def log(subset, name, value, it):
        tensorboard.add_scalar(f'{subset}.{name}', value, it)
    train(model,
          dataloader_train,
          dataloader_val,
          optimizer,
          loss,
          iou,
          device,
          num_epochs,
          log=log,
          weights_dir=EXPERIMENT_DIR,
          epochs_per_checkpoint=epochs_per_checkpoint)
