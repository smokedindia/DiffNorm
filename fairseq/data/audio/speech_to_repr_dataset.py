# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import torch
import numpy as np
import torch.nn.functional as F

from fairseq.data import ConcatDataset, Dictionary, FairseqDataset
from fairseq.data import data_utils as fairseq_data_utils
from fairseq.data.audio.audio_utils import get_features_or_waveform
from fairseq.data.audio.data_cfg import S2SDataConfig
from fairseq.data.audio.speech_to_text_dataset import (
    SpeechToTextDataset,
    SpeechToTextDatasetCreator,
    TextTargetMultitaskData,
    _collate_frames,
    _is_int_or_np_int
)
from fairseq.data.audio.feature_transforms import CompositeAudioFeatureTransform
from fairseq.data.audio.waveform_transforms import CompositeAudioWaveformTransform
from fairseq.data.audio.dataset_transforms import CompositeAudioDatasetTransform
from fairseq.data.audio.speech_to_speech_dataset import SpeechToSpeechDataset
logger = logging.getLogger(__name__)



@dataclass
class SpeechToReprDatasetItem(object):
    index: int
    src_audio: torch.Tensor
    tgt_feat: torch.Tensor


# src_speech to tgt feature dataset for diffusion based training
class SpeechToReprDataset(FairseqDataset):
    def __init__(self,
                 split: str,
                 is_train_split: bool,
                 cfg: S2SDataConfig,
                 audio_paths: List[str],
                 tgt_feat_paths: List[str],
                 src_n_frames: List[int],
                 tgt_n_frames: List[int],
                 src_langs: Optional[List[str]] = None,
                 ids: Optional[List[str]] = None,
                 ):

        self.split = split
        self.si_train_split = is_train_split
        self.cfg = cfg
        self.src_n_frames, self.tgt_n_frames = src_n_frames, tgt_n_frames
        self.n_samples = len(audio_paths)
        self.audio_paths = audio_paths
        self.tgt_feat_paths = tgt_feat_paths
        self.src_langs = src_langs
        self.ids = ids
        assert self.n_samples == len(self.src_n_frames) == len(self.tgt_n_frames)
        assert ids is None or len(ids) == self.n_samples
        assert src_langs is None or len(src_langs) == self.n_samples
        self.shuffle = cfg.shuffle if is_train_split else False

        self.feature_transforms = CompositeAudioFeatureTransform.from_config_dict(
            self.cfg.get_feature_transforms(split, is_train_split)
        ) # cmvn
        self.waveform_transforms = CompositeAudioWaveformTransform.from_config_dict(
            self.cfg.get_waveform_transforms(split, is_train_split)
        ) # none
        # print(self.feature_transforms, self.waveform_transforms, self.dataset_transforms)
        if self.feature_transforms and self.cfg.use_audio_input:
            logger.warning(
                "Feature transforms will not be applied. To use feature transforms, "
                "set use_audio_input as False in config."
            )
        logger.info(self.__repr__())



    def __getitem__(self, index: int) -> SpeechToReprDatasetItem:
        feat_path = self.tgt_feat_paths[index]
        feat_npy = np.load(feat_path) # T x C=768
        # use_audio_input = False, log mel feature is extracted, waveform_transform=None
        source = get_features_or_waveform(
            self.audio_paths[index],
            need_waveform=self.cfg.use_audio_input,
            use_sample_rate=16000,
            waveform_transforms=self.waveform_transforms,
        ) # T (compressed by logmel window) x C=80
        if self.feature_transforms is not None:
            source = self.feature_transforms(source)
        source = torch.from_numpy(source).float()
        feat = torch.from_numpy(feat_npy).float()
        return SpeechToReprDatasetItem(
            index=index,
            src_audio=source,
            tgt_feat=feat,
        )


    # overwrite based class function
    def __repr__(self):
        return (
                self.__class__.__name__
                + f'(split="{self.split}", n_samples={self.n_samples:_}, '
                  f"shuffle={self.shuffle}, "
                  f"feature_transforms={self.feature_transforms}, "
                  f"waveform_transforms={self.waveform_transforms}, "
        )

    def num_tokens(self, index):
        return self.src_n_frames[index] + self.tgt_n_frames[index]

    def size(self, index):
        return self.src_n_frames[index] + self.tgt_n_frames[index]

    @property
    def sizes(self):
        return np.array(self.src_n_frames) + np.array(self.tgt_n_frames)

    @property
    def can_reuse_epoch_itr_across_epochs(self):
        return True

    def ordered_indices(self):
        if self.shuffle:
            order = [np.random.permutation(len(self))]
        else:
            order = [np.arange(len(self))]
        # first by descending order of # of frames then by original/random order
        order.append([-n for n in self.tgt_n_frames])
        return np.lexsort(order)

    def prefetch(self, indices):
        raise False

    def __len__(self):
        return self.n_samples


    def collater(
            self, samples: List[SpeechToReprDatasetItem], return_order: bool = False
    ) -> Dict:
        if len(samples) == 0:
            return {}
        indices = torch.tensor([x.index for x in samples], dtype=torch.long)
        src_lengths = torch.tensor([x.src_audio.shape[0] for x in samples], dtype=torch.long)
        tgt_lengths = torch.tensor([x.tgt_feat.shape[0] for x in samples], dtype=torch.long)
        max_src_len = src_lengths.max().item()
        max_tgt_len = tgt_lengths.max().item()
        batch_size = len(samples)
        src_feature_size = samples[0].src_audio.shape[1] # should be 80, as defined by our mel config
        tgt_feature_size = samples[0].tgt_feat.shape[1] # should be 768, as defined by mhubert model used
        padded_src = samples[0].src_audio.new_zeros(batch_size, max_src_len, src_feature_size)
        padded_tgt = samples[0].src_audio.new_zeros(batch_size, max_tgt_len, tgt_feature_size)
        for i, sample in enumerate(samples):
            padded_src[i, :sample.src_audio.shape[0]] = sample.src_audio
            padded_tgt[i, :sample.tgt_feat.shape[0]] = sample.tgt_feat
        # re-order based on src length
        src_lengths, order = src_lengths.sort(descending=True)
        indices = indices.index_select(0, order)
        padded_src = padded_src.index_select(0, order)
        padded_tgt = padded_tgt.index_select(0, order)
        tgt_lengths = tgt_lengths.index_select(0, order)
        n_tokens = tgt_lengths.sum().item()
        net_input = {
            "src_tokens": padded_src,
            "src_lengths": src_lengths,
            "prev_output_tokens": None,  # not used in NAT generation
            "tgt_speaker": None,  # not used
        }
        out = {
            "id": indices,
            "net_input": net_input,
            "speaker": None,  # not used
            "target": padded_tgt,
            "target_lengths": tgt_lengths,
            "ntokens": n_tokens,
            "nsentences": batch_size,
        }
        return out




