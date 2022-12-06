from functools import partial
from typing import Callable, Optional

import cached_conv as cc
import gin
import numpy as np
import torch
import torch.nn as nn
from torch.nn.utils import weight_norm
from vector_quantize_pytorch import VectorQuantize
from typing import Sequence

from .core import amp_to_impulse_response, fft_convolve, mod_sigmoid


@gin.configurable
def normalization(module: nn.Module, mode: str = 'identity'):
    if mode == 'identity':
        return module
    elif mode == 'weight_norm':
        return weight_norm(module)
    else:
        raise Exception(f'Normalization mode {mode} not supported')


class ResidualVectorQuantize(nn.Module):

    def __init__(self, dim: int, num_quantizers: int, codebook_size: int,
                 dynamic_masking: bool) -> None:
        super().__init__()
        self.layers = nn.ModuleList([
            VectorQuantize(
                dim,
                codebook_size,
                channel_last=False,
                kmeans_init=True,
            ) for _ in range(num_quantizers)
        ])
        self._dynamic_masking = dynamic_masking
        self._num_quantizers = num_quantizers

    def forward(self, x):
        quantized_list = []
        losses = []

        for layer in self.layers:
            quantized, _, _ = layer(x)

            squared_diff = (quantized.detach() - x).pow(2)
            loss = squared_diff.reshape(x.shape[0], -1).mean(-1)

            x = x - quantized

            quantized_list.append(quantized)
            losses.append(loss)

        quantized_out, losses = map(
            partial(torch.stack, dim=-1),
            (quantized_list, losses),
        )

        if self.training and self._dynamic_masking:
            # BUILD MASK THRESHOLD
            mask_threshold = torch.randint(
                0,
                self._num_quantizers,
                (x.shape[0], ),
            )[..., None]
            quant_index = torch.arange(self._num_quantizers)[None]
            mask = quant_index > mask_threshold

            # BUILD MASK
            mask = mask.to(x.device)
            mask_threshold = mask_threshold.to(x.device)

            # QUANTIZER DROPOUT
            quantized_out = torch.where(
                mask[:, None, None, :],
                torch.zeros_like(quantized_out),
                quantized_out,
            )

            # LOSS DROPOUT
            losses = torch.where(
                mask,
                torch.zeros_like(losses),
                losses,
            )

            losses = losses / (mask_threshold + 1)

        quantized_out = quantized_out.sum(-1)
        losses = losses.sum(-1)
        return quantized_out, losses


class SampleNorm(nn.Module):

    def forward(self, x):
        return x / torch.norm(x, 2, 1, keepdim=True)


class Residual(nn.Module):

    def __init__(self, module, cumulative_delay=0):
        super().__init__()
        additional_delay = module.cumulative_delay
        self.aligned = cc.AlignBranches(
            module,
            nn.Identity(),
            delays=[additional_delay, 0],
        )
        self.cumulative_delay = additional_delay + cumulative_delay

    def forward(self, x):
        x_net, x_res = self.aligned(x)
        return x_net + x_res


class ResidualLayer(nn.Module):

    def __init__(self, dim, kernel_size, dilations, cumulative_delay=0):
        super().__init__()
        net = []
        cd = 0
        for d in dilations:
            net.append(nn.LeakyReLU(.2))
            net.append(
                normalization(
                    cc.Conv1d(
                        dim,
                        dim,
                        kernel_size,
                        dilation=d,
                        padding=cc.get_padding(kernel_size, dilation=d),
                        cumulative_delay=cd,
                    )))
            cd = net[-1].cumulative_delay
        self.net = Residual(
            cc.CachedSequential(*net),
            cumulative_delay=cumulative_delay,
        )
        self.cumulative_delay = self.net.cumulative_delay

    def forward(self, x):
        return self.net(x)


