from typing import Optional

import torch
import transformers
import transformers.quantizers

from ultravox.inference import infer
from ultravox.inference import utils
from ultravox.model import ultravox_model
from ultravox.model import ultravox_processing
from ultravox.model import wandb_utils


class UltravoxInference(infer.LocalInference):
    def __init__(
        self,
        model_path: str,
        audio_processor_id: Optional[str] = None,
        tokenizer_id: Optional[str] = None,
        device: Optional[str] = None,
        data_type: Optional[str] = None,
        quant_bits: Optional[int] = None,
    ):
        """
        Args:
            model_path: can refer to a HF hub model_id, a local path, or a W&B artifact
                Examples:
                    fixie-ai/ultravox
                    runs/llama2_asr_gigaspeech/checkpoint-1000/
                    wandb://fixie/ultravox/model-llama2_asr_gigaspeech:v0
            audio_processor_id: model_id for the audio processor to use. If not provided, it will be inferred
            tokenizer_id: model_id for the tokenizer to use. If not provided, it will be inferred
            device: where to put the model and data
            data_type: data type to use for the model
        """
        device = device or utils.default_device()
        dtype = utils.get_dtype(data_type) if data_type else utils.default_dtype()

        if wandb_utils.is_wandb_url(model_path):
            model_path = wandb_utils.download_model_from_wandb(model_path)
        model = ultravox_model.UltravoxModel.from_pretrained(
            model_path,
            torch_dtype=dtype,
        )
        model.to(dtype=dtype, device=device)
        model.merge_and_unload()

        if quant_bits is not None:
            if quant_bits == 8:
                bnb_kwargs = {"load_in_8bit": True}
            elif quant_bits == 4:
                bnb_kwargs = {
                    "load_in_4bit": True,
                    "bnb_4bit_compute_dtype": torch.bfloat16,
                }
            else:
                raise ValueError(f"Unsupported quant_bits: {quant_bits}")

            quant_config = transformers.BitsAndBytesConfig(**bnb_kwargs)
            hf_quantizer = transformers.quantizers.AutoHfQuantizer.from_config(
                quant_config
            )
            hf_quantizer.preprocess_model(model, device_map=device)

        tokenizer_id = tokenizer_id or model_path
        tokenizer = transformers.AutoTokenizer.from_pretrained(tokenizer_id)

        tokenizer.padding_side = "left"
        tokenizer.pad_token = tokenizer.eos_token

        # tincans-ai models don't set audio_model_id, instead audio_config._name_or_path has the
        # model name. A default value is added just as a precaution, but it shouldn't be needed.
        audio_processor = transformers.AutoProcessor.from_pretrained(
            audio_processor_id
            or model.config.audio_model_id
            or model.config.audio_config._name_or_path
            or "facebook/wav2vec2-base-960h"
        )

        processor = ultravox_processing.UltravoxProcessor(
            audio_processor, tokenizer=tokenizer, stack_factor=model.config.stack_factor
        )

        super().__init__(
            model=model,
            processor=processor,
            tokenizer=tokenizer,
            device=device,
            dtype=dtype,
        )