class SpeechToReprDatasetCreator(object):
    # mandatory columns
    KEY_ID, KEY_SRC_AUDIO, KEY_SRC_N_FRAMES = "id", "src_audio", "src_n_frames"
    KEY_TGT_AUDIO, KEY_TGT_N_FRAMES = "tgt_audio", "tgt_n_frames"
    # optional columns
    KEY_SRC_LANG, KEY_TGT_LANG = "src_lang", "tgt_lang"
    # default values
    DEFAULT_LANG = ""
    KEY_SRC_FEAT = "src_feat"

    @classmethod
    def _from_list(
        cls,
        split_name: str,
        is_train_split,
        samples: List[Dict],
        data_cfg: S2SDataConfig,
    ) -> SpeechToSpeechDataset:

        src_audio_paths = [
            s[cls.KEY_SRC_AUDIO] for s in samples
        ]
        ids = [s[cls.KEY_ID] for s in samples]
        src_n_frames = [int(s[cls.KEY_SRC_N_FRAMES]) for s in samples]
        tgt_feat_paths = [s[cls.KEY_TGT_AUDIO] for s in samples]
        tgt_n_frames = [int(s[cls.KEY_TGT_N_FRAMES]) for s in samples]

        ds = SpeechToReprDataset(
            split=split_name,
            is_train_split=is_train_split,
            cfg=data_cfg,
            audio_paths=src_audio_paths,
            tgt_feat_paths=tgt_feat_paths,
            src_n_frames=src_n_frames,
            tgt_n_frames=tgt_n_frames,
            ids=ids,
        )
        return ds

    @staticmethod
    def _load_samples_from_tsv(feat_root, raw_audio_root, split):
        feat_manifest_file = f"{feat_root}/{split}.manifest.tsv"
        translation_manifest_file = f"{raw_audio_root}/{split}.tsv"
        samples = []
        # we first prepare a dictionary to map audio id to the feature path
        id2feat = {}
        with open(feat_manifest_file, "r") as f:
            feat_dir = f.readline().strip()
            for line in f:
                if len(line.strip()) == 0:
                    continue
                feat_name, feat_len = line.strip().split("\t")
                # feat example: common_voice_es_19979909.feat.npy
                feat_id = feat_name.split(".")[0]
                feat_path = f"{feat_dir}/{feat_name}"
                id2feat[feat_id] = (feat_path, feat_len)
        counter = 0
        with open(translation_manifest_file) as f:
            f.readline() # skip the first title line
            # manifest has form <id, src_audio_path, #src_frames, tgt_audio_token, #tgt_frames>
            for line in f:
                if len(line.strip()) == 0:
                    continue
                src_id, src_audio_path, src_n_frames, tgt_audio_token, tgt_n_frames = line.rstrip().split("\t")
                if src_id not in id2feat:
                    print(f"src_id: {src_id} not found in feat manifest")
                    continue
                feat_path, feat_len = id2feat[src_id]
                samples.append({
                    SpeechToReprDatasetCreator.KEY_ID: src_id,
                    SpeechToReprDatasetCreator.KEY_SRC_AUDIO: src_audio_path,
                    SpeechToReprDatasetCreator.KEY_SRC_N_FRAMES: src_n_frames,
                    SpeechToReprDatasetCreator.KEY_TGT_AUDIO: feat_path,
                    SpeechToReprDatasetCreator.KEY_TGT_N_FRAMES: feat_len,
                })
                counter += 1
                if (not "train" in split) and counter > 4000:
                # if counter > 1000: # debug code
                    # only keep 4k samples for dev/test purpose
                    break
        return samples

    @classmethod
    def from_tsv(
        cls,
        feat_root: str,
        audio_root: str,
        data_cfg: S2SDataConfig,
        splits: str,
        is_train_split: bool,
        epoch: int,
        seed: int,
        target_is_code: bool = False,
        tgt_dict: Dictionary = None,
        n_frames_per_step: int = 1,
        multitask: Optional[Dict] = None,
    ) -> SpeechToSpeechDataset:
        datasets = []
        for split in splits.split(","):
            samples = SpeechToReprDatasetCreator._load_samples_from_tsv(feat_root, audio_root, split)
            ds = cls._from_list(
                split_name=split,
                is_train_split=is_train_split,
                samples=samples,
                data_cfg=data_cfg,
            )
            datasets.append(ds)
        return ConcatDataset(datasets) if len(datasets) > 1 else datasets[0]
