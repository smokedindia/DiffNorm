# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import collections
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
class ReprToReprDatasetItem(object):
    index: int
    src_feat: torch.Tensor
    tgt_feat: torch.Tensor
    tgt_unit: torch.Tensor
    reduce_tgt_unit: torch.Tensor
    reduce_tgt_feat: torch.Tensor
    # duration_label: torch.Tensor


# src_speech to tgt feature dataset for diffusion based training
class ReprToReprUnitDataset(FairseqDataset):
    def __init__(self,
                 split: str,
                 is_train_split: bool,
                 cfg: S2SDataConfig,
                 audio_paths: List[str],
                 tgt_feat_paths: List[str],
                 tgt_units: List[List[int]],
                 src_n_frames: List[int],
                 tgt_n_frames: List[int],
                 src_langs: Optional[List[str]] = None,
                 ids: Optional[List[str]] = None,
                 tgt_dict: Optional[Dictionary] = None,
                 ):

        self.split = split
        self.si_train_split = is_train_split
        self.cfg = cfg
        self.src_n_frames, self.tgt_n_frames = src_n_frames, tgt_n_frames
        self.n_samples = len(audio_paths)
        self.audio_paths = audio_paths # the audio is also feature path
        self.tgt_feat_paths = tgt_feat_paths # tgt feature is the encoded mhubert feature from VAE
        self.tgt_units = tgt_units
        self.src_langs = src_langs
        self.ids = ids
        self.tgt_dict = tgt_dict
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


    def _reduce_tgt(self, tokens):
        dedup_tokens = []
        duration_label = []
        index_to_keep = []
        accu_duration = 1
        for i, token in enumerate(tokens):
            if i == 0:
                dedup_tokens.append(token)
                index_to_keep.append(i)
            else:
                if token != tokens[i - 1]:
                    # triggering a new duration counter
                    duration_label.append(accu_duration)
                    dedup_tokens.append(token)
                    index_to_keep.append(i)
                    accu_duration = 1
                else:
                    # duplicates found
                    accu_duration += 1
        # append the last duration
        duration_label.append(accu_duration)
        return dedup_tokens, duration_label, torch.Tensor(index_to_keep).long()

    def __getitem__(self, index: int) -> ReprToReprDatasetItem:
        src_feat_path = self.audio_paths[index]
        # src_len x 768
        src_feat = torch.from_numpy(np.load(src_feat_path)).float()
        tgt_feat_path = self.tgt_feat_paths[index]
        # tgt x 768, tgt length normally larger than src length
        tgt_feat = torch.from_numpy(np.load(tgt_feat_path)).float()
        tgt_units = self.tgt_units[index]
        # need to convert it back to strings to encode
        tgt_units_str = " ".join([str(x) for x in tgt_units])
        reduce_tgt, duration_label, index_to_keep = self._reduce_tgt(tgt_units)
        reduce_tgt_str = " ".join([str(x) for x in reduce_tgt])
        # assert len(reduce_tgt) == len(duration_label)
        # assert len(tgt_units) == tgt_feat.shape[0]

        reduced_tgt = self.tgt_dict.encode_line(
            reduce_tgt_str,
            add_if_not_exist=False,
            append_eos=False,
        ).long()

        target = self.tgt_dict.encode_line(
            tgt_units_str,
            add_if_not_exist=False,
            append_eos=False,
        ).long()
        reduce_tgt_feat = tgt_feat[index_to_keep]

        return ReprToReprDatasetItem(
            index=index,
            src_feat=src_feat,
            tgt_feat=tgt_feat,
            tgt_unit=target,
            reduce_tgt_unit=reduced_tgt,
            reduce_tgt_feat=reduce_tgt_feat,
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
        return self.tgt_n_frames[index]

    def size(self, index):
        return self.tgt_n_frames[index]

    @property
    def sizes(self):
        return np.array(self.tgt_n_frames)

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
            self, samples: List[ReprToReprDatasetItem], return_order: bool = False
    ) -> Dict:
        """
        src_feat: T x C
        tgt_feat: z (16) x T x C
        """

        if len(samples) == 0:
            return {}
        indices = torch.tensor([x.index for x in samples], dtype=torch.long)
        batch_size = len(samples)
        src_lengths = torch.tensor([x.src_feat.shape[0] for x in samples], dtype=torch.long)
        tgt_lengths = torch.tensor([x.tgt_feat.shape[0] for x in samples], dtype=torch.long)
        reduce_tgt_lengths = torch.tensor([x.reduce_tgt_unit.shape[0] for x in samples], dtype=torch.long)

        max_src_len = src_lengths.max().item()
        max_tgt_len = tgt_lengths.max().item()
        max_reduce_tgt_len = reduce_tgt_lengths.max().item()
        feature_size = samples[0].src_feat.shape[1]

        padded_src = samples[0].src_feat.new_zeros(batch_size, max_src_len, feature_size)
        padded_tgt = samples[0].src_feat.new_zeros(batch_size, max_tgt_len, feature_size)
        padded_tgt_unit = samples[0].tgt_unit.new_zeros(batch_size, max_tgt_len)
        padded_reduce_tgt_unit = samples[0].reduce_tgt_unit.new_zeros(batch_size, max_reduce_tgt_len)
        padded_reduce_tgt_feat = samples[0].reduce_tgt_feat.new_zeros(batch_size, max_reduce_tgt_len, feature_size)

        for i, sample in enumerate(samples):
            padded_src[i, :sample.src_feat.shape[0]] = sample.src_feat
            padded_tgt[i, :sample.tgt_feat.shape[0]] = sample.tgt_feat
            padded_tgt_unit[i, :sample.tgt_unit.shape[0]] = sample.tgt_unit
            padded_reduce_tgt_unit[i, :sample.reduce_tgt_unit.shape[0]] = sample.reduce_tgt_unit
            padded_reduce_tgt_feat[i, :sample.reduce_tgt_feat.shape[0]] = sample.reduce_tgt_feat

        # re-order based on src length
        src_lengths, order = src_lengths.sort(descending=True)
        indices = indices.index_select(0, order)
        padded_src = padded_src.index_select(0, order)
        padded_tgt = padded_tgt.index_select(0, order)
        padded_tgt_unit = padded_tgt_unit.index_select(0, order)
        tgt_lengths = tgt_lengths.index_select(0, order)

        padded_reduce_tgt_unit = padded_reduce_tgt_unit.index_select(0, order)
        padded_reduce_tgt_feat = padded_reduce_tgt_feat.index_select(0, order)
        reduce_tgt_lengths = reduce_tgt_lengths.index_select(0, order)
        n_tokens = reduce_tgt_lengths.sum().item()
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
            "target_unit": padded_tgt_unit,
            "reduce_target": padded_reduce_tgt_feat,
            "reduce_target_unit": padded_reduce_tgt_unit,
            "target_lengths": tgt_lengths,
            "reduce_target_lengths": reduce_tgt_lengths,
            "ntokens": n_tokens,
            "nsentences": batch_size,
        }
        return out




