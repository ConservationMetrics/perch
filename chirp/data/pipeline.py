# coding=utf-8
# Copyright 2022 The Chirp Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Data pipeline functions."""

import functools
from typing import Dict, Tuple

import jax
import tensorflow as tf
import tensorflow_datasets as tfds

_DEFAULT_DATASET_DIR = None


def _trim(audio: tf.Tensor,
          window_size: int) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
  """Trims an audio sequence."""
  max_start_index = tf.shape(audio)[0] - window_size
  max_start_index = tf.maximum(max_start_index, 1)
  start_index = tf.random.uniform(
      shape=[], minval=0, maxval=max_start_index, dtype=tf.int32)
  end_index = start_index + window_size
  trimmed = audio[start_index:start_index + window_size]

  pad_length = window_size - tf.shape(trimmed)[0]
  pads = [[0, pad_length]]
  trimmed = tf.pad(trimmed, pads)
  trimmed = tf.reshape(trimmed, [window_size])
  return trimmed, start_index, end_index


def _normalize_audio(audio: tf.Tensor,
                     target_gain: tf.Tensor) -> Tuple[tf.Tensor, tf.Tensor]:
  """Renormalizes an audio sequence to a max absolute value of target_gain."""
  max_gain = tf.reduce_max(tf.abs(audio), axis=0, keepdims=True)
  gain_scalar = target_gain / (max_gain + 0.01)
  audio = audio * gain_scalar
  return audio, gain_scalar


def mix_audio(dataset: tf.data.Dataset, mixin_prob: float) -> tf.data.Dataset:
  """Mix audio samples.

  Args:
    dataset: The dataset of normalized audio samples. Must be before
      mel-spectrogram creation.
    mixin_prob: The probability with which samples are mixed. Note that if we
      mix, e.g., 50% of samples, the final ratio between mixed and unmixed
      samples is 1:2. More formally, to get a fraction `p` of the samples to be
        mixed, set `mixin_prob` to `2 * p / (p + 1)`.

  Returns:
    A dataset with mixed audio examples.
  """

  def key_func(_):
    return tf.cast(tf.less(tf.random.uniform([]), mixin_prob), tf.int64)

  def reduce_func(key, dataset):
    key = tf.equal(key, 0)
    return tf.cond(
        key, lambda: dataset.batch(1, drop_remainder=True).map(_mix_audio),
        lambda: dataset.batch(2, drop_remainder=True).map(_mix_audio))

  def _mix_audio(examples: Dict[str, tf.Tensor]) -> Dict[str, tf.Tensor]:
    if examples['source_audio'].shape[0] == 1:
      examples['source_audio'] = tf.concat(
          [examples['source_audio'],
           tf.zeros_like(examples['source_audio'])],
          axis=0)
    # Remove the dummy batch dimension added by multi_hot.
    examples['source_audio'] = examples['source_audio'][:, 0]

    for key in ('label', 'genus', 'family', 'order', 'bg_labels'):
      examples[key] = tf.reduce_max(examples[key], axis=0)
    # TODO(bartvm): Replace averaging with leaving first example untouched and
    # mixing in second example with a random gain
    # Scale source_audio to match gain in the mixed audio.
    examples['source_audio'] /= examples['audio'].shape[0]
    examples['audio'] = tf.reduce_mean(examples['audio'], axis=0)
    return examples

  return dataset.group_by_window(key_func, reduce_func, window_size=2)


def process_audio(example: Dict[str, tf.Tensor], info: tfds.core.DatasetInfo,
                  window_size_s: int, min_gain: float,
                  max_gain: float) -> Dict[str, tf.Tensor]:
  """Processes an example.

  Args:
    example: the input example.
    info: dataset information.
    window_size_s: window size (in seconds) for the random cropping operation.
    min_gain: minimum gain for the random renormalization operation.
    max_gain: maximum gain for the random renormalization operation.

  Returns:
    The processed example.
  """
  example['audio'], start_ind, end_ind = _trim(
      example['audio'],
      window_size=window_size_s * info.features['audio'].sample_rate)
  example['audio'], gain_scalar = _normalize_audio(
      example['audio'],
      target_gain=tf.random.uniform([], minval=min_gain, maxval=max_gain))
  example['source_audio'] = (
      example['source_audio'][:, start_ind:end_ind] * gain_scalar)
  return example


def multi_hot(
    example: Dict[str, tf.Tensor],
    info: tfds.core.DatasetInfo,
) -> Dict[str, tf.Tensor]:
  """Convert labels to multi-hot representation.

  This must be done before batching.

  Args:
    example: the input example.
    info: dataset information.

  Returns:
    The processed example with `bg_labels` replaced using a multi-hot
    representation.
  """
  del example['filename']
  del example['label_str']
  for key, feature in info.features.items():
    if (isinstance(feature, tfds.features.Sequence) and
        isinstance(feature.feature, tfds.features.ClassLabel)):
      example[key] = tf.clip_by_value(
          tf.reduce_sum(
              tf.one_hot(
                  example[key], feature.feature.num_classes, dtype=tf.int32),
              axis=0), 0, 1)
  # Add a dummy batch dimension so that source_audio has shape [num_sources, T]
  example['source_audio'] = example['audio'][tf.newaxis, ...]
  return example


def get_dataset(split: str,
                batch_size: int,
                dataset_directory: str = _DEFAULT_DATASET_DIR,
                **data_config) -> Tuple[tf.data.Dataset, tfds.core.DatasetInfo]:
  """Returns the placeholder dataset.

  Args:
    split: data split, e.g. 'train', 'test', 'train[:80%]', etc.
    batch_size: batch size.
    dataset_directory: dataset directory.
    **data_config: Data configuration, passed on to `process_audio`.

  Returns:
    The placeholder dataset.
  """
  mixin_prob = data_config.pop('mixin_prob')

  builder = tfds.core.builder_from_directory(dataset_directory)
  ds = builder.as_dataset(split=split).map(
      functools.partial(multi_hot, info=builder.info))
  # TODO(bartvm): Pass `train` argument instead of relying on split name.
  if 'train' in split:
    ds = ds.shuffle(batch_size * 10)
    ds = mix_audio(ds, mixin_prob)
  else:
    # TODO(tomdenton): Add an option to mix-in audio during eval.
    ds = mix_audio(ds, 0.0)

  per_device_batch_size = batch_size // jax.device_count()
  ds = ds.batch(per_device_batch_size, drop_remainder=True)

  def process_batch(batch):
    return tf.vectorized_map(
        functools.partial(process_audio, info=builder.info, **data_config),
        batch)

  ds = builder.as_dataset(split=split).map(
      functools.partial(multi_hot, info=builder.info))
  # TODO(bartvm): Pass `train` argument instead of relying on split name.
  if 'train' in split:
    ds = ds.shuffle(batch_size * 10)
    if mixin_prob > 0.0:
      ds = mix_audio(ds, mixin_prob)

  per_device_batch_size = batch_size // jax.device_count()
  ds = ds.batch(per_device_batch_size, drop_remainder=True)

  ds = ds.map(process_batch, num_parallel_calls=tf.data.AUTOTUNE)
  ds = ds.batch(jax.device_count(), drop_remainder=True)
  if 'train' in split:
    ds = ds.repeat()
  ds = ds.prefetch(tf.data.AUTOTUNE)
  return ds, builder.info