class DilatedUnit(nn.Module):

    def __init__(self, dim: int, kernel_size: int, dilation: int) -> None:
        super().__init__()
        net = [
            nn.LeakyReLU(.2),
            normalization(
                cc.Conv1d(dim,
                          dim,
                          kernel_size=kernel_size,
                          padding=cc.get_padding(kernel_size))),
            nn.LeakyReLU(.2),
            normalization(cc.Conv1d(dim, dim, kernel_size=1)),
        ]

        self.net = cc.CachedSequential(*net)
        self.cumulative_delay = net[1].cumulative_delay

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class ResidualBlock(nn.Module):

    def __init__(self,
                 dim,
                 kernel_size,
                 dilations_list,
                 cumulative_delay=0) -> None:
        super().__init__()
        layers = []
        cd = 0

        for dilations in dilations_list:
            layers.append(
                ResidualLayer(
                    dim,
                    kernel_size,
                    dilations,
                    cumulative_delay=cd,
                ))
            cd = layers[-1].cumulative_delay

        self.net = cc.CachedSequential(
            *layers,
            cumulative_delay=cumulative_delay,
        )
        self.cumulative_delay = self.net.cumulative_delay

    def forward(self, x):
        return self.net(x)


@gin.configurable
class ResidualStack(nn.Module):

    def __init__(self,
                 dim,
                 kernel_sizes,
                 dilations_list,
                 cumulative_delay=0) -> None:
        super().__init__()
        blocks = []
        for k in kernel_sizes:
            blocks.append(ResidualBlock(dim, k, dilations_list))
        self.net = cc.AlignBranches(*blocks, cumulative_delay=cumulative_delay)
        self.cumulative_delay = self.net.cumulative_delay

    def forward(self, x):
        x = self.net(x)
        x = torch.stack(x, 0).sum(0)
        return x


