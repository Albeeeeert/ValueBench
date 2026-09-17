from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..io_utils import load_json, sha256_file
from ..schemas import ScenarioElement


def _text(value: Any, field: str, path: Path) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise ValueError(f"{path}: {field} cannot be empty")
    return parsed


def validate_manifest(input_dir: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        raise ValueError(f"{manifest_path}: manifest must contain an entries array")
    for entry in manifest["entries"]:
        if not isinstance(entry, dict):
            raise ValueError(f"{manifest_path}: manifest entry must be an object")
        relative = str(entry.get("canonical_relative_path", "")).strip()
        if not relative:
            raise ValueError(f"{manifest_path}: entry lacks canonical_relative_path")
        candidate = (input_dir / relative).resolve()
        if input_dir.resolve() not in candidate.parents:
            raise ValueError(f"{manifest_path}: input path escapes its root: {relative}")
        if not candidate.is_file():
            raise FileNotFoundError(f"manifest input is missing: {candidate}")
        expected = str(entry.get("sha256", "")).strip()
        if expected and sha256_file(candidate) != expected:
            raise ValueError(f"manifest hash mismatch: {candidate}")
    return manifest


def load_scenario_elements(
    input_dir: Path,
    manifest_path: Path | None = None,
    *,
    allowlist: set[str] | None = None,
    selected_pairs: list[tuple[str, str]] | None = None,
) -> list[ScenarioElement]:
    root = input_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"scenario input directory is missing: {root}")
    if manifest_path is not None:
        validate_manifest(root, manifest_path.resolve())
    units: list[ScenarioElement] = []
    selected_set = set(selected_pairs) if selected_pairs is not None else None
    seen: set[tuple[str, str]] = set()
    for path in sorted(root.glob("*.json")):
        raw = load_json(path)
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: scenario file must be an object")
        scenario_id = _text(raw.get("scenario_id"), "scenario_id", path)
        category = raw.get("category_path_en", [])
        if not isinstance(category, list) or not category:
            raise ValueError(f"{path}: category_path_en must be a non-empty array")
        elements = raw.get("elements")
        if not isinstance(elements, list) or not elements:
            raise ValueError(f"{path}: elements must be a non-empty array")
        for element in elements:
            if not isinstance(element, dict):
                raise ValueError(f"{path}: element must be an object")
            element_id = _text(element.get("element_id"), "element_id", path)
            if allowlist and element_id not in allowlist:
                continue
            key = (scenario_id, element_id)
            if selected_set is not None and key not in selected_set:
                continue
            if key in seen:
                raise ValueError(f"duplicate scenario/element key: {scenario_id}/{element_id}")
            seen.add(key)
            units.append(ScenarioElement(
                scenario_id=scenario_id,
                source_path=Path(path.name),
                category_path=[str(value).strip() for value in category if str(value).strip()],
                title=_text(raw.get("title"), "title", path),
                source_text=_text(raw.get("source_text"), "source_text", path),
                element_id=element_id,
                label=_text(element.get("label"), "element.label", path),
                target_axis=_text(element.get("target_axis"), "element.target_axis", path),
                risk_pattern=_text(element.get("risk_pattern"), "element.risk_pattern", path),
                bias_surfaces=[str(value).strip() for value in element.get("bias_surfaces", []) if str(value).strip()],
                constraints=dict(element.get("constraints", {})) if isinstance(element.get("constraints"), dict) else {},
                notes=element.get("notes", ""),
            ))
    if not units:
        raise ValueError(f"no scenario elements loaded from {root}")
    if selected_set is not None and seen != selected_set:
        missing = sorted(selected_set - seen)
        raise ValueError(f"selection references missing scenario elements: {missing[:3]}")
    if selected_pairs is not None:
        by_key = {(unit.scenario_id, unit.element_id): unit for unit in units}
        units = [by_key[key] for key in selected_pairs]
    return units


def load_selection_pairs(path: Path | None) -> list[tuple[str, str]] | None:
    if path is None:
        return None
    value = load_json(path.resolve())
    rows = value.get("selection", []) if isinstance(value, dict) else []
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: selection manifest must contain a non-empty selection array")
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: selection entry must be an object")
        scenario_id = str(row.get("scenario_id", "")).strip()
        element_id = str(row.get("element_id", "")).strip()
        if not scenario_id or not element_id:
            raise ValueError(f"{path}: selection entry lacks scenario_id or element_id")
        pair = (scenario_id, element_id)
        if pair in seen:
            raise ValueError(f"{path}: duplicate selection pair {scenario_id}/{element_id}")
        pairs.append(pair)
        seen.add(pair)
    declared = value.get("mechanism_count_per_profile")
    if declared is not None and int(declared) != len(pairs):
        raise ValueError(f"{path}: declared mechanism count does not match selection")
    return pairs


def expand_scenario_variants(
    units: list[ScenarioElement],
    variants_per_scenario: int,
) -> list[ScenarioElement]:
    """Create a stable number of generation jobs per scenario by cycling its elements."""
    if variants_per_scenario <= 0:
        return units
    by_scenario: dict[str, list[ScenarioElement]] = {}
    for unit in units:
        by_scenario.setdefault(unit.scenario_id, []).append(unit)
    expanded: list[ScenarioElement] = []
    for scenario_units in by_scenario.values():
        for offset in range(variants_per_scenario):
            expanded.append(replace(
                scenario_units[offset % len(scenario_units)],
                variant_index=offset + 1,
                variant_count=variants_per_scenario,
            ))
    return expanded
