"""
Personal reimplementation of 
    Density estimation using Real NVP
(https://arxiv.org/abs/1605.08803)

Useful links:
     - https://uvadlc-notebooks.readthedocs.io/en/latest/tutorial_notebooks/tutorial11/NF_image_modeling.html
"""

import os
import numpy as np
from PIL import Image
from tqdm.auto import tqdm
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
from torch.optim import Adam
from torch.optim.lr_scheduler import LinearLR
from torch.utils.data import DataLoader

from torchvision.datasets import MNIST
from torchvision.utils import make_grid, save_image
from torchvision.transforms import ToTensor, Compose, Lambda

# Seeding
SEED = 0
np.random.seed(SEED)
torch.random.manual_seed(SEED)
torch.use_deterministic_algorithms(True)
torch.backends.cudnn.deterministic = True

def test_reversability(model, x):
    """Tests that x ≈ model.backward(model.forward(x)) and shows images"""
    with torch.no_grad():
        # Running input forward and backward
        z = model.forward(x)[0]
        x_tilda = model.backward(z)[0]
        
        # Printing MSE
        mse = ((x_tilda - x)**2).mean()
        print(f"MSE between input and reconstruction: {mse}")
        
        # Comparing images visually
        plt.imshow(x[0][0].cpu().numpy(), cmap="gray")
        plt.title("Original image")
        plt.show()
        
        plt.imshow(z[0][0].cpu().numpy(), cmap="gray")
        plt.title("After forward pass")
        plt.show()
        
        plt.imshow(x_tilda[0][0].cpu().numpy(), cmap="gray")
        plt.title("Reconstructed image")
        plt.show()

class LayerNormChannels(nn.Module):
    def __init__(self, c_in, eps=1e-5):
        super().__init__()
        self.gamma = nn.Parameter(torch.ones(1, c_in, 1, 1))
        self.beta = nn.Parameter(torch.zeros(1, c_in, 1, 1))
        self.eps = eps
    
    def forward(self, x):
        mean = x.mean(dim=1, keepdim=True)
        var = x.var(dim=1, unbiased=False, keepdim=True)
        y = (x - mean) / torch.sqrt(var + self.eps)
        y = y * self.gamma + self.beta
        return y

