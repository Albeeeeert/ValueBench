from __future__ import annotations

import gc
import importlib.util
import logging
import threading
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from typing import Any

from ..config import LocalImageConfig


class FatalLocalImageError(RuntimeError):
    """A local model failure that must stop the image stage."""


def inspect_local_image(config: LocalImageConfig) -> dict[str, Any]:
    """Inspect local prerequisites without loading weights or accessing the network."""
    missing = [
        name for name in ("torch", "diffusers", "transformers", "accelerate", "safetensors")
        if importlib.util.find_spec(name) is None
    ]
    model_ready = (Path(config.model_path) / "model_index.json").is_file()
    cuda_ready = False
    indices = config.cuda_indices(0)
    error = ""
    if "torch" not in missing:
        try:
            import torch

            device_count = torch.cuda.device_count()
            indices = config.cuda_indices(device_count)
            cuda_ready = torch.cuda.is_available() and bool(indices) and all(
                index < device_count for index in indices
            )
        except Exception as exc:
            error = str(exc)
    return {
        "model_index_present": model_ready,
        "missing_dependencies": missing,
        "cuda_device_available": cuda_ready,
        "required_cuda_devices": [f"cuda:{index}" for index in indices],
        "error": error,
        "ready": model_ready and not missing and cuda_ready,
        "weights_loaded": False,
    }


class _LocalRuntime:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pipe: Any = None
        self.torch: Any = None
        self.failure: FatalLocalImageError | None = None


class LocalQwenImageClient:
    """Lightweight per-task clients share one lazily loaded, serialized pipeline."""

    def __init__(
        self,
        config: LocalImageConfig,
        *,
        logger: logging.Logger | None = None,
        _runtime: _LocalRuntime | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.request_ids: list[str] = []
        self._runtime = _runtime or _LocalRuntime()

    def clone(self) -> "LocalQwenImageClient":
        return LocalQwenImageClient(self.config, logger=self.logger, _runtime=self._runtime)

    def generation_settings(self) -> dict[str, Any]:
        return {
            "backend": "local", **asdict(self.config),
            "generator_device": "cpu",
        }

    def _load(self) -> None:
        runtime = self._runtime
        if runtime.failure is not None:
            raise runtime.failure
        if runtime.pipe is not None:
            return
        try:
            if not (Path(self.config.model_path) / "model_index.json").is_file():
                raise FileNotFoundError(
                    "local_image.model_path must contain a complete Diffusers model directory "
                    "including model_index.json"
                )
            try:
                import torch
                from diffusers import QwenImagePipeline
            except ImportError as exc:
                raise RuntimeError(
                    "install the local image dependencies with: pip install -r requirements.txt"
                ) from exc
            runtime.torch = torch
            device_count = torch.cuda.device_count()
            indices = self.config.cuda_indices(device_count)
            if not torch.cuda.is_available() or not indices or any(
                device_index >= device_count for device_index in indices
            ):
                required = ", ".join(f"cuda:{device_index}" for device_index in indices) or "at least one visible GPU"
                raise RuntimeError(f"CUDA device unavailable: requires {required}")
            index = indices[0]
            self.logger.info("loading local Qwen-Image | model=%s", self.config.model_path)
            load_options: dict[str, Any] = {}
            if self.config.device_map == "balanced":
                load_options["device_map"] = "balanced"
            runtime.pipe = QwenImagePipeline.from_pretrained(
                self.config.model_path,
                torch_dtype=getattr(torch, self.config.dtype),
                local_files_only=True,
                **load_options,
            )
            if self.config.device_map == "balanced":
                return
            if self.config.cpu_offload == "sequential":
                runtime.pipe.enable_sequential_cpu_offload(gpu_id=index)
            elif self.config.cpu_offload == "model":
                runtime.pipe.enable_model_cpu_offload(gpu_id=index)
            else:
                runtime.pipe.to(self.config.device)
        except Exception as exc:
            runtime.pipe = None
            runtime.failure = FatalLocalImageError(f"local Qwen-Image loading failed: {exc}")
            raise runtime.failure from exc

    def generate(self, prompt: str) -> bytes:
        runtime = self._runtime
        with runtime.lock:
            self._load()
            torch = runtime.torch
            width, height = map(int, self.config.size.split("*"))
            try:
                with torch.inference_mode():
                    result = runtime.pipe(
                        prompt=prompt,
                        negative_prompt=self.config.negative_prompt,
                        width=width,
                        height=height,
                        num_inference_steps=self.config.num_inference_steps,
                        true_cfg_scale=self.config.true_cfg_scale,
                        generator=torch.Generator(device="cpu").manual_seed(self.config.seed),
                    ).images[0]
                buffer = BytesIO()
                result.save(buffer, format="PNG")
                return buffer.getvalue()
            except torch.cuda.OutOfMemoryError as exc:
                runtime.failure = FatalLocalImageError(
                    "local Qwen-Image ran out of CUDA memory; free GPU memory or switch to "
                    "local_image.device_map: null with cpu_offload: sequential"
                )
                raise runtime.failure from exc

    def close(self) -> None:
        with self._runtime.lock:
            self._runtime.pipe = None
            if self._runtime.torch is not None:
                torch = self._runtime.torch
                self._runtime.torch = None
                gc.collect()
                if torch.cuda.is_available():
                    for index in self.config.cuda_indices(torch.cuda.device_count()):
                        if index < torch.cuda.device_count():
                            with torch.cuda.device(f"cuda:{index}"):
                                torch.cuda.empty_cache()
