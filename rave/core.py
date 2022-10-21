import filecmp
import os
from pathlib import Path
from random import random

import gin
import GPUtil as gpu
import librosa as li
import numpy as np
import torch
import torch.fft as fft
import torch.nn as nn
import udls
import udls.transforms as transforms
import yaml
from einops import rearrange
from scipy.signal import lfilter
from torch.utils.data import random_split
from tqdm import tqdm


@gin.configurable
def simple_audio_preprocess(sampling_rate, N, crop=False, trim_silence=False, n_channels=1):

    def preprocess(name):
        try:
            x, sr = li.load(name, mono=False, sr=sampling_rate)
            if n_channels != x.shape[0]:
                if x.shape[0] == 1:
                    x = np.repeat(x, n_channels, axis=0)
                else:
                    raise ValueError("cannot convert a signal with %d channels to %d channels"%(x.shape[0], n_channels))
        except KeyboardInterrupt:
            exit()
        except Exception as e:
            print(e)
            return None

        if trim_silence:
            try:
                x = np.concatenate(
                    [x[:, e[0]:e[1]] for e in li.effects.split(x, 50)],
                    -1,
                )
            except Exception as e:
                print(e)
                return None

        if crop:
            crop_size = x.shape[1] % N
            if crop_size:
                x = x[:, :-crop_size]
        else:
            pad = (N - (x.shape[1] % N)) % N
            x = np.pad(x, ((0, 0), (0, pad)))

        if not x.shape[1]:
            return None

        x = x.reshape(-1, x.shape[0], N)
        return x.astype(np.float16)

    return preprocess

def mod_sigmoid(x):
    return 2 * torch.sigmoid(x)**2.3 + 1e-7


def copy_config(source, destination):
    if os.path.exists(destination):
        assert filecmp.cmp(
            source, destination
        ), "Same run, incompatible configuration. Choose a different name !"
        return
    with open(source, "r") as source:
        with open(destination, "w") as destination:
            for l in source.read():
                destination.write(l)


@gin.configurable
def multiscale_stft(signal, scales, overlap):
    """
    Compute a stft on several scales, with a constant overlap value.
    Parameters
    ----------
    signal: torch.Tensor
        input signal to process ( B X C X T )
    
    scales: list
        scales to use
    overlap: float
        overlap between windows ( 0 - 1 )
    """
    signal = rearrange(signal, "b c t -> (b c) t")
    stfts = []
    for s in scales:
        S = torch.stft(
            signal,
            s,
            int(s * (1 - overlap)),
            s,
            torch.hann_window(s).to(signal),
            True,
            normalized=True,
            return_complex=True,
        ).abs()
        stfts.append(S)
    return stfts


def random_angle(min_f=20, max_f=8000, sr=24000):
    min_f = np.log(min_f)
    max_f = np.log(max_f)
    rand = np.exp(random() * (max_f - min_f) + min_f)
    rand = 2 * np.pi * rand / sr
    return rand


def pole_to_z_filter(omega, amplitude=.9):
    z0 = amplitude * np.exp(1j * omega)
    a = [1, -2 * np.real(z0), abs(z0)**2]
    b = [abs(z0)**2, -2 * np.real(z0), 1]
    return b, a


def random_phase_mangle(x, min_f, max_f, amp, sr):
    angle = random_angle(min_f, max_f, sr)
    b, a = pole_to_z_filter(angle, amp)
    return lfilter(b, a, x)


