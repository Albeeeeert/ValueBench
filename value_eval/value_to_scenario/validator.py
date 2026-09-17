from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .utils import atomic_write_json, atomic_write_text
from .schema import ScenarioRecord


def validate_and_manifest(
    *,
    output_dir: Path,
    expected_records: Sequence[ScenarioRecord],
    fingerprint: str,
    anomalies: list[dict[str, Any]],
    element_status: dict[str, Any],
    min_elements: int,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    expected = {record.scenario_id for record in expected_records}
    files = sorted(output_dir.glob("*.json"))
    entries: list[dict[str, Any]] = []
    ids: list[str] = []
    low_element_count = 0
    manual_review_count = 0

    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid JSON {path.name}: {exc}")
            continue
        scenario_id = str(raw.get("scenario_id", "")).strip()
        ids.append(scenario_id)
        if path.stem != scenario_id:
            errors.append(f"filename/id mismatch: {path.name} != {scenario_id}")
        category = raw.get("category_path_en", [])
        if not isinstance(category, list) or not any(str(item).strip() for item in category):
            errors.append(f"{scenario_id}: category_path_en must be a non-empty array")
            category = []
        for field in ("title", "source_text"):
            if not str(raw.get(field, "")).strip():
                errors.append(f"{scenario_id}: {field} cannot be empty")
        elements = raw.get("elements")
        if not isinstance(elements, list) or not elements:
            errors.append(f"{scenario_id}: no usable elements")
            elements = []
        if len(elements) < min_elements:
            low_element_count += 1
        element_ids: list[str] = []
        for index, element in enumerate(elements, start=1):
            if not isinstance(element, dict):
                errors.append(f"{scenario_id}: element {index} must be an object")
                continue
            element_id = str(element.get("element_id", "")).strip()
            element_ids.append(element_id)
            for field in ("element_id", "label", "target_axis", "risk_pattern"):
                if not str(element.get(field, "")).strip():
                    errors.append(f"{scenario_id}: element {index} has empty {field}")
            if not isinstance(element.get("bias_surfaces", []), list):
                errors.append(f"{scenario_id}: element {index} bias_surfaces must be an array")
            if not isinstance(element.get("constraints", {}), dict):
                errors.append(f"{scenario_id}: element {index} constraints must be an object")
        if len(element_ids) != len(set(element_ids)):
            errors.append(f"{scenario_id}: duplicate element_id values")
        generation = raw.get("generation", {})
        if not isinstance(generation, dict):
            generation = {}
        if generation.get("requires_manual_review"):
            manual_review_count += 1
        entries.append(
            {
                "scenario_id": scenario_id,
                "canonical_relative_path": path.name,
                "category_path_en": category if isinstance(category, list) else [],
                "element_count": len(elements),
                "generation_method": generation.get("method", "unknown"),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )

    duplicate_ids = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicate_ids:
        errors.append(f"duplicate scenario_id values: {duplicate_ids}")
    actual = set(ids)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append(f"missing scenario files: {missing}")
    if extra:
        errors.append(f"unexpected scenario files: {extra}")
    if len(files) != len(expected):
        errors.append(f"file count {len(files)} != expected scenario count {len(expected)}")
    if low_element_count:
        warnings.append(
            f"{low_element_count} plans contain fewer than {min_elements} elements"
        )
    if manual_review_count:
        warnings.append(f"{manual_review_count} plans require manual review")

    return {
        "schema_version": "value-to-scenario-manifest-v1",
        "fingerprint": fingerprint,
        "valid": not errors,
        "expected_scenarios": len(expected),
        "output_files": len(files),
        "unique_scenario_ids": len(actual),
        "duplicate_scenario_ids": duplicate_ids,
        "low_element_count": low_element_count,
        "manual_review_count": manual_review_count,
        "minimum_elements_target": min_elements,
        "source_numbering_anomalies": anomalies,
        "element_status_counts": element_status.get("counts", {}),
        "errors": errors,
        "warnings": warnings,
        "entries": entries,
    }


def write_report(report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    atomic_write_json(json_path, report)
    lines = [
        "# Value to Scenario Build Report",
        "",
        f"- Valid: `{report.get('valid')}`",
        f"- Fingerprint: `{report.get('fingerprint', '')}`",
        f"- Expected scenarios: {report.get('expected_scenarios', 0)}",
        f"- Output files: {report.get('output_files', 0)}",
        f"- Unique scenario IDs: {report.get('unique_scenario_ids', 0)}",
        f"- Plans requiring manual review: {report.get('manual_review_count', 0)}",
        f"- Plans below the element target: {report.get('low_element_count', 0)}",
        "",
        "## Errors",
        "",
    ]
    errors = report.get("errors", [])
    lines.extend(f"- {item}" for item in errors or ["None"])
    lines.extend(["", "## Warnings", ""])
    warnings = report.get("warnings", [])
    lines.extend(f"- {item}" for item in warnings or ["None"])
    lines.extend(["", "## Source Numbering Anomalies", ""])
    anomaly_items = report.get("source_numbering_anomalies", [])
    if anomaly_items:
        for item in anomaly_items:
            lines.append(f"- `{item.get('type')}` row {item.get('row_id')}: `{item}`")
    else:
        lines.append("- None")
    atomic_write_text(markdown_path, "\n".join(lines) + "\n")
