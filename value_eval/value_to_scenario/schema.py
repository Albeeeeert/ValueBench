from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal


ScenarioType = Literal["enumerated", "abstract", "mixed"]


@dataclass(frozen=True)
class ScenarioRecord:
    scenario_id: str
    source_path: str
    category_path_en: List[str]
    title: str
    translation_en: str
    judging_criteria_en: List[str]
    safe_content_boundary: str
    benchmark_generation_notes: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioElement:
    element_id: str
    label: str
    target_axis: str
    risk_pattern: str
    bias_surfaces: List[str]
    constraints: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class ScenarioElementPlan:
    schema_version: str
    scenario_id: str
    source_path: str
    category_path_en: List[str]
    title: str
    source_text: str
    scenario_type: ScenarioType
    abstraction_policy: Dict[str, Any]
    elements: List[ScenarioElement]
    stage_outputs: Dict[str, Any] = field(default_factory=dict)
    quality_warnings: List[str] = field(default_factory=list)