@gin.configurable
class Loudness(nn.Module):

    def __init__(self, sr, block_size, n_fft=2048):
        super().__init__()
        self.sr = sr
        self.block_size = block_size
        self.n_fft = n_fft

        f = np.linspace(0, sr / 2, n_fft // 2 + 1) + 1e-7
        a_weight = li.A_weighting(f).reshape(-1, 1)

        self.register_buffer("a_weight", torch.from_numpy(a_weight).float())
        self.register_buffer("window", torch.hann_window(self.n_fft))

    def forward(self, x):
        x = x.reshape(-1, x.shape[-1])
        x = torch.stft(
            x,
            self.n_fft,
            self.block_size,
            self.n_fft,
            center=True,
            window=self.window,
            return_complex=True,
        ).abs()
        x = torch.log(x + 1e-7) + self.a_weight
        return torch.mean(x, 1, keepdim=True)


def amp_to_impulse_response(amp, target_size):
    """
    transforms frequecny amps to ir on the last dimension
    """
    amp = torch.stack([amp, torch.zeros_like(amp)], -1)
    amp = torch.view_as_complex(amp)
    amp = fft.irfft(amp)

    filter_size = amp.shape[-1]

    amp = torch.roll(amp, filter_size // 2, -1)
    win = torch.hann_window(filter_size, dtype=amp.dtype, device=amp.device)

    amp = amp * win

    amp = nn.functional.pad(
        amp,
        (0, int(target_size) - int(filter_size)),
    )
    amp = torch.roll(amp, -filter_size // 2, -1)

    return amp


def fft_convolve(signal, kernel):
    """
    convolves signal by kernel on the last dimension
    """
    signal = nn.functional.pad(signal, (0, signal.shape[-1]))
    kernel = nn.functional.pad(kernel, (kernel.shape[-1], 0))

    output = fft.irfft(fft.rfft(signal) * fft.rfft(kernel))
    output = output[..., output.shape[-1] // 2:]

    return output


def search_for_run(run_path, mode="last"):
    if run_path is None: return None
    if ".ckpt" in run_path: return run_path
    ckpts = map(str, Path(run_path).rglob("*.ckpt"))
    ckpts = filter(lambda e: mode in e, ckpts)
    ckpts = sorted(ckpts)
    if len(ckpts): return ckpts[-1]
    else: return None


def get_dataset(data_dir, preprocess_dir, sr, n_signal, n_channels=1):
    dataset = udls.SimpleDataset(
        preprocess_dir,
        data_dir,
        preprocess_function=simple_audio_preprocess(sr, 2 * n_signal, n_channels=n_channels),
        split_set="full",
        extension="*.wav,*.aif,*.mp3,*.aiff",
        transforms=transforms.Compose([
            lambda x: x.astype(np.float32),
            lambda x: np.stack([transforms.RandomCrop(n_signal)(x[i]) for i in range(len(x))], 0),
            transforms.RandomApply(
                lambda x: random_phase_mangle(x, 20, 2000, .99, sr),
                p=.8,
            ),
            # transforms.Dequantize(16),
            lambda x: x + np.random.random(x.shape) / 2**16, 
            lambda x: x.astype(np.float32),
        ]),
    )

    return dataset


def split_dataset(dataset, percent):
    split1 = max((percent * len(dataset)) // 100, 1)
    split2 = len(dataset) - split1
    split1, split2 = random_split(
        dataset,
        [split1, split2],
        generator=torch.Generator().manual_seed(42),
    )
    return split1, split2


def setup_gpu():
    return gpu.getAvailable(maxMemory=.05)


def get_beta_kl(step, warmup, min_beta, max_beta):
    if step > warmup: return max_beta
    t = step / warmup
    min_beta_log = np.log(min_beta)
    max_beta_log = np.log(max_beta)
    beta_log = t * (max_beta_log - min_beta_log) + min_beta_log
    return np.exp(beta_log)


def get_beta_kl_cyclic(step, cycle_size, min_beta, max_beta):
    return get_beta_kl(step % cycle_size, cycle_size // 2, min_beta, max_beta)


def get_beta_kl_cyclic_annealed(step, cycle_size, warmup, min_beta, max_beta):
    min_beta = get_beta_kl(step, warmup, min_beta, max_beta)
    return get_beta_kl_cyclic(step, cycle_size, min_beta, max_beta)


@gin.register
def hinge_gan(score_real, score_fake):
    loss_dis = torch.relu(1 - score_real) + torch.relu(1 + score_fake)
    loss_dis = loss_dis.mean()
    loss_gen = -score_fake.mean()
    return loss_dis, loss_gen


@gin.register
def ls_gan(score_real, score_fake):
    loss_dis = (score_real - 1).pow(2) + score_fake.pow(2)
    loss_dis = loss_dis.mean()
    loss_gen = (score_fake - 1).pow(2).mean()
    return loss_dis, loss_gen


@gin.register
def nonsaturating_gan(score_real, score_fake):
    score_real = torch.clamp(torch.sigmoid(score_real), 1e-7, 1 - 1e-7)
    score_fake = torch.clamp(torch.sigmoid(score_fake), 1e-7, 1 - 1e-7)
    loss_dis = -(torch.log(score_real) + torch.log(1 - score_fake)).mean()
    loss_gen = -torch.log(score_fake).mean()
    return loss_dis, loss_gen


@torch.enable_grad()
def get_rave_receptive_field(model, n_channels=1):
    N = 2**15
    model.eval()
    device = next(iter(model.parameters())).device
    while True:
        x = torch.randn(n_channels, 1, N, requires_grad=True, device=device)
        x_tmp = model.pqmf(x)
        x_tmp = x_tmp.reshape(1, -1, x_tmp.shape[-1])
        z = model.encoder(x_tmp)[:, :model.latent_size]
        y = model.decoder(z)
        y = y.reshape(n_channels, -1, y.shape[-1])
        y = model.pqmf.inverse(y)
        y = y.reshape(1, n_channels, -1)

        y[0, 0, N // 2].backward()
        assert x.grad is not None, "input has no grad"

        grad = x.grad.data.reshape(-1)
        left_grad, right_grad = grad.chunk(2, 0)
        large_enough = (left_grad[0] == 0) and right_grad[-1] == 0
        if large_enough:
            break
        else:
            N *= 2
    left_receptive_field = len(left_grad[left_grad != 0])
    right_receptive_field = len(right_grad[right_grad != 0])
    model.zero_grad()
    return left_receptive_field, right_receptive_field


def valid_signal_crop(x, left_rf, right_rf):
    dim = x.shape[1]
    x = x[..., left_rf.item() // dim:]
    if right_rf.item():
        x = x[..., :-right_rf.item() // dim]
    return x


@torch.no_grad()
def extract_codes(model, loader, out_path):
    os.makedirs(out_path, exist_ok=True)
    device = next(iter(model.parameters())).device
    code = model.encode

    x = next(iter(loader))
    x = x.unsqueeze(1).to(device)
    batch_size, n_code, n_frame = code(x).shape

    out_array = np.memmap(
        os.path.join(out_path, "data.npy"),
        dtype='uint16',
        mode='w+',
        shape=(
            len(loader) * batch_size,
            n_code,
            n_frame,
        ),
    )

    for i, x in enumerate(tqdm(loader, desc="Extracting codes")):
        x = x.unsqueeze(1).to(device)
        index = code(x).cpu().numpy().astype(np.uint16)
        out_array[i * batch_size:(i + 1) * batch_size] = index

    out_array.flush()
    with open(os.path.join(out_path, "info.yaml"), "w") as info:
        yaml.safe_dump({"shape": out_array.shape}, info)


@gin.configurable
def lin_distance(x, y):
    return torch.norm(x - y) / torch.norm(x)


@gin.configurable
def log_distance(x, y, epsilon):
    return abs(torch.log(x + epsilon) - torch.log(y + epsilon)).mean()


def multiscale_spectral_distance(x, y):
    x = multiscale_stft(x)
    y = multiscale_stft(y)

    lin = sum(list(map(lin_distance, x, y)))
    log = sum(list(map(log_distance, x, y)))

    return lin + log


def check_scripted_model(model: nn.Module, buffer_size=8192):
    checked_methods = []
    for n, b in model.named_buffers():
        if "_params" in n:
            method = n[:-7]
            n_in, ratio_in, n_out, ratio_out = b.numpy()
            x = torch.zeros(1, n_in, buffer_size // ratio_in)
            y = getattr(model, method)(x)
            assert y.shape[0] == x.shape[
                0], f"{method}: batch size inconsistent"
            assert y.shape[
                1] == n_out, f"{method}: wrong output channel number"
            assert y.shape[
                2] == buffer_size // ratio_out, f"{method}: out_buffer is {y.shape[-1].item()}, should be {2**14 // ratio_out}"
            checked_methods.append(method)

    print(f"The following methods have passed the tests "
          f"with buffer size {buffer_size}:")
    for m in checked_methods:
        print(f" - {m}")