class ReprToReprUnitDatasetCreator(object):
    # mandatory columns
    KEY_ID, KEY_SRC_AUDIO, KEY_SRC_N_FRAMES = "id", "src_audio", "src_n_frames"
    KEY_TGT_AUDIO, KEY_TGT_N_FRAMES = "tgt_audio", "tgt_n_frames"
    KEY_TGT_UNIT = "tgt_unit"
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
        tgt_dict: Dictionary,
    ) -> SpeechToSpeechDataset:

        src_audio_paths = [
            s[cls.KEY_SRC_AUDIO] for s in samples
        ]
        ids = [s[cls.KEY_ID] for s in samples]
        src_n_frames = [int(s[cls.KEY_SRC_N_FRAMES]) for s in samples]
        tgt_feat_paths = [s[cls.KEY_TGT_AUDIO] for s in samples]
        tgt_n_frames = [int(s[cls.KEY_TGT_N_FRAMES]) for s in samples]
        tgt_units = [s[cls.KEY_TGT_UNIT] for s in samples]
        ds = ReprToReprUnitDataset(
            split=split_name,
            is_train_split=is_train_split,
            cfg=data_cfg,
            audio_paths=src_audio_paths,
            tgt_feat_paths=tgt_feat_paths,
            tgt_units=tgt_units,
            src_n_frames=src_n_frames,
            tgt_n_frames=tgt_n_frames,
            ids=ids,
            tgt_dict=tgt_dict
        )
        return ds




    @staticmethod
    def _load_samples_from_tsv(src_feat_dir, tgt_feat_dir, raw_audio_root, split):
        def prepare_id2feat_dict(manifest_file):
            id2feat = {}
            with open(manifest_file, "r") as f:
                feat_dir = f.readline().strip()
                for line in f:
                    if len(line.strip()) == 0:
                        continue
                    feat_name, feat_len = line.strip().split("\t")
                    # feat example: common_voice_es_19979909.feat.npy
                    feat_id = feat_name.split(".")[0]
                    feat_path = f"{feat_dir}/{feat_name}"
                    id2feat[feat_id] = (feat_path, feat_len)
            return id2feat


        feat_manifest_file = f"{src_feat_dir}/{split}.manifest.tsv"
        tgt_manifest_file = f"{tgt_feat_dir}/{split}.manifest.tsv"
        translation_manifest_file = f"{raw_audio_root}/{split}.quant.tsv"
        samples = []

        src_id2feat = prepare_id2feat_dict(feat_manifest_file)
        tgt_id2feat = prepare_id2feat_dict(tgt_manifest_file)
        # print(f"src_id2feat: {len(src_id2feat)}, tgt_id2feat: {len(tgt_id2feat)}")

        counter = 0
        with open(translation_manifest_file) as f:
            f.readline() # skip the first title line
            # manifest has form <id, src_audio_path, #src_frames, tgt_audio_token, #tgt_frames>
            for line in f:
                if len(line.strip()) == 0:
                    continue

                # src_id, src_audio_path, src_n_frames, tgt_audio_token, tgt_n_frames = line.rstrip().split("\t")
                src_id, tgt_audio_token = line.rstrip().split("|")
                src_id = src_id.split(".")[0]
                if (src_id not in src_id2feat) or (src_id not in tgt_id2feat):
                    print(f"src_id: {src_id} not found in feat manifest")
                    continue
                src_feat_path, src_feat_len = src_id2feat[src_id]
                tgt_feat_path, tgt_feat_len = tgt_id2feat[src_id]

                tgt_tokens = [int(x) for x in tgt_audio_token.split(" ")]
                if not (len(tgt_tokens) == int(tgt_feat_len)):
                    print(f"warning: mismatched feature and unit size. tgt_tokens: {len(tgt_tokens)}, tgt_feat_len: {tgt_feat_len}")
                    continue

                samples.append({
                    ReprToReprUnitDatasetCreator.KEY_ID: src_id,
                    ReprToReprUnitDatasetCreator.KEY_SRC_AUDIO: src_feat_path,
                    ReprToReprUnitDatasetCreator.KEY_SRC_N_FRAMES: src_feat_len,
                    ReprToReprUnitDatasetCreator.KEY_TGT_AUDIO: tgt_feat_path,
                    ReprToReprUnitDatasetCreator.KEY_TGT_UNIT: tgt_tokens,
                    ReprToReprUnitDatasetCreator.KEY_TGT_N_FRAMES: tgt_feat_len,
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
        src_feat_dir: str,
        tgt_feat_dir: str,
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
            samples = ReprToReprUnitDatasetCreator._load_samples_from_tsv(
                src_feat_dir, tgt_feat_dir, audio_root, split)
            ds = cls._from_list(
                split_name=split,
                is_train_split=is_train_split,
                samples=samples,
                data_cfg=data_cfg,
                tgt_dict=tgt_dict,
            )
            datasets.append(ds)
        return ConcatDataset(datasets) if len(datasets) > 1 else datasets[0]
