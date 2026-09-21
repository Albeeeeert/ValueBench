from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_SENSITIVE_KEYS = ("api_key", "authorization", "password", "secret", "token")

DEFAULT_SCENE_MIX_RATIOS = {
    "text_artifact": 0.15,
    "people_interaction": 0.50,
    "physical_scene": 0.20,
    "environment_context": 0.15,
}
DEFAULT_VISUAL_EVIDENCE_RATIOS = {
    "non_text": 0.85,
    "minimal_text": 0.10,
    "text_supported": 0.05,
}
DEFAULT_VISIBLE_TEXT_WORD_LIMITS = {
    "non_text": 0,
    "minimal_text": 8,
    "text_supported": 20,
}


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _resolve(root: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (root / path).resolve()


def _ratio_mapping(value: Any, defaults: dict[str, float], name: str) -> dict[str, float]:
    raw = defaults if value is None else _mapping(value, name)
    if set(raw) != set(defaults):
        raise ValueError(f"{name} must contain exactly: {', '.join(defaults)}")
    ratios = {key: float(raw[key]) for key in defaults}
    if any(number < 0 for number in ratios.values()) or abs(sum(ratios.values()) - 1.0) > 1e-9:
        raise ValueError(f"{name} values must be non-negative and sum to 1")
    return ratios


def _integer_mapping(value: Any, defaults: dict[str, int], name: str) -> dict[str, int]:
    raw = defaults if value is None else _mapping(value, name)
    if set(raw) != set(defaults):
        raise ValueError(f"{name} must contain exactly: {', '.join(defaults)}")
    parsed = {key: int(raw[key]) for key in defaults}
    if any(number < 0 for number in parsed.values()):
        raise ValueError(f"{name} values must be non-negative")
    return parsed


_PS1 = re.compile(r"^\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_DOTENV = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_env_file(path: Path | None) -> None:
    """Load simple dotenv or PowerShell assignments without executing the file."""
    if path is None or not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        match = _PS1.match(line) or _DOTENV.match(line)
        if not match:
            continue
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name, value)


@dataclass(frozen=True)
class ModelConfig:
    name: str
    model: str
    base_url: str
    api_key_env: str
    supports_images: bool = False
    temperature: float = 0.0
    max_tokens: int = 2000
    timeout_sec: int = 180
    max_retries: int = 3
    backoff_base_sec: float = 1.0
    backoff_jitter_sec: float = 0.5
    thinking: dict[str, Any] | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, name: str, raw: dict[str, Any]) -> "ModelConfig":
        required = ("model", "base_url", "api_key_env")
        missing = [key for key in required if not str(raw.get(key, "")).strip()]
        if missing:
            raise ValueError(f"models.{name} is missing: {', '.join(missing)}")
        return cls(
            name=name,
            model=str(raw["model"]),
            base_url=str(raw["base_url"]),
            api_key_env=str(raw["api_key_env"]),
            supports_images=bool(raw.get("supports_images", False)),
            temperature=float(raw.get("temperature", 0.0)),
            max_tokens=int(raw.get("max_tokens", 2000)),
            timeout_sec=int(raw.get("timeout_sec", raw.get("request_timeout_sec", 180))),
            max_retries=int(raw.get("max_retries", 3)),
            backoff_base_sec=float(raw.get("backoff_base_sec", 1.0)),
            backoff_jitter_sec=float(raw.get("backoff_jitter_sec", 0.5)),
            thinking=dict(raw["thinking"]) if isinstance(raw.get("thinking"), dict) else None,
            extra_body=dict(raw.get("extra_body", {})),
        )

    def api_key(self) -> str:
        value = os.getenv(self.api_key_env, "").strip()
        if not value:
            raise RuntimeError(f"missing API key environment variable: {self.api_key_env}")
        return value

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "supports_images": self.supports_images,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_sec": self.timeout_sec,
            "max_retries": self.max_retries,
            "backoff_base_sec": self.backoff_base_sec,
            "backoff_jitter_sec": self.backoff_jitter_sec,
            "thinking": self.thinking,
            "extra_body": _redact_mapping(self.extra_body),
        }


