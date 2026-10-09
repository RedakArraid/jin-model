from __future__ import annotations

from pathlib import Path
from typing import Any

from ..common import compact_common, parse_json_object
from ..prompts import extraction_prompt
from ..render import contact_sheet
from .base import BenchmarkAdapter


MODEL_SPECS = {
    "granite_docling_258m": {
        "model_id": "ibm-granite/granite-docling-258M",
        "loader": "multimodal",
        "license": "apache-2.0",
        "track": "direct_ie",
    },
    "glm_ocr": {
        "model_id": "zai-org/GLM-OCR",
        "loader": "glm_ocr",
        "license": "mit",
        "track": "direct_ie",
    },
    "paddleocr_vl_1_6": {
        "model_id": "PaddlePaddle/PaddleOCR-VL-1.6",
        "loader": "paddle_remote",
        "license": "apache-2.0",
        "track": "direct_ie",
    },
    "qwen3_vl_2b": {
        "model_id": "Qwen/Qwen3-VL-2B-Instruct",
        "loader": "multimodal",
        "license": "apache-2.0",
        "track": "direct_ie",
    },
    "qwen3_vl_4b": {
        "model_id": "Qwen/Qwen3-VL-4B-Instruct",
        "loader": "multimodal",
        "license": "apache-2.0",
        "track": "direct_ie",
    },
}


class TransformersVLMAdapter(BenchmarkAdapter):
    track = "direct_ie"

    def __init__(self, spec_name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if spec_name not in MODEL_SPECS:
            raise KeyError(spec_name)
        self.spec_name = spec_name
        self.spec = MODEL_SPECS[spec_name]
        self.name = spec_name
        self.model_id = self.spec["model_id"]

    def load(self) -> None:
        import torch
        from transformers import AutoProcessor

        loader = self.spec["loader"]
        if loader == "glm_ocr":
            from transformers import GlmOcrForConditionalGeneration
            model_cls = GlmOcrForConditionalGeneration
            trust_remote_code = False
        elif loader == "paddle_remote":
            from transformers import AutoModelForCausalLM
            model_cls = AutoModelForCausalLM
            trust_remote_code = True
        else:
            from transformers import AutoModelForMultimodalLM
            model_cls = AutoModelForMultimodalLM
            trust_remote_code = False

        self.processor = AutoProcessor.from_pretrained(
            self.model_id,
            trust_remote_code=trust_remote_code,
        )
        dtype = torch.float32 if self.device == "cpu" else torch.bfloat16
        kwargs: dict[str, Any] = {"trust_remote_code": trust_remote_code}
        if self.device == "auto":
            kwargs["device_map"] = "auto"
        else:
            kwargs["torch_dtype"] = dtype
        try:
            self.model = model_cls.from_pretrained(self.model_id, **kwargs)
        except TypeError:
            kwargs.pop("torch_dtype", None)
            self.model = model_cls.from_pretrained(self.model_id, **kwargs)
        if self.device != "auto":
            self.model = self.model.to(self.device)
        self.model.eval()
        parameter_count = sum(int(parameter.numel()) for parameter in self.model.parameters())
        parameter_bytes = sum(
            int(parameter.numel()) * int(parameter.element_size())
            for parameter in self.model.parameters()
        )
        cache_bytes = None
        try:
            from huggingface_hub import scan_cache_dir
            for repo in scan_cache_dir().repos:
                if repo.repo_id == self.model_id:
                    cache_bytes = int(repo.size_on_disk)
                    break
        except Exception:
            cache_bytes = None
        self.load_metadata = {
            "license": self.spec["license"],
            "loader": loader,
            "max_new_tokens": self.max_new_tokens,
            "parameter_count": parameter_count,
            "parameter_memory_bytes": parameter_bytes,
            "hf_cache_bytes": cache_bytes,
        }

    def _model_device(self):
        try:
            return self.model.device
        except Exception:
            return self.device

    def _prepare_inputs(self, image, prompt: str):
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }]
        try:
            inputs = self.processor.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                return_dict=True,
                return_tensors="pt",
            )
        except Exception:
            inputs = self.processor(
                text=prompt,
                images=image,
                return_tensors="pt",
            )
        if hasattr(inputs, "to"):
            inputs = inputs.to(self._model_device())
        else:
            inputs = {
                key: value.to(self._model_device()) if hasattr(value, "to") else value
                for key, value in inputs.items()
            }
        return inputs

    def extract(self, pdf_path: Path) -> dict[str, Any]:
        import torch

        image = contact_sheet(pdf_path, max_pages=self.max_pages)
        inputs = self._prepare_inputs(image, extraction_prompt())
        input_length = int(inputs["input_ids"].shape[-1]) if "input_ids" in inputs else 0
        with torch.inference_mode():
            output = self.model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
            )
        is_encoder_decoder = bool(
            getattr(getattr(self.model, "config", None), "is_encoder_decoder", False)
        )
        generated = (
            output[0]
            if is_encoder_decoder
            else output[0][input_length:] if input_length else output[0]
        )
        decoder = getattr(self.processor, "decode", None)
        if decoder is None:
            decoder = self.processor.tokenizer.decode
        text = decoder(generated, skip_special_tokens=True)
        parsed, error = parse_json_object(text)
        return {
            "prediction": compact_common(parsed or {}),
            "json_valid": parsed is not None,
            "parse_error": error,
            "raw_text": text,
        }