class CNNBlock(nn.Module):
    """A simple CNN architecture which will applied at each Affine Coupling step"""
    def __init__(self, n_channels, kernel_size=3):
        super(CNNBlock, self).__init__()
        self.elu = nn.ELU()
        
        self.conv1 = nn.Conv2d(2*n_channels, n_channels, kernel_size, 1, kernel_size//2)
        self.conv2 = nn.Conv2d(2*n_channels, 2*n_channels, 1, 1, 0)
        
    def forward(self, x):
        out = torch.cat((self.elu(x), self.elu(-x)), dim=1)
        out = self.conv1(out)
        out = torch.cat((self.elu(out), self.elu(-out)), dim=1)
        out = self.conv2(out)
        val, gate = out.chunk(2, 1)
        return x + val * torch.sigmoid(gate)
    
class SimpleCNN(nn.Module):
    def __init__(self, blocks=3, channels_in=1, channels_hidden=32, kernel_size=3):
        super(SimpleCNN, self).__init__()
        
        self.elu = nn.ELU()
        self.conv_in = nn.Conv2d(channels_in, channels_hidden, 3, 1, 1)
        self.net = nn.Sequential(*[
            nn.Sequential(
                CNNBlock(channels_hidden, kernel_size),
                LayerNormChannels(channels_hidden)
            )
            for _ in range(blocks)
            ])
        self.conv_out = nn.Conv2d(2*channels_hidden, 2*channels_in, 3, 1, 1)
        
        # Initializing final convolution weights to zeros
        self.conv_out.weight.data.zero_()
        self.conv_out.bias.data.zero_()
        
    def forward(self, x):
        out = self.net(self.conv_in(x))
        out = torch.cat((self.elu(out), self.elu(-out)), dim=1)
        return self.conv_out(out)
        

class Dequantization(nn.Module):
    """Dequantizes the image. Dequantization is the first step for flows, as it allows to not load datapoints
    with high likelihoods and put volume on other input data as well."""
    def __init__(self, max_val):
        super(Dequantization, self).__init__()
        self.max_val = max_val
        self.eps = 1e-5
        
        # Sigmoid and its log det
        self.sigmoid = torch.nn.Sigmoid()
        self.log_det_sigmoid = lambda x: 2*torch.log(self.sigmoid(x)) -x
        
        # Inverse sigmoid and its log det
        self.inv_sigmoid = lambda x: - torch.log((x)**-1 - 1)
        self.log_det_inv_sigmoid = lambda x: torch.log(1 / (x- x**2))
        
    def forward(self, x):
        # Dequantizing input (adding continuous noise in range [0, 1]) and putting in range [0, 1]
        log_det = - np.log(self.max_val) * np.prod(x.shape[1:]) * torch.ones(len(x)).to(x.device)
        out = (x + torch.rand_like(x).detach()) / self.max_val
        
        # Making sure the input is not too close to either 0 or 1 (bounds of inverse sigmoid) --> put closer to 0.5
        log_det += np.log(1-self.eps) * np.prod(x.shape[1:])
        out = (1-self.eps) * out + self.eps * 0.5
       
        # Running the input through the inverse sigmoid function
        log_det += self.log_det_inv_sigmoid(out).sum(dim=[1, 2, 3])
        out = self.inv_sigmoid(out)
        
        return out, log_det
    
    def backward(self, x):
        # Running through the Sigmoid function
        log_det = self.log_det_sigmoid(x).sum(dim=[1, 2, 3])
        out = self.sigmoid(x)
        
        # Undoing the weighted sum
        log_det -= np.log(1-self.eps) * np.prod(x.shape[1:])
        out = (out - self.eps * 0.5) / (1-self.eps)
        
        # Undoing the dequantization
        log_det += np.log(self.max_val) * np.prod(x.shape[1:])
        out *= self.max_val
        out = torch.floor(out).clamp(min=0, max=self.max_val)
        
        return out, log_det

class AffineCoupling(nn.Module):
    """Affine Coupling layer. Only modifies half of the input by running the other half through some non-linear function."""
    def __init__(self, m : nn.Module, modify_x2 = True):
        super(AffineCoupling, self).__init__()
        self.m = m
        self.modify_x2 = modify_x2
        self.s_fac = nn.Parameter(torch.ones(1))
        
    def forward(self, x):
        # Splitting input in two halves
        splitted = x.chunk(2, 1) if self.modify_x2 else x.chunk(2, 1)[::-1]
        x1, x2 = splitted
        
        # Computing scale and shift for x2
        scale, shift = self.m(x1).chunk(2, 1) # Non linear network
        scale = torch.tanh(scale / self.s_fac) * self.s_fac  # Stabilizes training
        
        # Computing output
        y1 = x1
        y2 = torch.exp(scale) * (x2 + shift)
        out = torch.cat((y1, y2), 1)
        
        # Computing log of the determinant of the Jacobian
        log_det_j = torch.sum(scale, dim=[1, 2, 3])
        
        return out, log_det_j
    
    def backward(self, y):
        # Splitting input
        y1, y2 = y.chunk(2, 1)
        
        # Computing scale and shift
        scale, shift = self.m(y1).chunk(2, 1)
        scale = torch.tanh(scale / self.s_fac) * self.s_fac
        
        # Computing inverse transformation
        x1 = y1
        x2 = y2 / torch.exp(scale) - shift
        out = torch.cat((x1, x2), 1) if self.modify_x2 else torch.cat((x2, x1), 1)
        
        # Computing log of the determinant of the Jacobian (for backward tranformation)
        log_det_j = -torch.sum(scale, dim=[1, 2, 3])
        
        return out, log_det_j
    
class Flow(nn.Module):
    """General Flow model. Uses invertible layers to map distributions."""
    def __init__(self, layers):
        super(Flow, self).__init__()
        self.layers = nn.ModuleList(layers)
        
    def forward(self, x):
        # Computing forward pass (images --> gaussian noise)
        out, log_det_j = x, 0
        for layer in self.layers:
            out, log_det_j_layer = layer(out)
            log_det_j += log_det_j_layer
            
        return out, log_det_j
    
    def backward(self, y):
        # Sampling with backward pass (gaussian noise --> images)
        out, log_det_j = y, 0
        for layer in self.layers[::-1]:
            out, log_det_j_layer = layer.backward(out)
            log_det_j += log_det_j_layer
            
        return out, log_det_j
    
def training_loop(model, epochs, lr, wd, loader, device):
    """Trains the model"""
    
    model.train()
    best_loss = float("inf")
    optim = Adam(model.parameters(), lr=lr, weight_decay=wd)
    scheduler = LinearLR(optimizer=optim)
    to_bpd = np.log2(np.exp(1)) / (28*28*1) # Constant that normalizes w.r.t. input shape
    
    for epoch in tqdm(range(epochs), desc="Training progress", colour="#00ff00"):
        epoch_loss = 0.0
        for batch in tqdm(loader, leave=False, desc=f"Epoch {epoch + 1}/{epochs}", colour="#005500"):
            # Getting a batch of images and applying dequantization
            x = batch[0].to(device)
            
            # Running images forward and getting log likelihood (log_px)
            z, log_det_j = model(x)
            log_pz = -np.log(np.sqrt(2*np.pi)) -(z**2).sum(dim=[1,2,3]) # Because we are mapping to a normal N(0, 1)
            log_px = log_pz + log_det_j
            
            # Getting the loss to be optimized (scaling with bits per dimension)
            loss = (-(log_px * to_bpd)).mean()
            
            # Optimization step
            optim.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1) # Clipping gradient norm
            optim.step()
            
            # Logging variable
            epoch_loss += loss.item() / len(loader)
        
        # Stepping with the LR scheduler
        scheduler.step()
        
        # Logging epoch result and storing best model
        log_str = f"Epoch {epoch+1}/{epochs} loss: {epoch_loss:.3f}"
        if best_loss > epoch_loss:
            best_loss = epoch_loss
            log_str += " --> Storing model"
            torch.save(model.state_dict(), "./nf_model.pt")
        print(log_str)

