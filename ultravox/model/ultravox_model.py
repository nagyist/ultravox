import logging
from collections import defaultdict
from typing import Any, Dict, Optional, Set, Tuple, Union

import peft
import torch
import torch.nn as nn
import torch.nn.functional as F
import transformers
import transformers.activations
import transformers.modeling_outputs
import transformers.modeling_utils
import transformers.models
from transformers.models.wav2vec2 import modeling_wav2vec2 as wav2vec2
from transformers.models.wav2vec2.configuration_wav2vec2 import Wav2Vec2Config
from transformers.models.whisper import modeling_whisper as whisper
from transformers.models.whisper.configuration_whisper import WhisperConfig

# We must use relative import in this directory to allow uploading to HF Hub
# Even "from . import X" pattern doesn't work (undocumented and unclear why)
from .ultravox_config import AdapterType
from .ultravox_config import LossConfig
from .ultravox_config import LossFunction
from .ultravox_config import UltravoxCFormerAdapterConfig
from .ultravox_config import UltravoxConfig
from .ultravox_config import UltravoxStackingAdapterConfig


class UltravoxModel(transformers.LlamaPreTrainedModel):
    """
    The Ultravox model which consists of an audio encoder and a language model.

    Audio input is processed by the audio encoder, then every `stack_factor` frames are stacked together and
    projected to the language model's embedding space using a few linear layers.
    The text is embedded by the language model as usual and then the audio and text embeddings are merged together.

    A special token `<|audio|>` is used to indicate the start of the audio embeddings in the merged embeddings.

    Parameters:
        config: Model configuration class with all the parameters of the model.
    """

    config_class = UltravoxConfig
    config: UltravoxConfig  # for type hinting
    _no_split_modules = ["Wav2Vec2Model", "WhisperEncoder", "LlamaDecoderLayer"]
    # We minimize the weights in state_dict in order to reduce the size of the checkpoint
    # The issue is that load_pretrained() uses state_dict() keys to know what keys are expected
    # As such we have to tell is to ignore some keys that are not always in the model
    _keys_to_ignore_on_load_unexpected = ["audio_tower.*", "language_model.*"]
    # Usually we load encoder weights from a pretrained model, so we don't want to load the decoder weights
    # Technically we never hit this issue because these keys are already removed from state_dict() however,
    # but there's no harm in keeping it here for when we change that behavior.
    _keys_to_ignore_on_load_missing = ["audio_tower.*"]

    def __init__(self, config: UltravoxConfig):
        super().__init__(config)

        self.keep_params: Set[str] = set()
        self.vocab_size = config.vocab_size

        self.audio_tower = self._create_audio_tower(config)
        self.adapter = self._create_adapter(config)
        self.language_model = self._create_language_model(config)

        self.loss_config = LossConfig()
        self.accumulated_losses: Dict[str, float] = defaultdict(float)
        self.step_counter = 0

        self.post_init()

    def get_input_embeddings(self):
        return self.language_model.get_input_embeddings()

    def set_input_embeddings(self, value):
        self.language_model.set_input_embeddings(value)

    def get_output_embeddings(self):
        return self.language_model.get_output_embeddings()

    def set_output_embeddings(self, new_embeddings):
        self.language_model.set_output_embeddings(new_embeddings)

    def set_decoder(self, decoder):
        self.language_model.set_decoder(decoder)

    def get_decoder(self):
        return self.language_model.get_decoder()

    def tie_weights(self):
        return self.language_model.tie_weights()

    def set_loss_config(self, loss_config: LossConfig):
        if (
            LossFunction.Input_KL in loss_config.loss_weights
            and self.config.adapter_type != AdapterType.CFORMER
        ):
            raise ValueError(
                f"Input KL loss is only supported for CFormer adapter, not {self.config.adapter_type}."
            )
        self.loss_config = loss_config

    def _setup_cache(
        self, cache_cls, max_batch_size: int, max_cache_len: Optional[int] = None
    ):
        self.language_model._setup_cache(cache_cls, max_batch_size, max_cache_len)

    def _reorder_cache(self, past_key_values, beam_idx):
        return self.language_model._reorder_cache(past_key_values, beam_idx)

    def _track_and_log_losses(
        self,
        losses: Dict[LossFunction, torch.Tensor],
        total_loss: float,
    ):
        """
        Track losses over multiple steps and log them at regular intervals.

        Args:
            losses (Dict[LossFunction, torch.FloatTensor]): Dictionary of individual losses
            total_loss (torch.FloatTensor): Total combined loss
        """
        # Accumulate losses
        for loss_fn, value in losses.items():
            self.accumulated_losses[loss_fn] += value.item()
        self.accumulated_losses["total"] += total_loss

        self.step_counter += 1

        # Log accumulated losses every n steps
        if self.step_counter % self.loss_config.logging_steps == 0:
            avg_losses = {
                k: v / self.loss_config.logging_steps
                for k, v in self.accumulated_losses.items()
            }

            loss_str = " , ".join(
                [
                    f"{loss_fn.value if isinstance(loss_fn, LossFunction) else loss_fn}: {value:.4f}"
                    for loss_fn, value in avg_losses.items()
                    if loss_fn != "total"
                ]
            )

            logging.info(
                f"Step {self.step_counter} | Avg Total: {avg_losses['total']:.4f} | Avg Losses: {loss_str}"
            )

            # Reset accumulated losses
            self.accumulated_losses.clear()

    def resize_token_embeddings(
        self,
        new_num_tokens: Optional[int] = None,
        pad_to_multiple_of: Optional[int] = None,
    ) -> nn.Embedding:
        model_embeds = self.language_model.resize_token_embeddings(
            new_num_tokens, pad_to_multiple_of
        )
        # update vocab size
        self.config.text_config.vocab_size = model_embeds.num_embeddings
        self.config.vocab_size = model_embeds.num_embeddings
        self.vocab_size = model_embeds.num_embeddings
        return model_embeds

    def _insert_tokens(
        self,
        inputs_embeds: torch.Tensor,
        attention_mask: torch.Tensor,
        insert_idx: torch.Tensor,
        insert_embeds: torch.Tensor,
        insert_len: torch.Tensor,
        labels: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.Tensor] = None,
        cache_position: Optional[torch.Tensor] = None,
    ) -> Tuple[
        torch.Tensor,
        torch.Tensor,
        Optional[torch.Tensor],
        Optional[torch.Tensor],
        Optional[torch.Tensor],
    ]:
        batch_size, seq_len, hidden_dim = inputs_embeds.shape

        if position_ids is not None and position_ids.shape != (batch_size, seq_len):
            raise ValueError(
                f"position_ids must have shape (batch_size, seq_len) if provided. Got: {position_ids.shape}"
            )

        # Handle cases where inputs start from a non-zero position:
        # 1. Calculate the offset using cache_position
        # 2. Store the attention mask for past tokens if there's an offset
        # 3. Adjust the attention mask to only include new tokens
        # 4. The stored past attention mask will be added back after processing
        if cache_position is not None:
            if cache_position.shape[0] != inputs_embeds.shape[1]:
                raise ValueError(
                    f"cache_position shape {cache_position.shape[0]} does not match inputs_embeds shape {inputs_embeds.shape[1]}"
                )
            if cache_position[-1] != attention_mask.shape[1] - 1:
                raise ValueError(
                    f"The last position in cache_position should be {attention_mask.shape[1]-1}, but got {cache_position[-1]}"
                )
            input_offset = int(cache_position[0].item())
        else:
            input_offset = int(0)
        past_attention_mask = (
            attention_mask[:, :input_offset] if input_offset > 0 else None
        )
        attention_mask = attention_mask[:, input_offset:]

        # Calculate the actual length of each sample and the new length after insertion
        num_tokens = attention_mask.sum(dim=1).long()
        new_num_tokens = num_tokens + insert_len
        new_seq_len = int(new_num_tokens.max().item())

        # Create new tensors with expanded size
        new_inputs_embeds = torch.zeros(
            (batch_size, new_seq_len, hidden_dim),
            device=inputs_embeds.device,
            dtype=inputs_embeds.dtype,
        )
        new_attention_mask = torch.zeros(
            (batch_size, new_seq_len),
            device=attention_mask.device,
            dtype=attention_mask.dtype,
        )
        new_labels = (
            torch.full(
                (batch_size, new_seq_len),
                fill_value=-100,
                device=labels.device,
                dtype=labels.dtype,
            )
            if labels is not None
            else None
        )
        new_position_ids = (
            torch.zeros(
                (batch_size, new_seq_len),
                device=attention_mask.device,
                dtype=torch.long,
            )
            if position_ids is not None
            else None
        )

        for i in range(batch_size):
            idx_i: int = int(insert_idx[i].item()) - input_offset
            insert_len_i: int = int(insert_len[i].item())
            orig_len_i: int = int(num_tokens[i].item())
            new_len_i: int = int(new_num_tokens[i].item())

            # Update inputs_embeds
            new_inputs_embeds[i, :idx_i] = inputs_embeds[i, :idx_i]
            if insert_len_i > 0:
                new_inputs_embeds[i, idx_i : idx_i + insert_len_i] = insert_embeds[
                    i, :insert_len_i
                ]
            new_inputs_embeds[i, idx_i + insert_len_i : new_len_i] = inputs_embeds[
                i, idx_i:orig_len_i
            ]

            # Update attention_mask
            new_attention_mask[i, :idx_i] = attention_mask[i, :idx_i]
            if insert_len_i > 0:
                new_attention_mask[i, idx_i : idx_i + insert_len_i] = (
                    1  # Set attention mask for new tokens
                )
            new_attention_mask[i, idx_i + insert_len_i : new_len_i] = attention_mask[
                i, idx_i:orig_len_i
            ]

            # Update labels if provided
            if labels is not None and new_labels is not None:
                new_labels[i, :idx_i] = labels[i, :idx_i]
                if insert_len_i > 0:
                    new_labels[i, idx_i : idx_i + insert_len_i] = -100
                new_labels[i, idx_i + insert_len_i : new_len_i] = labels[
                    i, idx_i:orig_len_i
                ]

            # Update position_ids if provided
            if position_ids is not None and new_position_ids is not None:
                new_position_ids[i, :idx_i] = position_ids[i, :idx_i]
                if insert_len_i > 0:
                    new_position_ids[i, idx_i : idx_i + insert_len_i] = position_ids[
                        i, idx_i
                    ].item() + torch.arange(
                        insert_len_i,
                        device=position_ids.device,
                        dtype=position_ids.dtype,
                    )
                new_position_ids[i, idx_i + insert_len_i : new_len_i] = (
                    position_ids[i, idx_i:orig_len_i] + insert_len_i
                )

        # Add past attention mask if it exists
        if past_attention_mask is not None:
            new_attention_mask = torch.cat(
                [past_attention_mask, new_attention_mask], dim=1
            )

        # Update cache_position if provided
        new_cache_position = cache_position
        if cache_position is not None:
            max_inserted = new_seq_len - seq_len
            new_cache_position = torch.cat(
                [
                    cache_position,
                    torch.arange(
                        cache_position[-1].item() + 1,
                        cache_position[-1].item() + 1 + max_inserted,
                        device=cache_position.device,
                        dtype=cache_position.dtype,
                    ),
                ]
            )

        return (
            new_inputs_embeds,
            new_attention_mask,
            new_labels,
            new_position_ids,
            new_cache_position,
        )

    def _compute_kl_loss(
        self,
        student_output: transformers.modeling_outputs.CausalLMOutputWithPast,
        teacher_output: transformers.modeling_outputs.CausalLMOutputWithPast,
        student_labels: torch.Tensor,
        teacher_labels: Optional[torch.Tensor] = None,
        student_input_len: Optional[torch.Tensor] = None,
        teacher_input_len: Optional[torch.Tensor] = None,
        input_start_idx: Optional[torch.Tensor] = None,
    ) -> Dict[LossFunction, torch.Tensor]:
        losses: Dict[LossFunction, torch.Tensor] = {}
        # compute the KL divergence loss between the two models
        if LossFunction.Response_KL in self.loss_config.loss_weights:
            loss = F.kl_div(
                F.log_softmax(
                    student_output.logits[student_labels != -100]
                    / self.loss_config.kl_temperature,
                    dim=-1,
                ),
                F.softmax(
                    teacher_output.logits[teacher_labels != -100]
                    / self.loss_config.kl_temperature,
                    dim=-1,
                ),
                reduction="batchmean",
            )
            losses[LossFunction.Response_KL] = loss
        if (
            LossFunction.Input_KL in self.loss_config.loss_weights
            and student_input_len is not None
        ):
            if input_start_idx is None or teacher_input_len is None:
                raise ValueError(
                    "audio_labels, audio_start_idx, and transcript_len must be provided for computing input KL loss"
                )

            # Check that audio_len equals transcript_len
            if not torch.all(torch.eq(student_input_len, teacher_input_len)):
                raise ValueError(
                    "audio_len must be equal to transcript_len for all samples in the batch for computing input KL loss"
                )
            # compute the KL divergence loss for audio tokens
            audio_mask = (
                input_start_idx.unsqueeze(1)
                <= torch.arange(student_labels.size(1), device=student_labels.device)
            ) & (
                input_start_idx.unsqueeze(1) + student_input_len.unsqueeze(1)
                > torch.arange(student_labels.size(1), device=student_labels.device)
            )

            loss = F.kl_div(
                F.log_softmax(
                    student_output.logits[audio_mask] / self.loss_config.kl_temperature,
                    dim=-1,
                ),
                F.softmax(
                    teacher_output.logits[audio_mask]
                    / self.loss_config.kl_temperature,
                    dim=-1,
                ),
                reduction="batchmean",
            )
            losses[LossFunction.Input_KL] = loss
        return losses

    def _process_audio_input(
        self,
        inputs_embeds: torch.Tensor,
        attention_mask: torch.Tensor,
        audio_values: torch.FloatTensor,
        audio_len: torch.Tensor,
        audio_start_idx: torch.Tensor,
        transcript_len: torch.Tensor,
        labels: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.Tensor] = None,
        cache_position: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor], torch.Tensor]:
        # Sanity checks
        if audio_values is None or audio_len is None or audio_start_idx is None:
            raise ValueError("audio_values, audio_len, and audio_start_idx must be provided for audio input processing")
        if not (len(audio_start_idx) == len(audio_len) == len(audio_values)):
            raise ValueError("audio_start_idx, audio_len, and audio_values must have the same batch size")
        if inputs_embeds.shape[0] != audio_values.shape[0]:
            raise ValueError(f"Batch size mismatch: inputs_embeds has shape {inputs_embeds.shape}, but audio_values has shape {audio_values.shape}")
        if attention_mask.shape != inputs_embeds.shape[:2]:
            raise ValueError(f"Attention mask shape {attention_mask.shape} does not match inputs_embeds shape {inputs_embeds.shape[:2]}")
        if labels is not None and labels.shape != inputs_embeds.shape[:2]:
            raise ValueError(f"Labels shape {labels.shape} does not match inputs_embeds shape {inputs_embeds.shape[:2]}")
        if position_ids is not None and position_ids.shape != inputs_embeds.shape[:2]:
            raise ValueError(f"Position IDs shape {position_ids.shape} does not match inputs_embeds shape {inputs_embeds.shape[:2]}")
        if cache_position is not None and cache_position.shape[0] != inputs_embeds.shape[1]:
            raise ValueError(f"Cache position shape {cache_position.shape[0]} does not match inputs_embeds sequence length {inputs_embeds.shape[1]}")
       
        # Process audio through audio tower
        audio_tower_output = self.audio_tower(audio_values).last_hidden_state
        audio_tower_output = audio_tower_output.to(self.language_model.dtype)

        # Get audio feature lengths
        audio_feature_len = self.audio_tower._get_feat_extract_output_lengths(audio_len)

        # Process through adapter
        audio_embeds, num_audio_tokens, num_pred_audio_tokens = self.adapter(
            audio_tower_output, audio_feature_len, transcript_len
        )

        # Insert audio embeddings
        new_inputs_embeds, new_attention_mask, new_labels, new_position_ids, new_cache_position = self._insert_tokens(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            insert_idx=audio_start_idx,
            insert_embeds=audio_embeds,
            insert_len=num_audio_tokens,
            labels=labels,
            position_ids=position_ids,
            cache_position=cache_position
        )
        return new_inputs_embeds, new_attention_mask, new_labels, new_position_ids, new_cache_position, num_audio_tokens, num_pred_audio_tokens

    def _process_text_input(
        self,
        inputs_embeds: torch.Tensor,
        attention_mask: torch.Tensor,
        audio_start_idx: torch.Tensor,
        transcript_ids: torch.Tensor,
        transcript_len: torch.Tensor,
        labels: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.Tensor] = None,
        cache_position: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
        # Sanity checks
        if transcript_ids is None or transcript_len is None or audio_start_idx is None:
            raise ValueError("transcript_ids, transcript_len, and audio_start_idx must be provided for text input processing")
        if not (len(audio_start_idx) == len(transcript_len) == len(transcript_ids)):
            raise ValueError("audio_start_idx, transcript_len, and transcript_ids must have the same batch size")
        if inputs_embeds.shape[0] != transcript_ids.shape[0]:
            raise ValueError(f"Batch size mismatch: inputs_embeds has shape {inputs_embeds.shape}, but transcript_ids has shape {transcript_ids.shape}")
        if attention_mask.shape != inputs_embeds.shape[:2]:
            raise ValueError(f"Attention mask shape {attention_mask.shape} does not match inputs_embeds shape {inputs_embeds.shape[:2]}")
        if labels is not None and labels.shape != inputs_embeds.shape[:2]:
            raise ValueError(f"Labels shape {labels.shape} does not match inputs_embeds shape {inputs_embeds.shape[:2]}")
        if position_ids is not None and position_ids.shape != inputs_embeds.shape[:2]:
            raise ValueError(f"Position IDs shape {position_ids.shape} does not match inputs_embeds shape {inputs_embeds.shape[:2]}")
        if cache_position is not None and cache_position.shape[0] != inputs_embeds.shape[1]:
            raise ValueError(f"Cache position shape {cache_position.shape[0]} does not match inputs_embeds sequence length {inputs_embeds.shape[1]}")
        # Check if transcript_len is less than or equal to the sequence length of transcript_ids
        if torch.any(transcript_len > transcript_ids.shape[1]):
            raise ValueError("transcript_len cannot be greater than the sequence length of transcript_ids")


        # Get transcript embeddings
        transcript_embeds = self.get_input_embeddings()(transcript_ids)

        # Insert transcript embeddings
        new_inputs_embeds, new_attention_mask, new_labels, new_position_ids, new_cache_position = self._insert_tokens(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            insert_idx=audio_start_idx,
            insert_embeds=transcript_embeds,
            insert_len=transcript_len,
            labels=labels,
            position_ids=position_ids,
            cache_position=cache_position
        )
        return new_inputs_embeds, new_attention_mask, new_labels, new_position_ids, new_cache_position

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        inputs_embeds: Optional[torch.Tensor] = None,
        labels: Optional[torch.Tensor] = None,
        audio_values: Optional[torch.FloatTensor] = None,
        audio_len: Optional[torch.Tensor] = None,
        audio_start_idx: Optional[torch.Tensor] = None,
        transcript_ids: Optional[torch.Tensor] = None,
        transcript_len: Optional[torch.Tensor] = None,
        past_key_values: Optional[Union[Tuple, transformers.cache_utils.Cache]] = None,
        **kwargs,
    ) -> Union[Tuple, transformers.modeling_outputs.CausalLMOutputWithPast]:
        """
        Forward pass for the Ultravox model.

        `input_ids` are the tokenized text input. They are embedded by the language model as usual.
        `audio_values` are processed by the audio encoder and then every `stack_factor` frames are stacked together and
        projected to the language model's embedding space using a few linear layers.
        The audio and text embeddings are merged together. A special token `<|audio|>` is used to indicate the start
        of the audio embeddings in the merged embeddings.

        Args:
            input_ids: The tokenized text input.
            audio_values: The processed audio values.
            inputs_embeds: The embeddings for the input tokens.
            labels: The tokenized text labels.
            attention_mask: The attention mask for the input.
            position_ids: The position ids for the input.
            past_key_values: The past key value cache for the language model attention layers.
            **kwargs: Additional keyword arguments. Passed directly to the language model.
        """
        if inputs_embeds is None:
            assert input_ids is not None, "You have to specify either input_ids or inputs_embeds"
            inputs_embeds = self.get_input_embeddings()(input_ids)

        orig_inputs_embeds = inputs_embeds
        orig_attention_mask = attention_mask
        orig_labels = labels
        orig_position_ids = kwargs.get("position_ids", None)
        orig_cache_position = kwargs.get("cache_position", None)

        if audio_values is not None:
            inputs_embeds, attention_mask, labels, position_ids, cache_position, num_audio_tokens, num_pred_audio_tokens = self._process_audio_input(
                inputs_embeds=orig_inputs_embeds,
                attention_mask=orig_attention_mask,
                audio_values=audio_values,
                audio_len=audio_len,
                audio_start_idx=audio_start_idx,
                transcript_len=transcript_len,
                labels=orig_labels,
                position_ids=orig_position_ids,
                cache_position=orig_cache_position
            )
            kwargs['position_ids'] = position_ids
            kwargs['cache_position'] = cache_position
        else:
            num_audio_tokens = None

        # Forward pass through language model
        lm_output = self.language_model.forward(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            labels=labels,
            past_key_values=past_key_values,
            **kwargs,
        )
        if self.training:
            losses: Dict[LossFunction, torch.Tensor] = {}
            if self.loss_config.contains_kl_loss:
                # Process text input for teacher model
                text_inputs_embeds, text_attention_mask, text_labels, text_position_ids, text_cache_position = self._process_text_input(
                    inputs_embeds=orig_inputs_embeds,
                    attention_mask=orig_attention_mask,
                    audio_start_idx=audio_start_idx,
                    transcript_ids=transcript_ids,
                    transcript_len=transcript_len,
                    labels=orig_labels,
                    position_ids=orig_position_ids,
                    cache_position=orig_cache_position
                )
                kwargs["position_ids"] = text_position_ids
                kwargs["cache_position"] = text_cache_position

                # Forward pass through teacher model
                with torch.no_grad():
                    text_lm_output = self.language_model.forward(
                        inputs_embeds=text_inputs_embeds,
                        attention_mask=text_attention_mask,
                        labels=text_labels,
                        past_key_values=past_key_values,
                        **kwargs,
                    )
                # Compute KL loss
                kl_loss = self._compute_kl_loss(
                    student_output=lm_output,
                    teacher_output=text_lm_output,
                    student_labels=labels,
                    teacher_labels=text_labels,
                    student_input_len=num_audio_tokens,
                    teacher_input_len=transcript_len,
                    input_start_idx=audio_start_idx,
                )
            for loss_fn, _ in self.loss_config.loss_weights.items():
                if loss_fn == LossFunction.Response_CE:
                    losses[loss_fn] = lm_output.loss
                elif loss_fn == LossFunction.Response_KL:
                    losses[loss_fn] = kl_loss[LossFunction.Response_KL]
                elif loss_fn == LossFunction.Input_KL:
                    losses[loss_fn] = kl_loss[LossFunction.Input_KL]
                elif loss_fn == LossFunction.CIF_L1:
                    assert (
                        transcript_len is not None
                    ), "transcript_len must be provided for computing CIF L1 loss"
                    losses[loss_fn] = F.l1_loss(
                        num_pred_audio_tokens / transcript_len,
                        torch.ones_like(transcript_len),
                        reduction="mean",
                    )
                else:
                    raise ValueError(f"Unsupported loss function: {loss_fn}")

            # Compute total loss after all individual losses are calculated
            total_loss = sum(
                weight * losses[loss_fn]
                for loss_fn, weight in self.loss_config.loss_weights.items()
            )
            assert isinstance(
                total_loss, torch.Tensor
            ), f"total_loss is not a tensor: {total_loss}"

            # Track and log losses
            self._track_and_log_losses(losses, total_loss.item())

            lm_output.loss = total_loss
        return lm_output


    def generate(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        audio_values: Optional[torch.FloatTensor] = None,
        audio_len: Optional[torch.Tensor] = None,
        audio_start_idx: Optional[torch.Tensor] = None,
        **kwargs
    ) -> Union[transformers.modeling_outputs.CausalLMOutputWithPast, torch.LongTensor]:
        # Prepare inputs
        if inputs_embeds is None:
            assert input_ids is not None, "You have to specify either input_ids or inputs_embeds"
            inputs_embeds = self.get_input_embeddings()(input_ids)

        # Process audio input if provided
        if audio_values is not None:
            assert audio_len is not None and audio_start_idx is not None, \
                "audio_len and audio_start_idx must be provided if audio_values are provided."
            
            inputs_embeds, attention_mask, _, position_ids, cache_position, _, _ = self._process_audio_input(
                inputs_embeds,
                attention_mask,
                audio_values,
                audio_len,
                audio_start_idx,
                None,  # labels are not needed for generation
                kwargs.get('position_ids', None),
                kwargs.get('cache_position', None)
            )
            # Update kwargs with processed audio inputs
            if position_ids is not None:
                kwargs['position_ids'] = position_ids
            if cache_position is not None:
                kwargs['cache_position'] = cache_position
    
        # Remove transcript_len and transcript_ids from kwargs
        kwargs.pop('transcript_len', None)
        kwargs.pop('transcript_ids', None)

        seq_len = inputs_embeds.size(1)
        valid_tokens = attention_mask.sum(dim=1)
        shift = seq_len - valid_tokens
        
        # Change the padding to the left
        for i in range(inputs_embeds.size(0)):
            inputs_embeds[i] = torch.roll(inputs_embeds[i], shifts=shift[i].item(), dims=0)
            attention_mask[i] = torch.roll(attention_mask[i], shifts=shift[i].item(), dims=0)

        # Call language model's generate method
        return self.language_model.generate(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            **kwargs
        )

    def prepare_inputs_for_generation(
        self,
        input_ids: torch.Tensor,
        audio_values: Optional[torch.FloatTensor] = None,
        audio_start_idx: Optional[torch.Tensor] = None,
        audio_len: Optional[torch.Tensor] = None,
        past_key_values: Optional[Union[Tuple, transformers.cache_utils.Cache]] = None,
        attention_mask: Optional[torch.Tensor] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
        cache_position: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        model_input = self.language_model.prepare_inputs_for_generation(
            input_ids=input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            cache_position=cache_position,
            **kwargs,
        )

        # include audio information in model_input only when it is needed during prefilling
        # audio_token_start_idx should always be relative to the current cache position
        prefill_start_idx = 0 if cache_position is None else cache_position[0]
        if (
            audio_values is not None
            and audio_start_idx is not None
            and prefill_start_idx <= torch.max(audio_start_idx)
        ):
            model_input["audio_values"] = audio_values
            model_input["audio_start_idx"] = audio_start_idx
            model_input["audio_len"] = audio_len

        return model_input

    @classmethod
    def _create_adapter(cls, config: UltravoxConfig) -> "UltravoxAdapter":
        if config.adapter_type is AdapterType.STACKING:
            adapter = StackingAdapter(config)
        elif config.adapter_type is AdapterType.CFORMER:
            adapter = CFormerAdapter(config)
        else:
            raise ValueError(f"Unsupported adapter type: {config.adapter_type}")
        return adapter

    @classmethod
    def _create_audio_tower(
        cls, config: UltravoxConfig
    ) -> Union[transformers.Wav2Vec2Model, "ModifiedWhisperEncoder"]:
        if config.audio_model_id is not None:
            if "whisper" in config.audio_model_id is not None:
                audio_tower = ModifiedWhisperEncoder.from_pretrained(
                    config.audio_model_id
                )
            else:
                audio_tower = transformers.AutoModel.from_pretrained(
                    config.audio_model_id
                )
        else:
            if "whisper" in config.audio_config._name_or_path:
                audio_tower = ModifiedWhisperEncoder(config.audio_config)
            else:
                with transformers.modeling_utils.no_init_weights():
                    # we only ever use from_config if the weights are retrained, hence initializing is not
                    # required. This makes the model quite creation faster since init on CPU is quite slow.
                    audio_tower = transformers.AutoModel.from_config(
                        config.audio_config
                    )

        if isinstance(
            audio_tower,
            (transformers.Wav2Vec2BertModel, transformers.WhisperModel),
        ):
            # For these models we only need the encoder part
            # Wav2Vec2BertModel -> Wav2Vec2BertEncoder
            # WhisperModel -> WhisperEncoder
            audio_tower = audio_tower.encoder

        audio_tower = apply_lora(audio_tower, config.audio_model_lora_config)
        return audio_tower

    @classmethod
    def _create_language_model(
        cls, config: UltravoxConfig
    ) -> transformers.LlamaForCausalLM:
        if config.text_model_id is not None:
            language_model = transformers.AutoModelForCausalLM.from_pretrained(
                config.text_model_id, attn_implementation=config._attn_implementation
            )
        else:
            with transformers.modeling_utils.no_init_weights():
                # we only ever use from_config if the weights are retrained, hence initializing is not
                # required. This makes the model quite creation faster since init on CPU is quite slow.
                language_model = transformers.AutoModelForCausalLM.from_config(
                    config.text_config, attn_implementation=config._attn_implementation
                )

        language_model = apply_lora(language_model, config.text_model_lora_config)
        return language_model

    def merge_and_unload(self):
        if isinstance(self.language_model, peft.PeftModel):
            self.language_model = self.language_model.merge_and_unload()
            # no need to download base language model weights anymore, so we can remove the id
            self.config.text_model_id = None
            self.keep_params.update(
                set(
                    [
                        f"language_model.{name}"
                        for name, _ in self.language_model.named_parameters()
                    ]
                )
            )

        if isinstance(self.audio_tower, peft.PeftModel):
            self.audio_tower = self.audio_tower.merge_and_unload()
            # no need to download base audio model weights anymore, so we can remove the id
            self.config.audio_model_id = None
            self.keep_params.update(
                set(
                    [
                        f"audio_tower.{name}"
                        for name, _ in self.audio_tower.named_parameters()
                    ]
                )
            )

        for param in ["text_model_lora_config", "audio_model_lora_config"]:
            if hasattr(self.config, param):
                delattr(self.config, param)

    def push_to_hub(self, *args, **kwargs):
        self.merge_and_unload()
        self.to(self.language_model.dtype)
        return super().push_to_hub(*args, **kwargs)

    def state_dict(self, *args, **kwargs):
        named_params = dict(self.named_parameters())
        state_dict = super().state_dict(*args, **kwargs)

        state_dict = {
            k: v
            for k, v in state_dict.items()
            if k in self.keep_params
            or (k in named_params and named_params[k].requires_grad)
        }
        return state_dict

    def load_state_dict(
        self,
        state_dict: Dict[str, Any],
        *args,
        **kwargs,
    ):
        self.keep_params.update(set(state_dict.keys()))
        return super().load_state_dict(state_dict, *args, **kwargs)

    def print_trainable_parameters(self):
        """
        Prints the number of trainable parameters in the model (reuses Peft model's method)
        """
        count_params = peft.peft_model.PeftModel.get_nb_trainable_parameters

        trainable_params, all_param = count_params(self)

        logging.info(
            f"trainable params: {trainable_params:,d} || all params: {all_param:,d}"
            f" || trainable%: {100 * trainable_params / all_param:.1f}%"
        )

        lm_trainable_params, lm_all_params = count_params(self.language_model)
        audio_trainable_params, audio_all_params = count_params(self.audio_tower)

        adapter_trainable_params = (
            trainable_params - lm_trainable_params - audio_trainable_params
        )
        projector_all_params = all_param - lm_all_params - audio_all_params

        logging.info(
            f"Trainable%:   "
            f" LLM: {100 * lm_trainable_params / lm_all_params:.1f}%"
            f" || Audio Encoder: {100 * audio_trainable_params / audio_all_params:.1f}%"
            f" || Adapter: {100 * adapter_trainable_params / projector_all_params:.1f}%"
        )


def is_cache_empty(
    past_key_values: Optional[Union[Tuple, transformers.cache_utils.Cache]]
) -> bool:
    """
    Check if the cache is empty.
    """
    if past_key_values is None:
        return True
    if isinstance(past_key_values, tuple):
        return all(len(c) == 0 for c in past_key_values)
    return past_key_values.get_seq_length() == 0


def apply_lora(model: torch.nn.Module, lora_config: dict) -> torch.nn.Module:
    """
    Applies LoRA finetuning to the model. If the `r` parameter is set to 0, the model is frozen instead.
    """
    lora_config = peft.LoraConfig(**lora_config or {})

    if lora_config.r == 0:
        # freeze the model entirely
        for param in model.parameters():
            param.requires_grad = False
    else:
        model = peft.get_peft_model(model, lora_config)

    return model


class ModifiedWhisperEncoder(whisper.WhisperEncoder):
    """
    Encoder portion of OpenAI's Whisper model.

    This implementation is a slightly modified version of HF Transformers' Whisper Encoder, with only a few fixes:
    1. base_model_prefix updated to allow for doing `.from_pretrained` directly on the encoder
    2. allow less than 30 second of audio padding to be passed in:
        - relaxed ValueError check for `input_features` length to be less than or equal to `expected_seq_length` instead of strictly equal
        - embed_pos is now sliced to match the length of `inputs_embeds`

    Original: https://github.com/huggingface/transformers/blob/main/src/transformers/models/whisper/modeling_whisper.py
    """

    base_model_prefix = "model.encoder"

    def _get_feat_extract_output_lengths(self, input_lengths: torch.Tensor):
        """
        Computes the output length of the convolutional layers
        """
        input_lengths = (input_lengths - 1) // 2 + 1

        return input_lengths

    def forward(
        self,
        input_features,
        attention_mask=None,
        head_mask=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        expected_seq_length = (
            self.config.max_source_positions
            * self.conv1.stride[0]
            * self.conv2.stride[0]
        )
        # Chunk the input_features if it exceeds expected_seq_length
        if input_features.shape[-1] > expected_seq_length:
            import warnings

            warnings.warn(
                f"Input features length ({input_features.shape[-1]}) exceeds the expected sequence length ({expected_seq_length}). The input will be chunked to {expected_seq_length}."
            )
            input_features = input_features[..., :expected_seq_length]

        output_attentions = (
            output_attentions
            if output_attentions is not None
            else self.config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )
        inputs_embeds = nn.functional.gelu(self.conv1(input_features))
        inputs_embeds = nn.functional.gelu(self.conv2(inputs_embeds))

        inputs_embeds = inputs_embeds.permute(0, 2, 1)
        embed_pos = self.embed_positions.weight[: inputs_embeds.size(-2)]

        hidden_states = inputs_embeds + embed_pos
        hidden_states = nn.functional.dropout(
            hidden_states, p=self.dropout, training=self.training
        )

        encoder_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None

        # check if head_mask has a correct number of layers specified if desired
        if head_mask is not None:
            assert head_mask.size()[0] == (
                len(self.layers)
            ), f"The head_mask should be specified for {len(self.layers)} layers, but it is for {head_mask.size()[0]}."

        for idx, encoder_layer in enumerate(self.layers):
            if output_hidden_states:
                encoder_states = encoder_states + (hidden_states,)
            # add LayerDrop (see https://arxiv.org/abs/1909.11556 for description)
            to_drop = False
            if self.training:
                dropout_probability = torch.rand([])
                if dropout_probability < self.layerdrop:  # skip the layer
                    to_drop = True

            if to_drop:
                layer_outputs = (None, None)
            else:
                if self.gradient_checkpointing and self.training:
                    layer_outputs = self._gradient_checkpointing_func(
                        encoder_layer.__call__,
                        hidden_states,
                        None,
                        (head_mask[idx] if head_mask is not None else None),
                        output_attentions,
                    )
                else:
                    layer_outputs = encoder_layer(
                        hidden_states,
                        None,
                        layer_head_mask=(
                            head_mask[idx] if head_mask is not None else None
                        ),
                        output_attentions=output_attentions,
                    )

                hidden_states = layer_outputs[0]

            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[1],)

        hidden_states = self.layer_norm(hidden_states)
        if output_hidden_states:
            encoder_states = encoder_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, encoder_states, all_attentions]
                if v is not None
            )
        return transformers.modeling_outputs.BaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=encoder_states,
            attentions=all_attentions,
        )


