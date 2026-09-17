from __future__ import annotations

from pathlib import Path
from typing import Any

from .taxonomy import read_source_rows


def read_and_validate_workbook(
    input_xlsx: Path,
    sheet_name: str,
) -> list[dict[str, Any]]:
    """读取并校验工作簿，确保付费 API 调用前输入数据满足约束。"""
    rows = read_source_rows(input_xlsx, sheet_name)
    if not rows:
        raise ValueError("workbook contains no non-empty taxonomy rows")

    row_ids = [int(row["row_id"]) for row in rows]
    expected_ids = list(range(1, len(rows) + 1))
    if row_ids != expected_ids:
        raise ValueError("workbook row_id values are not contiguous")

    required = ("level1_zh", "level2_zh", "level3_zh", "level4_zh", "criteria_zh")
    for row in rows:
        missing = [name for name in required if not str(row.get(name, "")).strip()]
        if missing:
            raise ValueError(f"row {row['row_id']} has empty required fields: {missing}")
    return rows
