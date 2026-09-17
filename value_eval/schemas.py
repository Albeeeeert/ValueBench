from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScenarioElement:
    scenario_id: str
    source_path: Path
    category_path: list[str]
    title: str
    source_text: str
    element_id: str
    label: str
    target_axis: str
    risk_pattern: str
    bias_surfaces: list[str]
    constraints: dict[str, Any]
    notes: Any = ""
    variant_index: int = 1
    variant_count: int = 1

    @property
    def theme(self) -> str:
        return self.category_path[0] if self.category_path else "Value"

    @property
    def subdimension(self) -> str:
        return " > ".join(self.category_path[1:]) or self.title

    def prompt_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_path"] = str(self.source_path)
        return value


@dataclass(frozen=True)
class BenchmarkItem:
    benchmark_id: str
    theme: str
    subdimension: str
    category_path_en: list[str]
    source_scenario_id: str
    source_element_id: str
    scenario_question_style: str
    image_description: str
    question: str
    options: dict[str, str]
    answer: str
    option_roles: dict[str, str]
    rationale: str
    tags: list[str]
    shared_image_id: str = ""
    shared_image_role: str = ""
    template_id: str = ""
    risk_combination_type: str = ""
    modality_risk_labels: dict[str, str] = field(default_factory=dict)
    risk_audit: dict[str, Any] = field(default_factory=dict)
    interaction_mechanism: str = ""
    program_lints: list[str] = field(default_factory=list)
    visual_evidence_mode: str = ""
    estimated_visible_text_words: int = 0
    quality_status: str = "structural_pass"
    generation_trace: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImageTask:
    image_key: str
    prompt: str
    benchmark_ids: list[str]
    profile: str
    shared_image_id: str = ""
    status: str = "pending"
    attempts: int = 0
    image_path: str = ""
    image_sha256: str = ""
    image_model: str = ""
    image_format: str = ""
    width: int = 0
    height: int = 0
    request_ids: list[str] = field(default_factory=list)
    seed: int | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ApiResponse:
    content: str
    reasoning_content: str
    usage: dict[str, Any]
    elapsed_sec: float
    request_id: str = ""
