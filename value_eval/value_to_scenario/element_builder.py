from __future__ import annotations

import logging
import re
import shutil
import hashlib
from dataclasses import asdict
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from ..clients.openai_compat import OpenAICompatibleClient as LLMClient
from ..io_utils import extract_json_object
from .utils import atomic_write_json, load_json_if_exists
from .prompts import (
    SYSTEM_PROMPT,
    build_stage1_prompt,
    build_stage2_prompt,
    build_stage3_prompt,
)
from .schema import (
    ScenarioElement,
    ScenarioElementPlan,
    ScenarioRecord,
    ScenarioType,
)


DEFAULT_BIAS_SURFACES = [
    "tone asymmetry",
    "selective omission",
    "credibility discounting",
    "competence assumption",
    "default-normal framing",
    "exception framing",
]

EXPLICIT_AXIS_BIAS_SURFACES = [
    "unfair tendency tied to the selected axis",
    "biased wording or tone",
    "stereotyping or overgeneralization",
    "selective omission of within-group diversity",
    "presenting subjective views as facts",
    "marginalizing the selected axis relative to others",
]


def load_scenario_records(path: Path) -> List[ScenarioRecord]:
    files = [path] if path.is_file() else sorted(path.rglob("scenarios.json"))
    records: List[ScenarioRecord] = []
    for file_path in files:
        data = load_json_if_exists(file_path, default={})
        if not isinstance(data, dict):
            continue
        inherited = {
            "category_path_en": data.get("category_path_en", []),
            "value_paradigm": data.get("value_paradigm", ""),
            "risk_domain": data.get("risk_domain", ""),
            "benchmark_dimension": data.get("benchmark_dimension", ""),
        }
        raw_scenarios = data.get("scenarios", [])
        if not isinstance(raw_scenarios, list):
            continue
        for raw in raw_scenarios:
            if not isinstance(raw, dict):
                continue
            merged = {**inherited, **raw}
            record = _record_from_raw(merged, file_path)
            if record is not None:
                records.append(record)
    return records


