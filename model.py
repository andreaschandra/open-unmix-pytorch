from torch.nn import LSTM, Linear, BatchNorm1d, Parameter
import torch
import torch.nn as nn
import torch.nn.functional as F
import utils
import json
import model
from pathlib import Path
from contextlib import redirect_stderr
from filtering import wiener
import io
from torchaudio.functional import istft


class NoOp(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x


class STFT(nn.Module):
    def __init__(
        self,
        n_fft=4096,
        n_hop=1024,
        center=True
    ):
        super(STFT, self).__init__()
        self.window = nn.Parameter(
            torch.hann_window(n_fft),
            requires_grad=False
        )
        self.n_fft = n_fft
        self.n_hop = n_hop
        self.center = center

    def forward(self, x):
        """
        Input: (nb_samples, nb_channels, nb_timesteps)
        Output:(nb_samples, nb_channels, nb_bins, nb_frames, 2)
        """

        nb_samples, nb_channels, nb_timesteps = x.size()

        # merge nb_samples and nb_channels for multichannel stft
        x = x.reshape(nb_samples*nb_channels, -1)

        # compute stft with parameters as close as possible scipy settings
        stft_f = torch.stft(
            x,
            n_fft=self.n_fft, hop_length=self.n_hop,
            window=self.window, center=self.center,
            normalized=False, onesided=True,
            pad_mode='reflect'
        )

        # reshape back to channel dimension
        stft_f = stft_f.contiguous().view(
            nb_samples, nb_channels, self.n_fft // 2 + 1, -1, 2
        )
        return stft_f


class Spectrogram(nn.Module):
    def __init__(
        self,
        power=1,
        mono=True
    ):
        super(Spectrogram, self).__init__()
        self.power = power
        self.mono = mono

    def forward(self, stft_f):
        """
        Input: complex STFT
            (nb_samples, nb_bins, nb_frames, 2)
        Output: Power/Mag Spectrogram
            (nb_frames, nb_samples, nb_channels, nb_bins)
        """
        stft_f = stft_f.transpose(2, 3)
        # take the magnitude
        stft_f = stft_f.pow(2).sum(-1).pow(self.power / 2.0)

        # downmix in the mag domain
        if self.mono:
            stft_f = torch.mean(stft_f, 1, keepdim=True)

        # permute output for LSTM convenience
        return stft_f.permute(2, 0, 1, 3)


def load_models(targets, model_name='umxhq', device='cpu'):
    """
    target model path can be either <target>.pth, or <target>-sha256.pth
    (as used on torchub)
    """
    if isinstance(targets, str):
        targets = [targets]

    model_path = Path(model_name).expanduser()
    if not model_path.exists():
        # model path does not exist, use hubconf model
        try:
            # disable progress bar
            err = io.StringIO()
            with redirect_stderr(err):
                return {
                    target: torch.hub.load(
                        'sigsep/open-unmix-pytorch',
                        model_name,
                        target=target,
                        device=device,
                        pretrained=True
                    )
                    for target in targets}
            print(err.getvalue())
        except AttributeError:
            raise NameError('Model does not exist on torchhub')
            # assume model is a path to a local model_name direcotry
    else:
        models = {}
        for target in targets:
            # load model from disk
            with open(Path(model_path, target + '.json'), 'r') as stream:
                results = json.load(stream)

            target_model_path = next(Path(model_path).glob("%s*.pth" % target))
            state = torch.load(
                target_model_path,
                map_location=device
            )

            max_bin = utils.bandwidth_to_max_bin(
                state['sample_rate'],
                results['args']['nfft'],
                results['args']['bandwidth']
            )

            models[target] = model.OpenUnmix(
                n_fft=results['args']['nfft'],
                n_hop=results['args']['nhop'],
                nb_channels=results['args']['nb_channels'],
                hidden_size=results['args']['hidden_size'],
                max_bin=max_bin
            )

            models[target].load_state_dict(state)
            models[target].stft.center = True
            models[target].eval()
            models[target].to(device)
        return models


class OpenUnmix(nn.Module):
    def __init__(
        self,
        n_fft=4096,
        n_hop=1024,
        input_is_spectrogram=False,
        hidden_size=512,
        nb_channels=2,
        sample_rate=44100,
        nb_layers=3,
        input_mean=None,
        input_scale=None,
        max_bin=None,
        unidirectional=False,
        power=1
    ):
        """
        Input: (nb_samples, nb_channels, nb_timesteps)
            or (nb_frames, nb_samples, nb_channels, nb_bins)
        Output: Power/Mag Spectrogram
                (nb_frames, nb_samples, nb_channels, nb_bins)
        """

        super(OpenUnmix, self).__init__()

        self.nb_output_bins = n_fft // 2 + 1
        if max_bin:
            self.nb_bins = max_bin
        else:
            self.nb_bins = self.nb_output_bins

        self.hidden_size = hidden_size

        self.stft = STFT(n_fft=n_fft, n_hop=n_hop)
        self.spec = Spectrogram(power=power, mono=(nb_channels == 1))
        self.register_buffer('sample_rate', torch.as_tensor(sample_rate))

        if input_is_spectrogram:
            self.transform = NoOp()
        else:
            self.transform = nn.Sequential(self.stft, self.spec)

        self.fc1 = Linear(
            self.nb_bins*nb_channels, hidden_size,
            bias=False
        )

        self.bn1 = BatchNorm1d(hidden_size)

        if unidirectional:
            lstm_hidden_size = hidden_size
        else:
            lstm_hidden_size = hidden_size // 2

        self.lstm = LSTM(
            input_size=hidden_size,
            hidden_size=lstm_hidden_size,
            num_layers=nb_layers,
            bidirectional=not unidirectional,
            batch_first=False,
            dropout=0.4 if nb_layers > 1 else 0,
        )

        fc2_hiddensize = hidden_size * 2
        self.fc2 = Linear(
            in_features=fc2_hiddensize,
            out_features=hidden_size,
            bias=False
        )

        self.bn2 = BatchNorm1d(hidden_size)

        self.fc3 = Linear(
            in_features=hidden_size,
            out_features=self.nb_output_bins*nb_channels,
            bias=False
        )

        self.bn3 = BatchNorm1d(self.nb_output_bins*nb_channels)

        if input_mean is not None:
            input_mean = torch.from_numpy(
                -input_mean[:self.nb_bins]
            ).float()
        else:
            input_mean = torch.zeros(self.nb_bins)

        if input_scale is not None:
            input_scale = torch.from_numpy(
                1.0/input_scale[:self.nb_bins]
            ).float()
        else:
            input_scale = torch.ones(self.nb_bins)

        self.input_mean = Parameter(input_mean)
        self.input_scale = Parameter(input_scale)

        self.output_scale = Parameter(
            torch.ones(self.nb_output_bins).float()
        )
        self.output_mean = Parameter(
            torch.ones(self.nb_output_bins).float()
        )

    def freeze(self):
        # set all parameters as not requiring gradient, more RAM-efficient
        # at test time
        for p in self.parameters():
            p.requires_grad = False

    def forward(self, x):
        # check for waveform or spectrogram
        # transform to spectrogram if (nb_samples, nb_channels, nb_timesteps)
        # and reduce feature dimensions, therefore we reshape
        x = self.transform(x)
        nb_frames, nb_samples, nb_channels, nb_bins = x.data.shape

        mix = x.detach().clone()

        # crop
        x = x[..., :self.nb_bins]
        # shift and scale input to mean=0 std=1 (across all bins)
        x += self.input_mean
        x *= self.input_scale

        # to (nb_frames*nb_samples, nb_channels*nb_bins)
        # and encode to (nb_frames*nb_samples, hidden_size)
        x = self.fc1(x.reshape(-1, nb_channels*self.nb_bins))
        # normalize every instance in a batch
        x = self.bn1(x)
        x = x.reshape(nb_frames, nb_samples, self.hidden_size)
        # squash range ot [-1, 1]
        x = torch.tanh(x)

        # apply 3-layers of stacked LSTM
        lstm_out = self.lstm(x)

        # lstm skip connection
        x = torch.cat([x, lstm_out[0]], -1)

        # first dense stage + batch norm
        x = self.fc2(x.reshape(-1, x.shape[-1]))
        x = self.bn2(x)

        x = F.relu(x)

        # second dense stage + layer norm
        x = self.fc3(x)
        x = self.bn3(x)

        # reshape back to original dim
        x = x.reshape(nb_frames, nb_samples, nb_channels, self.nb_output_bins)

        # apply output scaling
        x *= self.output_scale
        x += self.output_mean

        # since our output is non-negative, we can apply RELU
        x = F.relu(x) * mix

        return x


class Separator(nn.Module):
    """
    Separator class to encapsulate all the stereo filtering
    as a torch Module, to enable end-to-end learning.

    Parameters
    ----------
    targets: dictionary of target models {target: model}
        the spectrogram models to be used by the Separator. Each model
        may for instance be loaded with the `model.load_models` function

    niter: int
         Number of EM steps for refining initial estimates in a
         post-processing stage. Zeroed if only one target is estimated.
         defaults to 1.

    residual: str or None
        adds an additional residual target with provided name, obtained by
        subtracting the other estimated targets from the mixture, before any
        potential EM post-processing.
        Defaults to None

    out: str or None
        if provided, must be a string containing a valid expression for
        a dictionary, with keys the output target names, and values
        a list of targets that are used to build it. For instance:
        '{"vocals":["vocals"], "accompaniment":["drums","bass","other"]}'

    batch_size: {None | int}
        The size of the batches (number of frames) on which to apply filtering
        independently. This means assuming time varying stereo models and
        localization of sources.
        None means not batching but using the whole signal. It comes at the
        price of a much larger memory usage.
    """
    def __init__(
        self,
        targets,
        niter=1,
        softmask=False,
        residual=None,
        batch_size=None
    ):
        super(Separator, self).__init__()

        # saving parameters
        self.niter = niter
        self.residual = residual
        self.batch_size = batch_size

        # registering the targets models
        self.targets = nn.ModuleDict(targets)
        # adding till https://github.com/pytorch/pytorch/issues/38963
        self.nb_targets = len(self.targets)
        # get the sample_rate as the sample_rate of the first model
        # (tacitly assume it's the same for all targets)
        self.sample_rate = next(iter(self.targets.values())).sample_rate

    def freeze(self):
        # set all parameters as not requiring gradient, more RAM-efficient
        # at test time
        for p in self.parameters():
            p.requires_grad = False

    def forward(self, audio):
        """
        Performing the separation on audio input

        Parameters
        ----------
        audio: torch.Tensor [shape=(nb_samples, nb_channels, nb_timesteps)]
        mixture audio

        Returns
        -------
        estimates: `dict` [`str`: `torch.Tensor`
                                  shape(nb_samples, nb_channels, nb_timesteps)]
            dictionary of all restimates as performed by the separation model.

            """

        # initializing spectrograms variable
        spectrograms = None

        nb_sources = self.nb_targets
        nb_samples = audio.shape[0]

        for j, target in enumerate(self.targets):
            unmix_target = self.targets[target]

            # apply current model to get the source spectrogram
            target_spectrogram = unmix_target(audio)

            # output is nb_frames, nb_samples, nb_channels, nb_bins
            if spectrograms is None:
                # allocate the spectrograms variable
                spectrograms = torch.zeros(
                    target_spectrogram.shape + (nb_sources,),
                    device=target_spectrogram.device
                )

            spectrograms[..., j] = target_spectrogram

        # transposing it as
        # (nb_samples, nb_frames, nb_bins,{1,nb_channels}, nb_sources)
        spectrograms = spectrograms.permute(1, 0, 3, 2, 4)

        # getting the STFT of mix:
        # (nb_samples, nb_channels, nb_bins, nb_frames, 2)
        mix_stft = unmix_target.stft(audio)
        # rearranging it into:
        # (nb_samples, nb_frames, nb_bins, nb_channels, 2) to feed
        # into filtering methods
        mix_stft = mix_stft.permute(0, 3, 2, 1, 4)

        # create an additional target if we need to build a residual
        targets = list(self.targets.keys())
        if self.residual is not None:
            targets += [self.residual]
            nb_sources += 1

        if len(targets) == 1 and self.niter > 0:
            raise Exception('Cannot use EM if only one target is estimated.'
                            'Provide two targets or create an additional '
                            'one with `--residual`')

        nb_frames = spectrograms.shape[1]
        targets_stft = torch.zeros(
            mix_stft.shape + (nb_sources, ),
            device=mix_stft.device
        )
        for sample in range(nb_samples):
            pos = 0
            batch_size = self.batch_size if self.batch_size else nb_frames
            while pos < nb_frames:
                t = torch.arange(pos, min(nb_frames, pos+batch_size))
                pos = t[-1] + 1

                targets_stft[sample, t] = wiener(
                    spectrograms[sample, t], mix_stft[sample, t],
                    self.niter, use_softmask=False,
                    residual=self.residual
                )
        estimates = {}

        # getting to (nb_samples, nb_sources, channel, fft_size, n_frames, 2)
        targets_stft = targets_stft.permute(0, 5, 3, 2, 1, 4).contiguous()

        # Now performing the inverse STFTs
        estimates = istft(
            targets_stft,
            n_fft=unmix_target.stft.n_fft,
            hop_length=unmix_target.stft.n_hop,
            window=unmix_target.stft.window,
            center=unmix_target.stft.center,
            normalized=False,
            onesided=True,
            pad_mode='reflect',
            length=audio.shape[-1]
        )

        return estimates
