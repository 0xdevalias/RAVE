from typing import Sequence, Type

import cached_conv as cc
import gin
import numpy as np
import torch.nn as nn
import torch.nn.utils.weight_norm as wn


@gin.register
class ConvNet(nn.Module):

    def __init__(self, in_size, out_size, capacity, n_layers, kernel_size,
                 stride, conv) -> None:
        super().__init__()
        channels = [in_size]
        channels += list(capacity * 2**np.arange(n_layers))

        if isinstance(stride, int):
            stride = n_layers * [stride]

        net = []
        for i in range(n_layers):
            if not isinstance(kernel_size, int):
                pad = (cc.get_padding(kernel_size[0],
                                      stride[i],
                                      mode="centered")[0], 0)
                s = (stride[i], 1)
            else:
                pad = cc.get_padding(kernel_size, stride[i],
                                     mode="centered")[0]
                s = stride[i]
            net.append(
                conv(
                    channels[i],
                    channels[i + 1],
                    kernel_size,
                    stride=s,
                    padding=pad,
                ))
            net.append(nn.LeakyReLU(.2))
        net.append(conv(channels[-1], out_size, 1))

        self.net = nn.Sequential(*net)

    def forward(self, x):
        features = []
        for layer in self.net:
            x = layer(x)
            if isinstance(layer, nn.modules.conv._ConvNd):
                features.append(x)
        return features


@gin.register
class MultiScaleDiscriminator(nn.Module):

    def __init__(self, n_discriminators, convnet, n_channels=1) -> None:
        super().__init__()
        layers = []
        for i in range(n_discriminators):
            layers.append(convnet(in_size=n_channels))
        self.layers = nn.ModuleList(layers)

    def forward(self, x):
        features = []
        for layer in self.layers:
            features.append(layer(x))
            x = nn.functional.avg_pool1d(x, 2)
        return features


@gin.register
class MultiPeriodDiscriminator(nn.Module):

    def __init__(self, periods, convnet, n_channels=1) -> None:
        super().__init__()
        layers = []
        self.periods = periods

        for _ in periods:
            layers.append(convnet(in_size=n_channels))

        self.layers = nn.ModuleList(layers)

    def forward(self, x):
        features = []
        for layer, n in zip(self.layers, self.periods):
            features.append(layer(self.fold(x, n)))
        return features

    def fold(self, x, n):
        pad = (n - (x.shape[-1] % n)) % n
        x = nn.functional.pad(x, (0, pad))
        return x.reshape(*x.shape[:2], -1, n)


@gin.register
class CombineDiscriminators(nn.Module):

    def __init__(self, discriminators: Sequence[Type[nn.Module]]) -> None:
        super().__init__()
        self.discriminators = nn.ModuleList(disc_cls()
                                            for disc_cls in discriminators)

    def forward(self, x):
        features = []
        for disc in self.discriminators:
            features.extend(disc(x))
        return features
