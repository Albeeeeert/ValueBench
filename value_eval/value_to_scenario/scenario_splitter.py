from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .utils import atomic_write_json
from .schema import ScenarioRecord
from .taxonomy import _title_from_scene


# 源表格中同时存在“5.”、“5 ”和“5正文”三种编号形式。
# 编号标记只允许出现在行首，避免误拆评判标准正文中正常出现的数字。
SECTION_MARKER = re.compile(
    r"(?:^|\n)\s*(\d+)(?:[.．、]\s*|\s+|(?=[\u3400-\u9fff]))"
)


@dataclass(frozen=True)
class SplitResult:
    records: list[ScenarioRecord]
    anomalies: list[dict[str, Any]]
    dimension_count: int


def split_and_write_scenarios(
    *,
    translated_rows: Sequence[dict[str, Any]],
    extracted_records: Sequence[dict[str, Any]],
    taxonomy_root: Path,
) -> SplitResult:
    """生成清理后的 taxonomy 文件，并按出现位置分配全局唯一 ID。"""
    extracted_by_id = {int(row["row_id"]): row for row in extracted_records}
    records: list[ScenarioRecord] = []
    anomalies: list[dict[str, Any]] = []
    used_directories: set[str] = set()
    dimension_index: list[dict[str, Any]] = []
    scenario_index: list[dict[str, Any]] = []

    for row in translated_rows:
        row_id = int(row["row_id"])
        extracted = extracted_by_id[row_id]
        source_sections = split_numbered_sections(row.get("criteria_zh", ""))
        translated_sections = split_numbered_sections(row.get("criteria_en", ""))
        if len(source_sections) != len(translated_sections):
            raise ValueError(
                f"row {row_id}: source has {len(source_sections)} sections but "
                f"translation has {len(translated_sections)}"
            )

        anomalies.extend(_section_anomalies(row, source_sections))
        source_numbers = [int(section["scene_no"]) for section in source_sections]
        translation_numbers = [
            int(section["scene_no"]) for section in translated_sections
        ]
        if translation_numbers != source_numbers:
            anomalies.append(
                {
                    "type": "translation_number_mismatch",
                    "row_id": row_id,
                    "source_numbers": source_numbers,
                    "translation_numbers": translation_numbers,
                }
            )

        category_en = [
            str(row.get("level1_en", "")).strip(),
            str(row.get("level2_en", "")).strip(),
            str(row.get("level3_en", "")).strip(),
            str(row.get("level4_en", "")).strip(),
        ]
        category_zh = [
            str(row.get("level1_zh", "")).strip(),
            str(row.get("level2_zh", "")).strip(),
            str(row.get("level3_zh", "")).strip(),
            str(row.get("level4_zh", "")).strip(),
        ]
        dimension_dir = _unique_dimension_dir(
            taxonomy_root / "by_dimension", category_en, row_id, used_directories
        )
        scenarios: list[dict[str, Any]] = []
        criteria = [str(item).strip() for item in extracted["judging_criteria"] if str(item).strip()]

        # 即使源编号出现 1、2、3、5、4、5、6 等异常，顺序 ID 仍保持唯一和稳定。
        for ordinal, (source, translated) in enumerate(
            zip(source_sections, translated_sections), start=1
        ):
            scenario_id = f"row{row_id:03d}_s{ordinal:02d}"
            translated_text = str(translated.get("text", "")).strip()
            if not translated_text:
                raise ValueError(f"{scenario_id}: empty translated scenario text")
            payload = {
                "scenario_id": scenario_id,
                "scenario_ordinal": ordinal,
                "source_scene_no": int(source.get("scene_no", ordinal)),
                "translation_scene_no": int(translated.get("scene_no", ordinal)),
                "scenario_title_en": _title_from_scene(translated_text),
                "scenario_title_zh": _title_from_scene(source.get("text", "")),
                "source_text_zh": source.get("text", ""),
                "translation_en": translated_text,
                "judging_criteria_en": criteria,
                "safe_content_boundary": extracted.get("safe_content_boundary", ""),
                "benchmark_generation_notes": extracted.get(
                    "benchmark_generation_notes", ""
                ),
            }
            scenarios.append(payload)
            record = ScenarioRecord(
                scenario_id=scenario_id,
                source_path=(dimension_dir / "scenarios.json").relative_to(taxonomy_root).as_posix(),
                category_path_en=category_en,
                title=payload["scenario_title_en"],
                translation_en=translated_text,
                judging_criteria_en=criteria,
                safe_content_boundary=str(payload["safe_content_boundary"]),
                benchmark_generation_notes=str(payload["benchmark_generation_notes"]),
                metadata={
                    "row_id": row_id,
                    "excel_row": row.get("excel_row"),
                    "scenario_ordinal": ordinal,
                    "source_scene_no": payload["source_scene_no"],
                    "translation_scene_no": payload["translation_scene_no"],
                    "value_paradigm": extracted.get("value_paradigm", ""),
                    "risk_domain": extracted.get("risk_domain", ""),
                    "benchmark_dimension": extracted.get("benchmark_dimension", ""),
                },
            )
            records.append(record)
            scenario_index.append(
                {
                    "scenario_id": scenario_id,
                    "row_id": row_id,
                    "dimension_dir": dimension_dir.relative_to(taxonomy_root).as_posix(),
                    "scenario_title_en": payload["scenario_title_en"],
                    "source_scene_no": payload["source_scene_no"],
                }
            )

        dimension_payload = {
            "row_id": row_id,
            "excel_row": row.get("excel_row"),
            "language": "en",
            "category_path_zh": category_zh,
            "category_path_en": category_en,
            "value_paradigm": extracted.get("value_paradigm", ""),
            "risk_domain": extracted.get("risk_domain", ""),
            "benchmark_dimension": extracted.get("benchmark_dimension", ""),
            "criteria_zh_full": row.get("criteria_zh", ""),
            "criteria_en_full": row.get("criteria_en", ""),
            "scenarios": scenarios,
        }
        atomic_write_json(dimension_dir / "scenarios.json", dimension_payload)
        dimension_index.append(
            {
                "row_id": row_id,
                "dimension_dir": dimension_dir.relative_to(taxonomy_root).as_posix(),
                "category_path_zh": category_zh,
                "category_path_en": category_en,
                "scenario_count": len(scenarios),
            }
        )

    ids = [record.scenario_id for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario splitter produced duplicate scenario_id values")
    atomic_write_json(taxonomy_root / "dimension_index.en.json", dimension_index)
    atomic_write_json(taxonomy_root / "scenario_index.en.json", scenario_index)
    return SplitResult(records, anomalies, len(dimension_index))


def inspect_source_numbering(rows: Sequence[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    count = 0
    anomalies: list[dict[str, Any]] = []
    for row in rows:
        sections = split_numbered_sections(row.get("criteria_zh", ""))
        count += len(sections)
        anomalies.extend(_section_anomalies(row, sections))
    return count, anomalies


def split_numbered_sections(text: Any) -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    matches = list(SECTION_MARKER.finditer(raw))
    if not matches:
        return [{"scene_no": 1, "text": raw}]
    sections: list[dict[str, Any]] = []
    prefix = raw[: matches[0].start()].strip()
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        body = raw[start:end].strip()
        if prefix and index == 0:
            body = f"{prefix}\n{body}".strip()
        if body:
            marker = match.group(0).lstrip("\n").lstrip()
            number = match.group(1)
            suffix = marker[len(number) :]
            if suffix.startswith((".", "．", "、")):
                marker_style = "punctuated"
            elif suffix and suffix[0].isspace():
                marker_style = "whitespace"
            else:
                marker_style = "implicit_cjk"
            sections.append(
                {
                    "scene_no": int(number),
                    "text": body,
                    "marker_style": marker_style,
                }
            )
    return sections or [{"scene_no": 1, "text": raw}]


def _section_anomalies(
    row: dict[str, Any],
    sections: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    row_id = int(row["row_id"])
    numbers = [int(section["scene_no"]) for section in sections]
    assigned = list(range(1, len(sections) + 1))
    anomalies: list[dict[str, Any]] = []
    if numbers != assigned:
        anomalies.append(
            {
                "type": "non_sequential_source_numbering",
                "row_id": row_id,
                "excel_row": row.get("excel_row"),
                "source_numbers": numbers,
                "assigned_ordinals": assigned,
            }
        )
    for ordinal, section in enumerate(sections, start=1):
        marker_style = str(section.get("marker_style", "punctuated"))
        if marker_style != "punctuated":
            anomalies.append(
                {
                    "type": "nonstandard_source_marker",
                    "row_id": row_id,
                    "excel_row": row.get("excel_row"),
                    "scenario_ordinal": ordinal,
                    "source_scene_no": int(section["scene_no"]),
                    "marker_style": marker_style,
                }
            )
    return anomalies


def _unique_dimension_dir(
    base: Path,
    category: Sequence[str],
    row_id: int,
    used: set[str],
) -> Path:
    path = base.joinpath(*[_short_slug(part) for part in category])
    key = str(path).casefold()
    if key in used:
        path = path.with_name(f"{path.name}-row{row_id:03d}")
        key = str(path).casefold()
    if key in used:
        raise ValueError(f"row {row_id}: duplicate dimension output path {path}")
    used.add(key)
    return path


def _short_slug(text: str, max_len: int = 20) -> str:
    """生成较短的路径片段，避免多层目录在 Windows 上超过路径长度限制。"""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"
    if len(slug) <= max_len:
        return slug
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:max_len - 9].rstrip('-')}-{digest}"