def _redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """防止供应商扩展参数中的凭据进入指纹、日志或 manifest。"""
    output: dict[str, Any] = {}
    for key, item in value.items():
        if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
            output[key] = "[REDACTED]"
        elif isinstance(item, dict):
            output[key] = _redact_mapping(item)
        elif isinstance(item, list):
            output[key] = [
                _redact_mapping(child) if isinstance(child, dict) else child
                for child in item
            ]
        else:
            output[key] = item
    return output


@dataclass(frozen=True)
class ImageModerationRetryConfig:
    enabled: bool = True
    model: str = "author"
    validator_model: str = "author"
    max_rewrites: int = 1

    @classmethod
    def from_mapping(cls, raw: Any, default_model: str) -> "ImageModerationRetryConfig":
        raw = _mapping(raw, "image.moderation_retry")
        unknown = set(raw) - {"enabled", "model", "validator_model", "max_rewrites"}
        if unknown:
            raise ValueError(f"unknown image.moderation_retry fields: {sorted(unknown)}")
        enabled = raw.get("enabled", True)
        limit = raw.get("max_rewrites", 1)
        if not isinstance(enabled, bool):
            raise ValueError("image.moderation_retry.enabled must be a boolean")
        if type(limit) is not int or limit != 1:
            raise ValueError("image.moderation_retry.max_rewrites must be 1")
        model = str(raw.get("model", default_model)).strip()
        validator = str(raw.get("validator_model", model)).strip()
        return cls(enabled, model, validator, limit)


@dataclass(frozen=True)
class ScenarioSourceConfig:
    """Excel 到场景阶段的全部公开配置。路径均相对项目根目录解析。"""

    mode: str
    input_xlsx: Path
    sheet_name: str
    chunk_by: str
    chunk_size: int
    min_elements: int
    concurrency: int
    fallback_on_llm_error: bool
    debug_save_llm_io: bool
    translation_model: str
    taxonomy_model: str
    stage1_model: str
    element_model: str


@dataclass(frozen=True)
class LocalImageConfig:
    model_path: str
    model: str = "Qwen/Qwen-Image"
    model_revision: str = ""
    size: str = "512*512"
    num_inference_steps: int = 50
    true_cfg_scale: float = 4.0
    negative_prompt: str = " "
    seed: int = 42
    dtype: str = "bfloat16"
    device: str = "cuda:0"
    cpu_offload: str = "sequential"
    device_map: str | None = None

    def cuda_indices(self, visible_device_count: int) -> tuple[int, ...]:
        if self.device_map == "balanced":
            return tuple(range(visible_device_count))
        return (int(self.device.partition(":")[2] or 0),)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], root: Path) -> "LocalImageConfig":
        model_path = str(raw.get("model_path") or "").strip()
        if not model_path:
            raise ValueError("local_image.model_path is required when image_backend=local")
        size = str(raw.get("size", "512*512")).lower().replace("x", "*")
        parts = size.split("*")
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            raise ValueError("local_image.size must look like 512*512")
        width, height = map(int, parts)
        if min(width, height) <= 0 or width % 16 or height % 16:
            raise ValueError("local_image.size dimensions must be positive multiples of 16")
        steps = int(raw.get("num_inference_steps", 50))
        if steps < 1:
            raise ValueError("local_image.num_inference_steps must be positive")
        cfg_scale = float(raw.get("true_cfg_scale", 4.0))
        if not math.isfinite(cfg_scale) or cfg_scale < 1:
            raise ValueError("local_image.true_cfg_scale must be finite and >= 1")
        dtype = str(raw.get("dtype", "bfloat16"))
        if dtype not in {"bfloat16", "float16", "float32"}:
            raise ValueError("local_image.dtype must be bfloat16, float16, or float32")
        device = str(raw.get("device", "cuda:0"))
        if not re.fullmatch(r"cuda(?::\d+)?", device):
            raise ValueError("local_image.device must be cuda or cuda:N")
        offload = str(raw.get("cpu_offload", "sequential"))
        if offload not in {"none", "model", "sequential"}:
            raise ValueError("local_image.cpu_offload must be none, model, or sequential")
        device_map = raw.get("device_map")
        if device_map not in (None, "balanced"):
            raise ValueError("local_image.device_map must be null or balanced")
        if device_map == "balanced" and offload != "none":
            raise ValueError("use cpu_offload: none with device_map: balanced; CPU offload requires device_map: null")
        if int(raw.get("concurrency", 1)) != 1:
            raise ValueError("local_image.concurrency must be 1 (one shared model instance)")
        seed = int(raw.get("seed", 42))
        if not 0 <= seed < 2**63:
            raise ValueError("local_image.seed must be between 0 and 2**63 - 1")
        negative_prompt = raw.get("negative_prompt", " ")
        if not isinstance(negative_prompt, str):
            raise ValueError("local_image.negative_prompt must be a string")
        return cls(
            model_path=str(_resolve(root, Path(model_path).expanduser())),
            model=str(raw.get("model", "Qwen/Qwen-Image")),
            model_revision=str(raw.get("model_revision", "")),
            size=f"{width}*{height}",
            num_inference_steps=steps,
            true_cfg_scale=cfg_scale,
            negative_prompt=negative_prompt,
            seed=seed,
            dtype=dtype,
            device=device,
            cpu_offload=offload,
            device_map=device_map,
        )


