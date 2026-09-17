from __future__ import annotations

import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .generation.input_loader import (
    expand_scenario_variants,
    load_scenario_elements,
    load_selection_pairs,
)
from .generation.runner import BenchmarkGenerator
from .image_generation.runner import ImageGenerator, discover_tasks
from .io_utils import atomic_write_json, utc_now
from .response_collection.runner import ResponseCollector, load_samples
from .value_to_scenario import PreparationOptions, ScenarioPreparationRunner


def generation_config(config: PipelineConfig) -> PipelineConfig:
    """Excel 模式下让下游阶段读取同一 run 内刚发布的场景数据。"""
    if config.scenario_source.mode == "prepared":
        return config
    return replace(
        config,
        input_dir=config.prepared_scenario_dir,
        input_manifest=config.prepared_scenario_manifest,
        input_selection=None,
    )


def _portable(config: PipelineConfig, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(config.root).as_posix()
    except ValueError:
        return f"external/{path.name}"


def build_logger(config: PipelineConfig) -> logging.Logger:
    log_path = config.log_root / config.execution_run_id / "pipeline.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"value_eval.{config.execution_run_id}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger


def inspect_pipeline(config: PipelineConfig) -> dict[str, Any]:
    effective = generation_config(config)
    prepared_ready = effective.input_dir.is_dir() and (
        effective.input_manifest is None or effective.input_manifest.is_file()
    )
    source_units = []
    units = []
    if prepared_ready:
        source_units = load_scenario_elements(
            effective.input_dir,
            effective.input_manifest,
            selected_pairs=load_selection_pairs(effective.input_selection),
        )
        units = expand_scenario_variants(source_units, effective.variants_per_scenario)
    excel = None
    if config.scenario_source.mode == "excel":
        excel = ScenarioPreparationRunner(config, logging.getLogger(__name__)).inspect_excel()
    benchmark_root = config.run_root / "benchmark"
    image_root = config.run_root / "images"
    benchmark_files = sorted(benchmark_root.glob("*/benchmark.json")) if benchmark_root.exists() else []
    image_tasks = 0
    if benchmark_files:
        image_tasks = sum(len(tasks) for tasks in discover_tasks(benchmark_root).values())
    response_samples = len(load_samples(config.run_root)) if benchmark_files else 0
    required_aliases = {config.planner_model, config.author_model, config.target_model}
    if config.scenario_source.mode == "excel":
        required_aliases.update({
            config.scenario_source.translation_model,
            config.scenario_source.taxonomy_model,
            config.scenario_source.stage1_model,
            config.scenario_source.element_model,
        })
    required_envs = sorted({config.models[alias].api_key_env for alias in required_aliases})
    image_env = str(config.image.get("api_key_env", "DASHSCOPE_API_KEY"))
    required_envs = sorted(set(required_envs + [image_env]))
    target = config.models[config.target_model]
    mode = str(config.response.get("mode", "image_text"))
    planned_capacity = len(units) * len(config.styles)
    estimated_minimum = None
    if excel is not None and not units:
        jobs_per_scenario = (
            config.variants_per_scenario
            if config.variants_per_scenario > 0
            else config.scenario_source.min_elements
        )
        estimated_minimum = int(excel["expected_scenarios"]) * jobs_per_scenario * len(config.styles)
        planned_capacity = estimated_minimum
    return {
        "schema_version": "value-eval-preflight-v1",
        "run_id": config.execution_run_id,
        "base_run_id": config.run_id,
        "config": _portable(config, config.config_path),
        "input_dir": _portable(config, effective.input_dir),
        "input_manifest": _portable(config, effective.input_manifest),
        "input_selection": _portable(config, effective.input_selection),
        "scenario_count": len({unit.scenario_id for unit in source_units}),
        "scenario_element_count": len(source_units),
        "generation_job_count": len(units),
        "scenario_source_mode": config.scenario_source.mode,
        "prepared_scenarios_ready": prepared_ready,
        "excel": excel,
        "profiles": list(config.profiles),
        "styles": list(config.styles),
        "planned_questions_per_profile": min(
            planned_capacity,
            config.max_items_per_profile or planned_capacity,
        ),
        "variants_per_scenario": config.variants_per_scenario,
        "estimated_min_questions_before_limit": estimated_minimum,
        "existing_benchmark_files": [_portable(config, path) for path in benchmark_files],
        "existing_image_task_count": image_tasks,
        "existing_response_sample_count": response_samples,
        "response_mode": mode,
        "generate_images_during_benchmark": config.generate_images_during_benchmark,
        "target_supports_images": target.supports_images,
        "model_capability_ready": not mode.startswith("image_") or target.supports_images,
        "required_api_key_envs": required_envs,
        "missing_api_key_envs": [name for name in required_envs if not os.getenv(name, "").strip()],
        "network_requests_made": False,
    }


class Pipeline:
    def __init__(self, config: PipelineConfig, *, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or build_logger(config)

    def run_benchmark(self, *, force: bool = False) -> dict[str, Any]:
        self.logger.info("stage=benchmark status=starting")
        effective = generation_config(self.config)
        incremental_images = (
            ImageGenerator(self.config, logger=self.logger, incremental_force=force)
            if self.config.generate_images_during_benchmark
            else None
        )
        generator = BenchmarkGenerator(
            effective,
            logger=self.logger,
            on_items_committed=(
                incremental_images.submit_items if incremental_images is not None else None
            ),
        )
        try:
            result = generator.run(force=force)
        except Exception:
            if incremental_images is not None:
                summary = incremental_images.finish_incremental(raise_fatal=False)
                self.logger.info(
                    "stage=incremental_images status=stopped benchmark_failed=true result=%s",
                    summary,
                )
            raise
        if incremental_images is not None:
            try:
                incremental_images.submit_benchmark_root(self.config.run_root / "benchmark")
            except Exception:
                incremental_images.finish_incremental(raise_fatal=False)
                raise
            result["images_during_benchmark"] = incremental_images.finish_incremental()
        self.logger.info("stage=benchmark status=completed result=%s", result)
        return result

    def run_images(self, *, force: bool = False, max_items: int = 0) -> dict[str, Any]:
        self.logger.info("stage=images status=starting")
        result = ImageGenerator(self.config, logger=self.logger).run(force=force, max_items=max_items)
        self.logger.info("stage=images status=completed result=%s", result)
        return result

    def run_responses(self, *, force: bool = False, max_items: int = 0) -> dict[str, Any]:
        self.logger.info("stage=responses status=starting judge=false")
        result = ResponseCollector(self.config, logger=self.logger).run(force=force, max_items=max_items)
        self.logger.info("stage=responses status=completed result=%s", result)
        return result

    def inspect_excel(self) -> dict[str, Any]:
        return ScenarioPreparationRunner(self.config, self.logger).inspect_excel()

    def prepare_scenarios(self, options: PreparationOptions) -> dict[str, Any]:
        self.logger.info("stage=scenario_preparation status=starting")
        result = ScenarioPreparationRunner(self.config, self.logger).run(options)
        self.logger.info("stage=scenario_preparation status=completed result=%s", result)
        return result

    def validate_scenarios(self) -> dict[str, Any]:
        effective = generation_config(self.config)
        units = load_scenario_elements(
            effective.input_dir,
            effective.input_manifest,
            selected_pairs=load_selection_pairs(effective.input_selection),
        )
        return {
            "schema_version": "scenario-input-validation-v1",
            "valid": True,
            "input_dir": _portable(self.config, effective.input_dir),
            "manifest": _portable(self.config, effective.input_manifest),
            "scenario_count": len({unit.scenario_id for unit in units}),
            "element_count": len(units),
        }

    def run_all(
        self,
        *,
        force: bool = False,
        retry_fallbacks: bool = False,
    ) -> dict[str, Any]:
        manifest_path = self.config.run_root / "run_manifest.json"
        manifest: dict[str, Any] = {
            "schema_version": "value-eval-run-manifest-v1",
            "run_id": self.config.execution_run_id,
            "base_run_id": self.config.run_id,
            "config": _portable(self.config, self.config.config_path),
            "status": "running",
            "started_at": utc_now(),
            "judge_enabled": False,
            "stages": {},
        }
        atomic_write_json(manifest_path, manifest)
        try:
            if self.config.scenario_source.mode == "excel":
                manifest["stages"]["scenario_preparation"] = self.prepare_scenarios(
                    PreparationOptions(
                        force=force,
                        retry_fallbacks=retry_fallbacks,
                    )
                )
                atomic_write_json(manifest_path, manifest)
            manifest["stages"]["benchmark"] = self.run_benchmark(force=force)
            atomic_write_json(manifest_path, manifest)
            manifest["stages"]["images"] = self.run_images(
                force=force and not self.config.generate_images_during_benchmark
            )
            atomic_write_json(manifest_path, manifest)
            manifest["stages"]["responses"] = self.run_responses(force=force)
            manifest.update({"status": "completed", "completed_at": utc_now()})
        except Exception as exc:
            manifest.update({
                "status": "failed",
                "completed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            atomic_write_json(manifest_path, manifest)
            raise
        atomic_write_json(manifest_path, manifest)
        return manifest
