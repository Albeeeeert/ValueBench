from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from ..clients.openai_compat import FatalModelError, OpenAICompatibleClient, OpenAICompatibleClient as LLMClient
from ..config import PipelineConfig
from .utils import atomic_write_json, load_json_if_exists
from .element_builder import ScenarioElementBuilder
from .schema import ScenarioRecord



def build_elements(
    *,
    cfg: PipelineConfig,
    records: Sequence[ScenarioRecord],
    output_dir: Path,
    cache_dir: Path,
    logger: logging.Logger,
    force: bool,
    retry_fallbacks: bool,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    client = _client(
        cfg, cfg.scenario_source.element_model, logger, purpose="element"
    )
    stage1_client = _client(
        cfg, cfg.scenario_source.stage1_model, logger, purpose="stage1"
    )
    primary = _builder(
        cfg=cfg,
        client=client,
        stage1_client=stage1_client,
        output_dir=output_dir,
        cache_dir=cache_dir,
        logger=logger,
        use_llm=True,
        force=force,
    )
    fallback = _builder(
        cfg=cfg,
        client=None,
        stage1_client=None,
        output_dir=output_dir,
        cache_dir=None,
        logger=logger,
        use_llm=False,
        force=force,
    )
    statuses: list[dict[str, Any]] = []

    workers = min(cfg.scenario_source.concurrency, max(1, len(records)))
    logger.info("scenario element workers: %d", workers)
    if workers == 1:
        for index, record in enumerate(records, start=1):
            result = _build_one_record(
                cfg=cfg,
                record=record,
                output_dir=output_dir,
                primary=primary,
                fallback=fallback,
                logger=logger,
                force=force,
                retry_fallbacks=retry_fallbacks,
            )
            statuses.append(result)
            _log_progress(logger, index, len(records), result)
    else:
        executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="scenario-element",
        )
        futures = []
        try:
            futures = [
                executor.submit(
                    _build_one_record,
                    cfg=cfg,
                    record=record,
                    output_dir=output_dir,
                    primary=primary,
                    fallback=fallback,
                    logger=logger,
                    force=force,
                    retry_fallbacks=retry_fallbacks,
                )
                for record in records
            ]
            for completed, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                statuses.append(result)
                _log_progress(logger, completed, len(records), result)
        except KeyboardInterrupt:
            cancelled = sum(future.cancel() for future in futures)
            running = sum(future.running() for future in futures)
            logger.warning(
                "scenario element interrupted | cancelled_pending=%d | "
                "running_requests=%d | press Ctrl+C again for immediate exit",
                cancelled,
                running,
            )
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    record_order = {
        record.scenario_id: index for index, record in enumerate(records)
    }
    statuses.sort(key=lambda item: record_order[str(item["scenario_id"])])

    counts: dict[str, int] = {}
    for item in statuses:
        status = str(item["status"])
        counts[status] = counts.get(status, 0) + 1
    summary = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "items": statuses,
    }
    atomic_write_json(output_dir.parent / "element_status.json", summary)
    return summary


def _build_one_record(
    *,
    cfg: PipelineConfig,
    record: ScenarioRecord,
    output_dir: Path,
    primary: ScenarioElementBuilder,
    fallback: ScenarioElementBuilder,
    logger: logging.Logger,
    force: bool,
    retry_fallbacks: bool,
) -> dict[str, Any]:
    """生成单个场景文件，供串行模式和并发任务共同调用。"""
    path = output_dir / f"{record.scenario_id}.json"
    if path.exists() and not force:
        existing = load_json_if_exists(path, None)
        if _valid_cached_plan(
            existing,
            record,
            retry_fallbacks=retry_fallbacks,
        ):
            return {"scenario_id": record.scenario_id, "status": "cached"}

    method = "llm"
    error = ""
    requires_review = False

    try:
        plan = primary.build_one(record, cache_key=record.scenario_id)
    except FatalModelError:
        raise
    except Exception as exc:
        if not cfg.scenario_source.fallback_on_llm_error:
            raise
        logger.warning(
            "scenario %s failed LLM generation; using heuristic fallback: %s",
            record.scenario_id,
            exc,
        )
        plan = fallback.build_one(record, cache_key=record.scenario_id)
        method = "heuristic_fallback"
        error = str(exc)[:2000]
        requires_review = True

    payload = asdict(plan)
    payload["source"] = dict(record.metadata)
    payload["generation"] = {
        "method": method,
        "requires_manual_review": requires_review,
        "llm_error": error,
    }
    atomic_write_json(path, payload)
    return {"scenario_id": record.scenario_id, "status": method}


def _log_progress(
    logger: logging.Logger,
    completed: int,
    total: int,
    result: dict[str, Any],
) -> None:
    """每完成一个场景就输出进度，便于观察长时间 API 构建。"""
    logger.info(
        "scenario elements progress: %d/%d | scenario=%s | status=%s",
        completed,
        total,
        result.get("scenario_id", "unknown"),
        result.get("status", "unknown"),
    )


def _valid_cached_plan(
    raw: Any,
    record: ScenarioRecord,
    *,
    retry_fallbacks: bool,
) -> bool:
    if not (
        isinstance(raw, dict)
        and raw.get("scenario_id") == record.scenario_id
        and raw.get("source_text") == record.translation_en
        and isinstance(raw.get("elements"), list)
        and bool(raw["elements"])
    ):
        return False
    generation = raw.get("generation", {})
    method = str(generation.get("method", "")) if isinstance(generation, dict) else ""
    if method == "heuristic":
        return False
    if retry_fallbacks and method == "heuristic_fallback":
        return False
    return True


def _builder(
    *,
    cfg: PipelineConfig,
    client: Optional[LLMClient],
    stage1_client: Optional[LLMClient],
    output_dir: Path,
    cache_dir: Optional[Path],
    logger: logging.Logger,
    use_llm: bool,
    force: bool,
) -> ScenarioElementBuilder:
    return ScenarioElementBuilder(
        client=client,
        stage1_client=stage1_client,
        output_dir=output_dir,
        cache_dir=cache_dir,
        failure_path=None,
        logger=logger,
        min_elements=cfg.scenario_source.min_elements,
        use_llm=use_llm,
        overwrite=force,
        reuse_legacy_outputs=False,
    )


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
