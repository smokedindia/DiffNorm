from argparse import Namespace
import torch
import torch.nn as nn

from fairseq.models.text_to_speech.fastspeech2 import VariancePredictor
from fairseq.models.text_to_speech.hifigan import Generator


class CodeGenerator(Generator):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.dict = nn.Embedding(cfg["num_embeddings"], cfg["embedding_dim"])
        self.multispkr = cfg.get("multispkr", None)
        self.embedder = cfg.get("embedder_params", None)

        if self.multispkr and not self.embedder:
            self.spkr = nn.Embedding(cfg.get("num_speakers", 200), cfg["embedding_dim"])
        elif self.embedder:
            self.spkr = nn.Linear(cfg.get("embedder_dim", 256), cfg["embedding_dim"])

        self.dur_predictor = None
        if cfg.get("dur_predictor_params", None):
            self.dur_predictor = VariancePredictor(
                Namespace(**cfg["dur_predictor_params"])
            )

    @staticmethod
    def _upsample(signal, max_frames):
        if signal.dim() == 3:
            bsz, channels, cond_length = signal.size()
        elif signal.dim() == 2:
            signal = signal.unsqueeze(2)
            bsz, channels, cond_length = signal.size()
        else:
            signal = signal.view(-1, 1, 1)
            bsz, channels, cond_length = signal.size()

        signal = signal.unsqueeze(3).repeat(1, 1, 1, max_frames // cond_length)

        # pad zeros as needed (if signal's shape does not divide completely with max_frames)
        reminder = (max_frames - signal.shape[2] * signal.shape[3]) // signal.shape[3]
        if reminder > 0:
            raise NotImplementedError(
                "Padding condition signal - misalignment between condition features."
            )

        signal = signal.view(bsz, channels, max_frames)
        return signal

    def forward(self, **kwargs):
        # bz x T -> bz x T x H -> bz x H x T
        x = self.dict(kwargs["code"]).transpose(1, 2)

        if self.dur_predictor and kwargs.get("dur_prediction", False):
            assert x.size(0) == 1, "only support single sample"
            log_dur_pred = self.dur_predictor(x.transpose(1, 2))
            dur_out = torch.clamp(
                torch.round((torch.exp(log_dur_pred) - 1)).long(), min=1
            )
            # B x C x T -> B x C x T' (T' = T * dur_out)
            x = torch.repeat_interleave(x, dur_out.view(-1), dim=2)

        if self.multispkr:
            assert (
                "spkr" in kwargs
            ), 'require "spkr" input for multispeaker CodeHiFiGAN vocoder'
            spkr = self.spkr(kwargs["spkr"]).transpose(1, 2)
            spkr = self._upsample(spkr, x.shape[-1])
            x = torch.cat([x, spkr], dim=1)

        for k, feat in kwargs.items():
            if k in ["spkr", "code", "dur_prediction"]:
                continue
            feat = self._upsample(feat, x.shape[-1])
            x = torch.cat([x, feat], dim=1)
        return super().forward(x)