class UpsampleLayer(nn.Module):

    def __init__(self, in_dim, out_dim, ratio, cumulative_delay=0):
        super().__init__()
        net = [nn.LeakyReLU(.2)]
        if ratio > 1:
            net.append(
                normalization(
                    cc.ConvTranspose1d(in_dim,
                                       out_dim,
                                       2 * ratio,
                                       stride=ratio,
                                       padding=ratio // 2)))
        else:
            net.append(
                normalization(
                    cc.Conv1d(in_dim, out_dim, 3, padding=cc.get_padding(3))))

        self.net = cc.CachedSequential(*net)
        self.cumulative_delay = self.net.cumulative_delay + cumulative_delay * ratio

    def forward(self, x):
        return self.net(x)


@gin.configurable
class NoiseGenerator(nn.Module):

    def __init__(self, in_size, data_size, ratios, noise_bands):
        super().__init__()
        net = []
        channels = [in_size] * len(ratios) + [data_size * noise_bands]
        cum_delay = 0
        for i, r in enumerate(ratios):
            net.append(
                cc.Conv1d(
                    channels[i],
                    channels[i + 1],
                    3,
                    padding=cc.get_padding(3, r),
                    stride=r,
                    cumulative_delay=cum_delay,
                ))
            cum_delay = net[-1].cumulative_delay
            if i != len(ratios) - 1:
                net.append(nn.LeakyReLU(.2))

        self.net = cc.CachedSequential(*net)
        self.data_size = data_size
        self.cumulative_delay = self.net.cumulative_delay * int(
            np.prod(ratios))

        self.register_buffer(
            "target_size",
            torch.tensor(np.prod(ratios)).long(),
        )

    def forward(self, x):
        amp = mod_sigmoid(self.net(x) - 5)
        amp = amp.permute(0, 2, 1)
        amp = amp.reshape(amp.shape[0], amp.shape[1], self.data_size, -1)

        ir = amp_to_impulse_response(amp, self.target_size)
        noise = torch.rand_like(ir) * 2 - 1

        noise = fft_convolve(noise, ir).permute(0, 2, 1, 3)
        noise = noise.reshape(noise.shape[0], noise.shape[1], -1)
        return noise


class GRU(nn.Module):

    def __init__(self,
                 dim: int,
                 num_layers: int,
                 dropout=0,
                 cumulative_delay=0) -> None:
        super().__init__()

        self.gru = nn.GRU(
            input_size=dim,
            hidden_size=dim,
            num_layers=num_layers,
            dropout=dropout,
            batch_first=True,
        )

        self.register_buffer(
            'gru_state',
            torch.zeros(num_layers, cc.MAX_BATCH_SIZE, dim),
        )

        self.cumulative_delay = cumulative_delay
        self.enabled = True

    def disable(self):
        self.enabled = False

    def enable(self):
        self.enabled = True

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if not self.enabled: return x

        x = x.permute(0, 2, 1)
        if cc.USE_BUFFER_CONV:
            x, state = self.gru(x, self.gru_state[:, :x.shape[0]])
            self.gru_state[:, :x.shape[0]] = state
        else:
            x = self.gru(x)[0]
        x = x.permute(0, 2, 1)
        return x


class Generator(nn.Module):

    def __init__(
        self,
        latent_size,
        capacity,
        data_size,
        ratios,
        loud_stride,
        use_noise,
        n_channels: int = 1,
        recurrent_layer: Optional[Callable[[], GRU]] = None,
    ):
        super().__init__()
        net = [
            normalization(
                cc.Conv1d(
                    latent_size,
                    2**len(ratios) * capacity,
                    7,
                    padding=cc.get_padding(7),
                ))
        ]

        if recurrent_layer is not None:
            net.append(
                recurrent_layer(
                    dim=2**len(ratios) * capacity,
                    cumulative_delay=net[0].cumulative_delay,
                ))

        for i, r in enumerate(ratios):
            in_dim = 2**(len(ratios) - i) * capacity
            out_dim = 2**(len(ratios) - i - 1) * capacity

            net.append(
                UpsampleLayer(
                    in_dim,
                    out_dim,
                    r,
                    cumulative_delay=net[-1].cumulative_delay,
                ))
            net.append(
                ResidualStack(out_dim,
                              cumulative_delay=net[-1].cumulative_delay))

        self.net = cc.CachedSequential(*net)

        wave_gen = normalization(
            cc.Conv1d(out_dim, data_size * n_channels, 7, padding=cc.get_padding(7)))

        loud_gen = normalization(
            cc.Conv1d(
                out_dim,
                1,
                2 * loud_stride + 1,
                stride=loud_stride,
                padding=cc.get_padding(2 * loud_stride + 1, loud_stride),
            ))

        branches = [wave_gen, loud_gen]

        if use_noise:
            noise_gen = NoiseGenerator(out_dim, data_size * n_channels)
            branches.append(noise_gen)

        self.synth = cc.AlignBranches(
            *branches,
            cumulative_delay=self.net.cumulative_delay,
        )

        self.use_noise = use_noise
        self.loud_stride = loud_stride
        self.cumulative_delay = self.synth.cumulative_delay

        self.register_buffer("warmed_up", torch.tensor(0))

    def set_warmed_up(self, state: bool):
        state = torch.tensor(int(state), device=self.warmed_up.device)
        self.warmed_up = state

    def forward(self, x):
        x = self.net(x)

        if self.use_noise:
            waveform, loudness, noise = self.synth(x)
        else:
            waveform, loudness = self.synth(x)
            noise = torch.zeros_like(waveform)

        if self.loud_stride != 1:
            loudness = loudness.repeat_interleave(self.loud_stride)
        loudness = loudness.reshape(x.shape[0], 1, -1)

        waveform = torch.tanh(waveform) * mod_sigmoid(loudness)

        if self.warmed_up and self.use_noise:
            waveform = waveform + noise

        return waveform


class Encoder(nn.Module):

    def __init__(
        self,
        data_size,
        capacity,
        latent_size,
        ratios,
        n_out,
        sample_norm,
        repeat_layers,
        n_channels: int = 1,
        recurrent_layer: Optional[Callable[[], GRU]] = None,
    ):
        super().__init__()
        net = [cc.Conv1d(data_size * n_channels, capacity, 7, padding=cc.get_padding(7))]

        for i, r in enumerate(ratios):
            in_dim = 2**i * capacity
            out_dim = 2**(i + 1) * capacity

            if sample_norm:
                net.append(SampleNorm())
            else:
                net.append(nn.BatchNorm1d(in_dim))
            net.append(nn.LeakyReLU(.2))
            net.append(
                cc.Conv1d(
                    in_dim,
                    out_dim,
                    2 * r + 1,
                    padding=cc.get_padding(2 * r + 1, r),
                    stride=r,
                    cumulative_delay=net[-3].cumulative_delay,
                ))

            for i in range(repeat_layers - 1):
                if sample_norm:
                    net.append(SampleNorm())
                else:
                    net.append(nn.BatchNorm1d(out_dim))
                net.append(nn.LeakyReLU(.2))
                net.append(
                    cc.Conv1d(
                        out_dim,
                        out_dim,
                        3,
                        padding=cc.get_padding(3),
                        cumulative_delay=net[-3].cumulative_delay,
                    ))

        net.append(nn.LeakyReLU(.2))

        if recurrent_layer is not None:
            net.append(
                recurrent_layer(
                    dim=out_dim,
                    cumulative_delay=net[-2].cumulative_delay,
                ))
            net.append(nn.LeakyReLU(.2))

        net.append(
            cc.Conv1d(
                out_dim,
                latent_size * n_out,
                5,
                padding=cc.get_padding(5),
                groups=n_out,
                cumulative_delay=net[-2].cumulative_delay,
            ))

        self.net = cc.CachedSequential(*net)
        self.cumulative_delay = self.net.cumulative_delay

    def forward(self, x):
        z = self.net(x)
        return z


class EncoderV2(nn.Module):

    def __init__(self, data_size: int, capacity: int, ratios: Sequence[int],
                 latent_size: int, n_out: int, kernel_size: int,
                 dilations: Sequence[int]) -> None:
        super().__init__()
        net = [
            normalization(
                cc.Conv1d(
                    data_size,
                    capacity,
                    kernel_size=kernel_size,
                    padding=cc.get_padding(kernel_size),
                )),
        ]

        num_channels = capacity
        for r in ratios:
            # ADD RESIDUAL DILATED UNITS
            for d in dilations:
                net.append(
                    Residual(
                        DilatedUnit(
                            dim=num_channels,
                            kernel_size=kernel_size,
                            dilation=d,
                        )))

            # ADD DOWNSAMPLING UNIT
            net.append(nn.LeakyReLU(.2))
            net.append(
                normalization(
                    cc.Conv1d(
                        num_channels,
                        num_channels * r,
                        kernel_size=2 * r,
                        stride=r,
                        padding=(r // 2, r // 2),
                    )))

            num_channels *= r

        net.append(nn.LeakyReLU(.2))
        net.append(
            normalization(
                cc.Conv1d(
                    num_channels,
                    latent_size * n_out,
                    kernel_size=kernel_size,
                    padding=cc.get_padding(kernel_size),
                )))

        self.net = cc.CachedSequential(*net)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class GeneratorV2(nn.Module):

    def __init__(self, data_size: int, capacity: int, ratios: Sequence[int],
                 latent_size: int, kernel_size: int,
                 dilations: Sequence[int]) -> None:
        super().__init__()
        num_channels = np.prod(ratios) * capacity * 2
        net = [
            normalization(
                cc.Conv1d(
                    latent_size,
                    num_channels,
                    kernel_size=kernel_size,
                    padding=cc.get_padding(kernel_size),
                )),
        ]

        for r in ratios:
            # ADD DOWNSAMPLING UNIT
            net.append(nn.LeakyReLU(.2))
            net.append(
                normalization(
                    cc.ConvTranspose1d(num_channels,
                                       num_channels // r,
                                       2 * r,
                                       stride=r,
                                       padding=r // 2)))

            num_channels = num_channels // r

            # ADD RESIDUAL DILATED UNITS
            for d in dilations:
                net.append(
                    Residual(
                        DilatedUnit(
                            dim=num_channels,
                            kernel_size=kernel_size,
                            dilation=d,
                        )))

        net.append(nn.LeakyReLU(.2))
        net.append(
            cc.Conv1d(
                num_channels,
                data_size,
                kernel_size=kernel_size,
                padding=cc.get_padding(kernel_size),
            ))

        self.net = cc.CachedSequential(*net)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)
    
    def set_warmed_up(self, state: bool):
        pass


class VariationalEncoder(nn.Module):

    def __init__(self, encoder, beta: float = 1.0, n_channels=1):
        super().__init__()
        self.encoder = encoder(n_channels=n_channels)
        self.beta = beta
        self.register_buffer("warmed_up", torch.tensor(0))

    def reparametrize(self, z):
        mean, scale = z.chunk(2, 1)
        std = nn.functional.softplus(scale) + 1e-4
        var = std * std
        logvar = torch.log(var)

        z = torch.randn_like(mean) * std + mean
        kl = (mean * mean + var - logvar - 1).sum(1).mean()

        return z, self.beta * kl

    def set_warmed_up(self, state: bool):
        state = torch.tensor(int(state), device=self.warmed_up.device)
        self.warmed_up = state

    def forward(self, x: torch.Tensor):
        z = self.encoder(x)
        if self.warmed_up:
            z = z.detach()
        return z


class WasserteinEncoder(nn.Module):

    def __init__(self, encoder_cls):
        super().__init__()
        self.encoder = encoder_cls()
        self.register_buffer("warmed_up", torch.tensor(0))

    def compute_mean_kernel(self, x, y):
        kernel_input = (x[:, None] - y[None]).pow(2).mean(2) / x.shape[-1]
        return torch.exp(-kernel_input).mean()

    def compute_mmd(self, x, y):
        x_kernel = self.compute_mean_kernel(x, x)
        y_kernel = self.compute_mean_kernel(y, y)
        xy_kernel = self.compute_mean_kernel(x, y)
        mmd = x_kernel + y_kernel - 2 * xy_kernel
        return mmd

    def reparametrize(self, z):
        z_reshaped = z.permute(0, 2, 1).reshape(-1, z.shape[1])
        reg = self.compute_mmd(z_reshaped, torch.randn_like(z_reshaped))
        return z, reg.mean()

    def set_warmed_up(self, state: bool):
        state = torch.tensor(int(state), device=self.warmed_up.device)
        self.warmed_up = state

    def forward(self, x: torch.Tensor):
        z = self.encoder(x)
        if self.warmed_up:
            z = z.detach()
        return z


class DiscreteEncoder(nn.Module):

    def __init__(self, encoder_cls, rvq_cls, latent_size, num_quantizers):
        super().__init__()
        self.encoder = encoder_cls()
        self.rvq = rvq_cls()
        self.noise_amp = nn.Parameter(torch.zeros(latent_size, 1))
        self.num_quantizers = num_quantizers
        self.register_buffer("warmed_up", torch.tensor(0))
        self.register_buffer("enabled", torch.tensor(0))

    def add_noise_to_vector(self, q):
        noise_amp = nn.functional.softplus(self.noise_amp) + 1e-3
        q = q + noise_amp * torch.randn_like(q)
        return q

    @torch.jit.ignore
    def reparametrize(self, z):
        if self.enabled:
            q, commmitment = self.rvq(z)
            q = self.add_noise_to_vector(q)
            return q, commmitment.mean()
        else:
            return z, torch.zeros_like(z).mean()

    def set_warmed_up(self, state: bool):
        state = torch.tensor(int(state), device=self.warmed_up.device)
        self.warmed_up = state

    def forward(self, x):
        z = torch.tanh(self.encoder(x))
        return z