class RMSNorm(transformers.models.llama.modeling_llama.LlamaRMSNorm):
    def __init__(self, hidden_size: int, init: float = 1, eps: float = 1e-6):
        super().__init__(hidden_size=hidden_size, eps=eps)
        self.weight.data.fill_(init)


# currently attention_mask is not yet implemented in the forward method
class UltravoxAdapter(nn.Module, transformers.modeling_utils.ModuleUtilsMixin):
    def __init__(self, config: UltravoxConfig) -> None:
        super().__init__()
        audio_config: Union[Wav2Vec2Config, WhisperConfig] = config.audio_config
        text_config: transformers.LlamaConfig = config.text_config

        self.input_size = audio_config.hidden_size
        # self.hidden_size always matches audio_config.hidden_size
        self.hidden_size = audio_config.hidden_size
        self.output_size = text_config.hidden_size

        self.post_ln = RMSNorm(self.hidden_size, init=config.norm_init)
        self.text_proj = nn.Linear(self.hidden_size, self.output_size)

    def forward(
        self,
        audio_features: torch.Tensor,
        num_frames: torch.Tensor,
        num_text_tokens: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        raise NotImplementedError(
            "Subclasses must implement this method to return a tuple of (hidden_states, num_audio_tokens, num_pred_audio_tokens)"
        )

    def project_to_text(self, hidden_states):
        hidden_states = self.post_ln(hidden_states)
        hidden_states = self.text_proj(hidden_states)
        return hidden_states


class SwiGLU(nn.Module):
    def forward(self, x):
        x, gate = x.chunk(2, dim=-1)
        return F.silu(gate) * x


class StackAudioFrames(nn.Module):
    def __init__(self, stack_factor: int):
        super().__init__()
        self.stack_factor = stack_factor

    def forward(
        self, x: torch.Tensor, num_frames: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if self.stack_factor == 1:
            return x, num_frames

        b, t, d = x.shape

        # Pad the audio features
        pad = (self.stack_factor - (t % self.stack_factor)) % self.stack_factor
        x = F.pad(x, (0, 0, 0, pad))

        # Stack the audio features
        x = x.reshape(b, -1, d * self.stack_factor)

        # Calculate the new number of frames after stacking
        num_frames = torch.ceil(num_frames.float() / self.stack_factor).long()

        return x, num_frames


class StackingAdapter(UltravoxAdapter):
    def __init__(self, config: UltravoxConfig):
        super().__init__(config)

        self.config = UltravoxStackingAdapterConfig(**config.adapter_config)

        self._pad_and_stack = StackAudioFrames(self.config.stack_factor)
        stacked_size = self.input_size * self.config.stack_factor
        self.ln_pre = RMSNorm(stacked_size, init=config.norm_init)
        # swiglu reduces dimension by 2, so we double it here before swigu to keep effective hidden size consistent.
        intermediate_size = (
            self.hidden_size * 2
            if self.config.activation == "swiglu"
            else self.hidden_size
        )
        self.linear_1 = nn.Linear(stacked_size, intermediate_size, bias=False)
        self.act = transformers.activations.get_activation(self.config.activation)

    def forward(
        self,
        audio_features: torch.Tensor,
        num_frames: torch.Tensor,
        num_text_tokens: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        hidden_states, num_audio_tokens = self._pad_and_stack(
            audio_features, num_frames
        )
        hidden_states = self.ln_pre(hidden_states)
        hidden_states = self.linear_1(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.project_to_text(hidden_states)
        return hidden_states, num_audio_tokens, num_audio_tokens


class CFormerAdapter(UltravoxAdapter):
    def __init__(self, config: UltravoxConfig):
        super().__init__(config)

        self.config = UltravoxCFormerAdapterConfig(**config.adapter_config)

        self.num_pre_cif_layers = self.config.num_pre_cif_layers
        self.num_post_cif_layers = self.config.num_post_cif_layers

        if self.num_pre_cif_layers or self.num_post_cif_layers:
            if config.audio_config.model_type == "whisper":
                transformer_layer_class = whisper.WhisperEncoderLayer
            elif config.audio_config.model_type == "wav2vec2":
                transformer_layer_class = wav2vec2.Wav2Vec2EncoderLayer
            else:
                raise ValueError(
                    f"Unsupported audio model type: {config.audio_config.model_type}"
                )

        if self.num_pre_cif_layers > 0:
            self.pre_cif_layers = nn.ModuleList(
                [
                    transformer_layer_class(config.audio_config)
                    for _ in range(self.num_pre_cif_layers)
                ]
            )

        self.cif_proj = nn.Linear(self.hidden_size - 1, self.hidden_size)

        if self.num_post_cif_layers > 0:
            self.post_cif_layers = nn.ModuleList(
                [
                    transformer_layer_class(config.audio_config)
                    for _ in range(self.num_post_cif_layers)
                ]
            )

    # This implements the continuous integrate-and-fire mechanism adapted from this paper: https://arxiv.org/abs/1905.11235
    # TODO: add support for attention_mask
    def forward_cif(
        self,
        hidden_states: torch.Tensor,
        alphas: torch.Tensor,
        num_tokens: torch.Tensor,
    ) -> torch.Tensor:
        device = hidden_states.device
        B, T, _ = hidden_states.size()

        max_num_tokens = int(num_tokens.max().item())

        # loop vars
        integrate = torch.zeros(
            [B], device=device, dtype=hidden_states.dtype
        )  # accumulated alpha value that hasn't benen fired yet
        remainds = torch.zeros(
            [B], device=device, dtype=hidden_states.dtype
        )  # reamining alpha value from recent firing
        token_index = torch.zeros(
            [B], device=device, dtype=torch.long
        )  # num of fires that has happened

        # weights: B x max_num_tokens x T, weights[i, j, k] is the contribution of the k-th speech feature to the j-th text/speech token for the i-th sample
        weights = torch.zeros(
            [B, max_num_tokens, T], device=device, dtype=hidden_states.dtype
        )
        for t in range(T):
            if t > 0:
                weights[:, :, t - 1].scatter_add_(
                    dim=1, index=token_index.unsqueeze(1), src=remainds.unsqueeze(1)
                )

            alpha = alphas[:, t]
            alpha_needed = 1 - integrate
            integrate += alpha
            ready_to_fire = integrate >= 1.0

            while True:  # allow repeated firing if integrate > threshold
                integrate = torch.where(ready_to_fire, integrate - 1, integrate)
                alpha_integrated = torch.where(ready_to_fire, alpha_needed, alpha)

                # print(f"alpha_integrated.dtype: {alpha_integrated.dtype}")
                # print(f"token_index.dtype: {token_index.dtype}")
                # print(f"weights.dtype: {weights.dtype}")
                weights[:, :, t].scatter_(
                    dim=1,
                    index=token_index.unsqueeze(1),
                    src=alpha_integrated.unsqueeze(1),
                )
                remainds = alpha - alpha_integrated

                token_index = token_index + ready_to_fire.type_as(token_index)
                token_index = torch.minimum(token_index, num_tokens - 1)

                alpha = remainds
                alpha_needed = 1
                ready_to_fire = integrate >= 1.0
                if not ready_to_fire.any():
                    break

        # the resulting hidden_states contains the hidden states of speech tokens right after CIF mechanism
        hidden_states = weights.type_as(hidden_states).bmm(hidden_states)

        return hidden_states

    def forward(
        self,
        audio_features: torch.Tensor,
        num_frames: torch.Tensor,
        num_text_tokens: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        # num_required_audio_tokens needs to be provided in the training mode as it's used for scaling alphas
        # in inference mode, num_required_audio_tokens is None as input and is determined by the accumulated predicted alpha values

        hidden_states = audio_features
        T = hidden_states.size(1)

        attention_mask = torch.arange(
            audio_features.shape[1], device=hidden_states.device
        ) < num_frames.unsqueeze(1)

        for layer in self.pre_cif_layers:
            hidden_states = layer(hidden_states, None, None)[0]

        # alphas is computed from the last element of hidden_states using a sigmoid function, and used to assign speech features to text/speech tokens.
        alphas = torch.sigmoid(hidden_states[:, :, -1])
        alphas = alphas * attention_mask
        num_pred_audio_tokens = alphas.sum(-1)

        if self.training:
            if num_text_tokens is None:
                raise ValueError(
                    "num_required_audio_tokens must be provided in training mode"
                )
            num_audio_tokens = num_text_tokens
        else:
            # num_tokens is determined by accumulated predicted alpha values in inference mode
            num_audio_tokens = torch.round(num_pred_audio_tokens).int()
            # force the number of predicted tokens to be at least 1 in non-streaming mode
            # this will break streaming mode and needs to be updated
            num_audio_tokens[num_audio_tokens < 1] = 1

        # scale alphas so that the sum of alphas is equal to num_tokens
        alphas = alphas * (num_audio_tokens / num_pred_audio_tokens)[:, None].repeat(
            1, T
        ).to(dtype=hidden_states.dtype)

        # remove the last element of hidden_states and apply CIF mechanism
        hidden_states = self.forward_cif(
            hidden_states[:, :, :-1], alphas, num_audio_tokens
        )
        # project back to self.hidden_size
        hidden_states = self.cif_proj(hidden_states)
        # Create attention mask based on num_tokens
        attention_mask = (
            torch.arange(hidden_states.shape[1], device=hidden_states.device)[None, :]
            < num_audio_tokens[:, None]
        )
        attention_mask = attention_mask.to(dtype=hidden_states.dtype)
        extended_attention_mask = self.get_extended_attention_mask(
            attention_mask,
            num_audio_tokens.shape,
            dtype=attention_mask.dtype,
        ).to(hidden_states.device)
        for layer in self.post_cif_layers:
            hidden_states = layer(hidden_states, extended_attention_mask, None)[0]

        hidden_states = self.project_to_text(hidden_states)

        return hidden_states, num_audio_tokens, num_pred_audio_tokens


transformers.activations.ACT2FN["swiglu"] = SwiGLU


UltravoxModel.register_for_auto_class()
transformers.AutoModel.register(UltravoxConfig, UltravoxModel)