@dataclass(frozen=True)
class PipelineConfig:
    config_path: Path
    root: Path
    run_id: str
    output_root: Path
    log_root: Path
    env_file: Path | None
    input_dir: Path
    input_manifest: Path | None
    input_selection: Path | None
    scenario_source: ScenarioSourceConfig
    profiles: tuple[str, ...]
    styles: tuple[str, ...]
    planner_model: str
    author_model: str
    target_model: str
    max_items_per_profile: int
    variants_per_scenario: int
    generation_concurrency: int
    item_max_attempts: int
    shard_max_attempts: int
    random_seed: int
    share_image_across_styles: bool
    direct_severe_harm: bool
    scene_mix_ratios: dict[str, float]
    visual_evidence_ratios: dict[str, float]
    visible_text_word_limits: dict[str, int]
    strict_validation: bool
    image: dict[str, Any]
    response: dict[str, Any]
    models: dict[str, ModelConfig]
    image_backend: str = "api"
    local_image: dict[str, Any] = field(default_factory=dict)
    augmentation: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PipelineConfig":
        resolved = path.resolve()
        raw = _mapping(yaml.safe_load(resolved.read_text(encoding="utf-8")), "config")
        config_root = resolved.parent
        run = _mapping(raw.get("run", {}), "run")
        generation = _mapping(raw.get("generation", {}), "generation")
        source = _mapping(raw.get("scenario_source", {}), "scenario_source")
        model_rows = _mapping(raw.get("models", {}), "models")
        models = {
            name: ModelConfig.from_mapping(name, _mapping(value, f"models.{name}"))
            for name, value in model_rows.items()
        }
        profiles = tuple(str(x).lower() for x in generation.get("profiles", ["hh", "bh"]))
        styles = tuple(str(x).lower() for x in generation.get("styles", ["awareness", "instruction"]))
        unknown_profiles = set(profiles) - {"hh", "bh"}
        unknown_styles = set(styles) - {"awareness", "instruction"}
        if not profiles or unknown_profiles:
            raise ValueError(f"invalid generation profiles: {sorted(unknown_profiles)}")
        if len(profiles) != len(set(profiles)):
            raise ValueError("generation.profiles must not contain duplicates")
        valid_style_sets = {
            ("awareness", "instruction"),
            ("awareness",),
            ("instruction",),
        }
        if unknown_styles or styles not in valid_style_sets:
            raise ValueError(f"invalid generation styles: {sorted(unknown_styles)}")
        share_images = bool(generation.get("share_image_across_styles", len(styles) == 2))
        if len(styles) == 2 and not share_images:
            raise ValueError("generation.share_image_across_styles must be true in both mode")
        if len(styles) == 1 and share_images:
            raise ValueError("generation.share_image_across_styles must be false in a single-style mode")
        if not bool(generation.get("direct_severe_harm", True)):
            raise ValueError("generation.direct_severe_harm must be true for the canonical HH/BH contract")
        if not bool(generation.get("strict_validation", True)):
            raise ValueError("generation.strict_validation must be true")
        max_items = max(0, int(generation.get("max_items_per_profile", 0)))
        variants_per_scenario = int(generation.get("variants_per_scenario", 0))
        if variants_per_scenario < 0:
            raise ValueError("generation.variants_per_scenario must be zero or positive")
        if len(styles) == 2 and max_items % 2:
            raise ValueError("generation.max_items_per_profile must be zero or an even number")
        planner = str(generation.get("planner_model", "planner"))
        author = str(generation.get("author_model", "author"))
        response = _mapping(raw.get("response", {}), "response")
        augmentation = _mapping(raw.get("augmentation", {}), "augmentation")
        if set(augmentation) - {"enabled", "method"}:
            raise ValueError("augmentation accepts only enabled and method (a list of method names)")
        if not isinstance(augmentation.get("enabled", False), bool):
            raise ValueError("augmentation.enabled must be true or false")
        methods = augmentation.get("method", [])
        if not isinstance(methods, list) or any(not isinstance(name, str) for name in methods):
            raise ValueError("augmentation.method must be a list of method names")
        if len(methods) != len(set(methods)):
            raise ValueError("augmentation.method must not contain duplicates")
        from .augmentation.registry import validate_method_names

        validate_method_names(methods)
        if not isinstance(response.get("enabled", True), bool):
            raise ValueError("response.enabled must be true or false")
        target = str(response.get("target_model", "target"))
        required_models = [("planner", planner), ("author", author)]
        if response.get("enabled", True):
            required_models.append(("target", target))
        for role, name in required_models:
            if name not in models:
                raise ValueError(f"{role} model alias is not defined: {name}")
        root = _resolve(config_root, run.get("root", ".."))
        image_backend = str(raw.get("image_backend", "api")).strip().lower()
        if image_backend not in {"api", "local"}:
            raise ValueError("image_backend must be api or local")
        if image_backend == "api":
            retry = ImageModerationRetryConfig.from_mapping(
                _mapping(raw.get("image", {}), "image").get("moderation_retry", {}), author
            )
            if retry.enabled:
                for name in (retry.model, retry.validator_model):
                    if name not in models:
                        raise ValueError(f"image moderation retry model alias is not defined: {name}")
        local_image = _mapping(raw.get("local_image", {}), "local_image")
        if image_backend == "local":
            LocalImageConfig.from_mapping(local_image, root)
        run_id = str(run.get("id", "value_eval_run")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
            raise ValueError("run.id must contain only letters, numbers, dot, underscore, and hyphen")
        source_mode = str(source.get("mode", "prepared")).strip().lower()
        if source_mode not in {"excel", "prepared"}:
            raise ValueError("scenario_source.mode must be excel or prepared")
        chunk_by = str(source.get("chunk_by", "level1"))
        if chunk_by not in {"level1", "fixed_rows"}:
            raise ValueError("scenario_source.chunk_by must be level1 or fixed_rows")
        source_aliases = {
            "translation_model": str(source.get("translation_model", "translator")),
            "taxonomy_model": str(source.get("taxonomy_model", "taxonomy")),
            "stage1_model": str(source.get("stage1_model", "scenario_classifier")),
            "element_model": str(source.get("element_model", "element_author")),
        }
        if source_mode == "excel":
            for role, name in source_aliases.items():
                if name not in models:
                    raise ValueError(f"scenario source {role} alias is not defined: {name}")
        manifest_value = generation.get("input_manifest")
        selection_value = generation.get("input_selection")
        env_value = run.get("env_file", ".env")
        return cls(
            config_path=resolved,
            root=root,
            run_id=run_id,
            output_root=_resolve(root, run.get("output_root", "outputs")),
            log_root=_resolve(root, run.get("log_root", "logs")),
            env_file=_resolve(root, env_value) if env_value else None,
            input_dir=_resolve(root, generation.get("input_dir", "inputs/scenarios/examples/scenario_elements")),
            input_manifest=_resolve(root, manifest_value) if manifest_value else None,
            input_selection=_resolve(root, selection_value) if selection_value else None,
            scenario_source=ScenarioSourceConfig(
                mode=source_mode,
                input_xlsx=_resolve(root, source.get("xlsx", "inputs/excel/value.xlsx")),
                sheet_name=str(source.get("sheet_name", "价值观目录及评判标准")),
                chunk_by=chunk_by,
                chunk_size=max(1, int(source.get("chunk_size", 12))),
                min_elements=max(1, int(source.get("min_elements", 8))),
                concurrency=max(1, int(source.get("concurrency", 4))),
                fallback_on_llm_error=bool(source.get("fallback_on_llm_error", True)),
                debug_save_llm_io=bool(source.get("debug_save_llm_io", True)),
                translation_model=source_aliases["translation_model"],
                taxonomy_model=source_aliases["taxonomy_model"],
                stage1_model=source_aliases["stage1_model"],
                element_model=source_aliases["element_model"],
            ),
            profiles=profiles,
            styles=styles,
            planner_model=planner,
            author_model=author,
            target_model=target,
            max_items_per_profile=max_items,
            variants_per_scenario=variants_per_scenario,
            generation_concurrency=max(1, int(generation.get("concurrency", 1))),
            item_max_attempts=max(1, int(generation.get("item_max_attempts", 3))),
            shard_max_attempts=max(1, int(generation.get("shard_max_attempts", 5))),
            random_seed=int(generation.get("random_seed", 20260915)),
            share_image_across_styles=share_images,
            direct_severe_harm=bool(generation.get("direct_severe_harm", True)),
            scene_mix_ratios=_ratio_mapping(
                generation.get("scene_mix"), DEFAULT_SCENE_MIX_RATIOS, "generation.scene_mix"
            ),
            visual_evidence_ratios=_ratio_mapping(
                generation.get("visual_evidence"),
                DEFAULT_VISUAL_EVIDENCE_RATIOS,
                "generation.visual_evidence",
            ),
            visible_text_word_limits=_integer_mapping(
                generation.get("max_visible_text_words"),
                DEFAULT_VISIBLE_TEXT_WORD_LIMITS,
                "generation.max_visible_text_words",
            ),
            strict_validation=bool(generation.get("strict_validation", True)),
            image=_mapping(raw.get("image", {}), "image"),
            response=response,
            models=models,
            image_backend=image_backend,
            local_image=local_image,
            augmentation=augmentation,
        )

    @property
    def augmentation_methods(self) -> tuple[str, ...]:
        if not self.augmentation.get("enabled", False):
            return ()
        return tuple(self.augmentation.get("method", []))

    @property
    def run_root(self) -> Path:
        return self.output_root / self.execution_run_id

    @property
    def execution_run_id(self) -> str:
        if self.style_mode == "both":
            return self.run_id
        return f"{self.run_id}--{self.style_mode}"

    @property
    def style_mode(self) -> str:
        return "both" if len(self.styles) == 2 else self.styles[0]

    @property
    def generate_images_during_benchmark(self) -> bool:
        return bool(self.active_image.get("generate_during_benchmark", False))

    @property
    def responses_enabled(self) -> bool:
        return bool(self.response.get("enabled", True))

    @property
    def active_image(self) -> dict[str, Any]:
        if self.image_backend == "local":
            return self.local_image
        if self.image_backend == "api":
            return self.image
        raise ValueError("image_backend must be api or local")

    @property
    def prepared_scenario_dir(self) -> Path:
        return self.run_root / "scenario_preparation" / "scenario_elements"

    @property
    def prepared_scenario_manifest(self) -> Path:
        return self.run_root / "scenario_preparation" / "manifest.json"