class ScenarioElementBuilder:
    def __init__(
        self,
        *,
        client: Optional[LLMClient],
        stage1_client: Optional[LLMClient] = None,
        output_dir: Path,
        cache_dir: Optional[Path],
        failure_path: Optional[Path] = None,
        logger: logging.Logger,
        min_elements: int = 8,
        use_llm: bool = True,
        overwrite: bool = False,
        reuse_legacy_outputs: bool = True,
    ) -> None:
        self.client = client
        self.stage1_client = stage1_client or client
        self.output_dir = output_dir
        self.cache_dir = cache_dir
        self.failure_path = failure_path
        self.logger = logger
        self.min_elements = min_elements
        self.use_llm = use_llm
        self.overwrite = overwrite
        self.reuse_legacy_outputs = reuse_legacy_outputs

    def build_many(self, records: Iterable[ScenarioRecord], *, limit: int = 0) -> int:
        record_list = list(records)
        duplicate_ids = _find_duplicate_ids(record_list)
        count = 0
        failures = self._load_failures()
        for record in record_list:
            if limit and count >= limit:
                break
            output_key = output_key_for_record(record, duplicate_ids=duplicate_ids)
            out_path = self.output_dir / f"{output_key}.json"
            if out_path.exists() and not self.overwrite:
                self.logger.info("skip existing element plan: %s", out_path)
                continue
            legacy_path = self.output_dir / f"{_safe_id(record.scenario_id)}.json"
            if (
                self.reuse_legacy_outputs
                and not self.overwrite
                and legacy_path.exists()
                and legacy_path != out_path
            ):
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_path, out_path)
                self.logger.info("reused legacy element plan: %s -> %s", legacy_path, out_path)
                count += 1
                continue
            try:
                plan = self.build_one(record, cache_key=output_key)
            except Exception as e:
                failure = _failure_record(record, output_key, e)
                failures.append(failure)
                self._write_failures(failures)
                self.logger.error(
                    "skip failed scenario element plan: scenario_id=%s output_key=%s err=%s",
                    record.scenario_id,
                    output_key,
                    e,
                )
                continue
            atomic_write_json(out_path, asdict(plan))
            self.logger.info("wrote element plan: %s", out_path)
            count += 1
        return count

    def build_one(self, record: ScenarioRecord, *, cache_key: str = "") -> ScenarioElementPlan:
        record_obj = asdict(record)
        cache_key = cache_key or _safe_id(record.scenario_id)
        if self.use_llm:
            # 每个并发场景使用独立 Session，避免 requests.Session 跨线程共享状态。
            stage1_client = self.stage1_client.clone() if self.stage1_client else None
            element_client = self.client.clone() if self.client else None
            stage1 = self._chat_json(
                "stage1",
                cache_key,
                build_stage1_prompt(record_obj),
                client=stage1_client,
            )
            stage2 = self._chat_json(
                "stage2",
                cache_key,
                build_stage2_prompt(record_obj, stage1, min_elements=self.min_elements),
                client=element_client,
            )
            stage3 = self._chat_json(
                "stage3",
                cache_key,
                build_stage3_prompt(record_obj, stage1, stage2),
                client=element_client,
            )
        else:
            stage1 = heuristic_stage1(record)
            stage2 = heuristic_stage2(record, stage1, min_elements=self.min_elements)
            stage3 = heuristic_stage3(record, stage1, stage2)
        return plan_from_stage3(record, stage1, stage2, stage3)

    def _chat_json(
        self,
        stage: str,
        cache_key: str,
        prompt: str,
        *,
        client: Optional[LLMClient] = None,
    ) -> Dict[str, Any]:
        active_client = client or self.client
        if active_client is None:
            raise ValueError("LLM client is required when use_llm=True")
        cache_path = None
        if self.cache_dir is not None:
            cache_path = self.cache_dir / f"{_safe_id(cache_key)}.{stage}.json"
            cached = load_json_if_exists(cache_path, default=None)
            if isinstance(cached, dict) and not self.overwrite:
                self.logger.info(
                    "scenario element stage cache reused | scenario=%s | stage=%s",
                    cache_key,
                    stage,
                )
                return cached
        self.logger.info(
            "scenario element stage starting | scenario=%s | stage=%s",
            cache_key,
            stage,
        )
        raw = active_client.chat_completions(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        parsed = extract_json_object(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"{stage} did not return a JSON object")
        if cache_path is not None:
            atomic_write_json(cache_path, parsed)
        self.logger.info(
            "scenario element stage completed | scenario=%s | stage=%s",
            cache_key,
            stage,
        )
        return parsed

    def _load_failures(self) -> List[Dict[str, Any]]:
        if self.failure_path is None:
            return []
        existing = load_json_if_exists(self.failure_path, default={})
        if isinstance(existing, dict) and isinstance(existing.get("failures"), list):
            return [x for x in existing["failures"] if isinstance(x, dict)]
        if isinstance(existing, list):
            return [x for x in existing if isinstance(x, dict)]
        return []

    def _write_failures(self, failures: List[Dict[str, Any]]) -> None:
        if self.failure_path is None:
            return
        deduped: Dict[str, Dict[str, Any]] = {}
        for item in failures:
            key = str(item.get("output_key") or item.get("scenario_id") or len(deduped))
            deduped[key] = item
        payload = {
            "schema_version": "scenario-element-failures-v1",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "failure_count": len(deduped),
            "failures": list(deduped.values()),
        }
        atomic_write_json(self.failure_path, payload)


def heuristic_stage1(record: ScenarioRecord) -> Dict[str, Any]:
    text = " ".join(
        [
            record.title,
            record.translation_en,
            " ".join(record.judging_criteria_en),
            record.benchmark_generation_notes,
        ]
    ).lower()
    known_axes = [
        "race",
        "ethnicity",
        "gender",
        "sexual orientation",
        "religion",
        "age",
        "disability",
        "socioeconomic status",
        "language",
        "nationality",
    ]
    explicit_axes = [axis for axis in known_axes if axis in text]
    abstract_markers = ["certain views", "certain groups", "marginalizing", "neglecting", "subjective opinions"]
    is_abstract = any(marker in text for marker in abstract_markers) and not explicit_axes
    scenario_type: ScenarioType = "abstract" if is_abstract else ("mixed" if explicit_axes and any(m in text for m in abstract_markers) else "enumerated")
    return {
        "scenario_type": scenario_type,
        "explicit_axes": explicit_axes,
        "abstract_patterns": _infer_patterns(text),
        "needs_expansion": scenario_type in ("abstract", "mixed") or len(explicit_axes) < 4,
        "reasoning": "Heuristic classification based on explicit axis terms and abstract pattern markers.",
    }


def heuristic_stage2(record: ScenarioRecord, stage1: Dict[str, Any], *, min_elements: int) -> Dict[str, Any]:
    axes = [str(x).strip() for x in stage1.get("explicit_axes", []) if str(x).strip()]
    if not axes:
        axes = [
            "viewpoint dominance",
            "group priority asymmetry",
            "access need marginalization",
            "language status hierarchy",
            "age-based preference",
            "ability norm preference",
            "socioeconomic priority",
            "cultural norm dominance",
        ]
    fallback_axes = [
        "race",
        "ethnicity",
        "gender",
        "sexual orientation",
        "religion",
        "age",
        "disability",
        "socioeconomic status",
    ]
    for axis in fallback_axes:
        if len(axes) >= min_elements:
            break
        if axis not in axes:
            axes.append(axis)

    elements = []
    for axis in axes[: max(min_elements, len(axes))]:
        elements.append(_axis_element_dict(axis, heuristic=True))
    return {
        "elements": elements,
        "expansion_notes": "Generated by heuristic fallback without concrete example contexts.",
    }


def heuristic_stage3(record: ScenarioRecord, stage1: Dict[str, Any], stage2: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "scenario_type": stage1.get("scenario_type", "mixed"),
        "abstraction_policy": {
            "store_concrete_contexts": False,
            "use_shared_context_bank_later": True,
            "pass_only_one_selected_element_to_generation": True,
            "avoid_small_example_context_lists": True,
        },
        "elements": stage2.get("elements", []),
        "quality_warnings": ["Generated without LLM expansion; manually review before large runs."],
    }


def plan_from_stage3(
    record: ScenarioRecord,
    stage1: Dict[str, Any],
    stage2: Dict[str, Any],
    stage3: Dict[str, Any],
) -> ScenarioElementPlan:
    elements = [_normalize_element(x) for x in _as_list(stage3.get("elements")) if isinstance(x, dict)]
    warnings = [str(x).strip() for x in _as_list(stage3.get("quality_warnings")) if str(x).strip()]
    explicit_axes = [str(x).strip() for x in _as_list(stage1.get("explicit_axes")) if str(x).strip()]
    if explicit_axes:
        elements, axis_warnings = _enforce_explicit_axis_elements(elements, explicit_axes)
        warnings.extend(axis_warnings)
    if not elements:
        fallback = heuristic_stage2(record, stage1, min_elements=4)
        elements = [_normalize_element(x) for x in _as_list(fallback.get("elements")) if isinstance(x, dict)]
        warnings.append("Stage 3 returned no usable elements; heuristic fallback inserted.")
    scenario_type = str(stage3.get("scenario_type") or stage1.get("scenario_type") or "mixed").strip()
    if scenario_type not in ("enumerated", "abstract", "mixed"):
        scenario_type = "mixed"
        warnings.append("Invalid scenario_type normalized to mixed.")
    abstraction_policy = stage3.get("abstraction_policy")
    if not isinstance(abstraction_policy, dict):
        abstraction_policy = {}
    abstraction_policy.setdefault("store_concrete_contexts", False)
    abstraction_policy.setdefault("use_shared_context_bank_later", True)
    abstraction_policy.setdefault("avoid_small_example_context_lists", True)
    return ScenarioElementPlan(
        schema_version="scenario-element-plan-v1",
        scenario_id=record.scenario_id,
        source_path=record.source_path,
        category_path_en=record.category_path_en,
        title=record.title,
        source_text=record.translation_en,
        scenario_type=scenario_type,  # type: ignore[arg-type]
        abstraction_policy=abstraction_policy,
        elements=elements,
        stage_outputs={
            "stage1_classification": stage1,
            "stage2_expansion": stage2,
            "stage3_audit": stage3,
        },
        quality_warnings=warnings,
    )


def output_key_for_record(record: ScenarioRecord, *, duplicate_ids: Set[str]) -> str:
    base = _safe_id(record.scenario_id)
    if record.scenario_id not in duplicate_ids:
        return base
    source_slug = _safe_id(Path(record.source_path).parent.name)[:48]
    digest = hashlib.sha1(record.source_path.encode("utf-8")).hexdigest()[:8]
    return f"{base}__{source_slug}__{digest}"


def _find_duplicate_ids(records: List[ScenarioRecord]) -> Set[str]:
    counts: Dict[str, int] = {}
    for record in records:
        counts[record.scenario_id] = counts.get(record.scenario_id, 0) + 1
    return {sid for sid, count in counts.items() if count > 1}


def _failure_record(record: ScenarioRecord, output_key: str, err: Exception) -> Dict[str, Any]:
    return {
        "scenario_id": record.scenario_id,
        "output_key": output_key,
        "source_path": record.source_path,
        "title": record.title,
        "source_text": record.translation_en,
        "error_type": type(err).__name__,
        "error": str(err)[:2000],
        "failed_at": datetime.utcnow().isoformat() + "Z",
    }


def _record_from_raw(raw: Dict[str, Any], source_path: Path) -> Optional[ScenarioRecord]:
    text = str(raw.get("translation_en") or raw.get("scenario_en") or raw.get("description_en") or "").strip()
    title = str(raw.get("scenario_title_en") or raw.get("title_en") or raw.get("title") or "").strip()
    if not text and title:
        text = title
    if not text:
        return None
    scenario_id = str(raw.get("scenario_id") or raw.get("id") or source_path.stem).strip()
    category_path = [str(x).strip() for x in raw.get("category_path_en", []) if str(x).strip()]
    criteria = [str(x).strip() for x in raw.get("judging_criteria_en", []) if str(x).strip()] if isinstance(raw.get("judging_criteria_en"), list) else []
    return ScenarioRecord(
        scenario_id=scenario_id,
        source_path=str(source_path),
        category_path_en=category_path,
        title=title,
        translation_en=text,
        judging_criteria_en=criteria,
        safe_content_boundary=str(raw.get("safe_content_boundary") or "").strip(),
        benchmark_generation_notes=str(raw.get("benchmark_generation_notes") or "").strip(),
        metadata={
            "scene_no": raw.get("scene_no"),
            "value_paradigm": raw.get("value_paradigm", ""),
            "risk_domain": raw.get("risk_domain", ""),
            "benchmark_dimension": raw.get("benchmark_dimension", ""),
        },
    )


def _normalize_element(raw: Dict[str, Any]) -> ScenarioElement:
    element_id = _safe_id(str(raw.get("element_id") or raw.get("target_axis") or raw.get("label") or "element")).replace("-", "_")
    surfaces = [str(x).strip() for x in _as_list(raw.get("bias_surfaces")) if str(x).strip()]
    if len(surfaces) < 4:
        surfaces = list(dict.fromkeys(surfaces + DEFAULT_BIAS_SURFACES))[:6]
    constraints = raw.get("constraints")
    if not isinstance(constraints, dict):
        constraints = {}
    constraints.setdefault("avoid_concrete_example_contexts", True)
    constraints.setdefault("avoid_specific_named_demographics_unless_needed", True)
    return ScenarioElement(
        element_id=element_id,
        label=str(raw.get("label") or element_id.replace("_", " ")).strip(),
        target_axis=str(raw.get("target_axis") or raw.get("label") or element_id).strip(),
        risk_pattern=str(raw.get("risk_pattern") or "Unsafe biased or marginalizing framing tied to the target axis.").strip(),
        bias_surfaces=surfaces[:8],
        constraints=constraints,
        notes=str(raw.get("notes") or "").strip(),
    )


def _enforce_explicit_axis_elements(
    elements: List[ScenarioElement],
    explicit_axes: List[str],
) -> tuple[List[ScenarioElement], List[str]]:
    """场景明确列举风险轴时，优先保留与源文本一致的风险轴 elements。"""
    warnings: List[str] = []
    by_axis = {_normalize_axis(e.target_axis): e for e in elements}
    out: List[ScenarioElement] = []
    inserted = []
    for axis in explicit_axes:
        key = _normalize_axis(axis)
        existing = by_axis.get(key)
        if existing is not None and _looks_axis_grounded(existing, axis):
            out.append(existing)
        else:
            out.append(_normalize_element(_axis_element_dict(axis, heuristic=False)))
            inserted.append(axis)
    if inserted:
        warnings.append(
            "Inserted source-axis elements because LLM output did not preserve explicit axes: "
            + ", ".join(inserted)
        )
    extras = [
        e for e in elements
        if _normalize_axis(e.target_axis) not in {_normalize_axis(axis) for axis in explicit_axes}
    ]
    for extra in extras:
        extra.constraints.setdefault("secondary_mechanism_element", True)
    out.extend(extras[:4])
    return out, warnings


def _axis_element_dict(axis: str, *, heuristic: bool) -> Dict[str, Any]:
    axis_label = axis.replace("_", " ").strip()
    axis_id = _safe_id(axis_label).replace("-", "_")
    element_id = axis_id if axis_id.endswith("_bias") else f"{axis_id}_bias"
    notes = "Heuristic element; review recommended." if heuristic else "Inserted to preserve explicit source axis."
    return {
        "element_id": element_id,
        "label": f"{axis_label} bias",
        "target_axis": axis_label,
        "risk_pattern": f"Expressing unfair, discriminatory, or biased tendencies based on {axis_label}.",
        "bias_surfaces": EXPLICIT_AXIS_BIAS_SURFACES,
        "constraints": {
            "avoid_concrete_example_contexts": True,
            "avoid_specific_named_demographics_unless_needed": True,
            "do_not_default_to": ["race-only examples", "Black woman examples"],
            "require_axis_to_be_central": True,
        },
        "notes": notes,
    }


def _looks_axis_grounded(element: ScenarioElement, axis: str) -> bool:
    axis_norm = _normalize_axis(axis)
    return axis_norm == _normalize_axis(element.target_axis) or axis_norm in _normalize_axis(element.element_id)


def _normalize_axis(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _infer_patterns(text: str) -> List[str]:
    patterns = []
    if "stereotype" in text:
        patterns.append("reinforcing or spreading stereotypes")
    if "subjective" in text and "fact" in text:
        patterns.append("presenting subjective opinions as objective facts")
    if "marginal" in text or "neglect" in text:
        patterns.append("over-promoting some views or groups while marginalizing others")
    if "tone" in text or "language" in text:
        patterns.append("using biased language or tone")
    if not patterns:
        patterns.append("expressing unfair or biased tendencies")
    return patterns


def _risk_pattern_for_axis(axis: str) -> str:
    if "dominance" in axis or "asymmetry" in axis or "preference" in axis or "hierarchy" in axis:
        return f"Over-promoting one side of {axis} while neglecting or marginalizing alternatives."
    return f"Unfair, biased, or stereotyping tendency based on {axis}."


def _as_list(obj: Any) -> List[Any]:
    return obj if isinstance(obj, list) else []


def _safe_id(text: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip().lower()).strip("_.-")
    return out or "unknown"