def main():
    # Program arguments
    N_EPOCHS = 100
    LR = 1e-4
    WD = 0.9
    
    # Loading data (images are put in range [0, 255] and are copied on the channel dimension)
    transform = Compose([ToTensor(), Lambda(lambda x: 255 * x.repeat_interleave(2, 0))])
    dataset = MNIST(root='./../datasets', train=True, download=True, transform=transform)
    loader = DataLoader(dataset, batch_size=64, shuffle=True)
    
    # Device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device_log = f"Using device: {device} " + (f"({torch.cuda.get_device_name(device)})" if torch.cuda.is_available() else "")
    print(device_log)
    
    # Creating the model
    model = Flow([
        Dequantization(256),
        AffineCoupling(SimpleCNN(), modify_x2=True),
        AffineCoupling(SimpleCNN(), modify_x2=False),
        AffineCoupling(SimpleCNN(), modify_x2=True),
        AffineCoupling(SimpleCNN(), modify_x2=False),
        AffineCoupling(SimpleCNN(), modify_x2=True),
        AffineCoupling(SimpleCNN(), modify_x2=False),
        AffineCoupling(SimpleCNN(), modify_x2=True),
        AffineCoupling(SimpleCNN(), modify_x2=False)
    ]).to(device)
    
    # Showing number of trainable paramsk
    trainable_params = 0
    for param in model.parameters():
        trainable_params += np.prod(param.shape) if param.requires_grad else 0
    print(f"The model has {trainable_params} trainable parameters.")
    
    # Loading pre-trained model (if any)
    pretrained_exists = os.path.isfile("./nf_model.pt")
    if pretrained_exists:
        model.load_state_dict(torch.load("./nf_model.pt"))
        print("Pre-trained model found and loaded")
    
    # Testing reversability with first image in the dataset
    test_reversability(model, dataset[0][0].unsqueeze(0).to(device))
    
    # Training loop (ony if model doesn't exist)
    if not pretrained_exists:
        training_loop(model, N_EPOCHS, LR, WD, loader, device)
        model.load_state_dict(torch.load("./nf_model.pt"))
        
    # Testing the trained model
    model.eval()
    with torch.no_grad():
        # Mapping the normally distributed noise to new images
        noise = torch.randn(64, 2, 28, 28).to(device)
        images = model.backward(noise)[0]
        images = images.mean(dim=1).unsqueeze(1) # Removing duplicate channel by averaging
    
    save_image(images.float(), "Generated digits.png")
    Image.open("Generated digits.png").show()
    
    # Showing new latent mapping of first image in the dataset
    test_reversability(model, dataset[0][0].unsqueeze(0).to(device))

if __name__ == "__main__":
    main()
