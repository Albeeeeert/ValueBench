from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..io_utils import sha256_bytes


def fingerprint(value: Any) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def resolve_artifact(root: Path, relative: str) -> Path:
    path = Path(relative)
    if not relative or path.is_absolute():
        raise ValueError(f"artifact path must be relative: {relative!r}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"artifact path escapes its root: {relative!r}")
    return resolved


@dataclass(frozen=True)
class AugmentationSource:
    benchmark: dict[str, Any]
    benchmark_path: str
    image_path: Path | None = None
    image_sha256: str = ""

    @property
    def source_id(self) -> str:
        return str(self.benchmark["benchmark_id"])

    def provenance(self) -> dict[str, Any]:
        return {
            "profile": "hh", "style": "instruction",
            "benchmark_id": self.source_id, "benchmark_path": self.benchmark_path,
            "benchmark": self.benchmark,
        }


@dataclass(frozen=True)
class AugmentationResult:
    prompt: str
    image_paths: tuple[Path, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


class AugmentationMethod(ABC):
    name: str
    version: str
    concurrency: int
    snapshot: dict[str, Any]
    fingerprint: str
    requires_original_image: bool = False
    supported_response_modes: tuple[str, ...] = ("image_text",)

    def bind(self, config):
        """Attach run settings without loading models or making requests."""
        return self

    def close(self) -> None:
        """Release method-specific model resources after generation."""

    @abstractmethod
    def generate(self, source: AugmentationSource, sample_id: str, output_dir: Path) -> list[AugmentationResult]:
        """Write images atomically and return one or more variants for one source."""
