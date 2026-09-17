from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..clients.openai_compat import OpenAICompatibleClient
from ..config import PipelineConfig
from .utils import atomic_write_json
from .taxonomy import extract_records, translate_rows



@dataclass(frozen=True)
class TaxonomyResult:
    translated_rows: list[dict[str, Any]]
    records: list[dict[str, Any]]
    source: str


def prepare_taxonomy(
    *,
    cfg: PipelineConfig,
    source_rows: list[dict[str, Any]],
    taxonomy_root: Path,
    logger: logging.Logger,
    force: bool,
) -> TaxonomyResult:
    atomic_write_json(taxonomy_root / "source_rows.zh.json", source_rows)
    translation_client = _client(
        cfg,
        cfg.scenario_source.translation_model,
        logger,
        purpose="translation",
    )
    taxonomy_client = _client(cfg, cfg.scenario_source.taxonomy_model, logger, purpose="taxonomy")
    translated = translate_rows(
        rows=source_rows,
        client=translation_client,
        output_root=taxonomy_root,
        chunk_by=cfg.scenario_source.chunk_by,
        chunk_size=cfg.scenario_source.chunk_size,
        debug_save_llm_io=cfg.scenario_source.debug_save_llm_io,
        resume=not force,
    )
    records = extract_records(
        translated_rows=translated,
        client=taxonomy_client,
        output_root=taxonomy_root,
        chunk_by=cfg.scenario_source.chunk_by,
        chunk_size=cfg.scenario_source.chunk_size,
        debug_save_llm_io=cfg.scenario_source.debug_save_llm_io,
        resume=not force,
    )

    _validate_complete_rows(source_rows, translated, records)
    return TaxonomyResult(translated_rows=translated, records=records, source="llm")


def _validate_complete_rows(
    source: list[dict[str, Any]],
    translated: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> None:
    expected = {int(row["row_id"]) for row in source}
    for label, rows in (("translation", translated), ("extraction", records)):
        ids = [int(row.get("row_id", -1)) for row in rows if isinstance(row, dict)]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{label} contains duplicate row_id values")
        if set(ids) != expected:
            missing = sorted(expected - set(ids))
            extra = sorted(set(ids) - expected)
            raise ValueError(f"{label} row mismatch: missing={missing}, extra={extra}")

    for row in translated:
        required = ("level1_en", "level2_en", "level3_en", "level4_en", "criteria_en")
        missing = [key for key in required if not str(row.get(key, "")).strip()]
        if missing:
            raise ValueError(f"translation row {row.get('row_id')} is missing {missing}")
    for row in records:
        if not row.get("judging_criteria"):
            raise ValueError(f"extraction row {row.get('row_id')} has no judging criteria")


def _client(
    cfg: PipelineConfig,
    alias: str,
    logger: logging.Logger,
    *,
    purpose: str,
) -> OpenAICompatibleClient:
    model = cfg.models[alias]
    if not model.api_key():
        raise ValueError(f"missing {purpose} API key in environment variable {model.api_key_env}")
    return OpenAICompatibleClient(model, logger=logger)
