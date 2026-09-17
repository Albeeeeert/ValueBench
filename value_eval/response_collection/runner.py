from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..clients.openai_compat import FatalModelError, OpenAICompatibleClient
from ..config import PipelineConfig
from ..io_utils import append_jsonl, atomic_write_json, iter_jsonl, load_json, stable_id, utc_now
from ..augmentation.base import resolve_artifact
from ..augmentation.runner import load_augmented_samples, response_datasets


MODES = {"image_text", "image_mcq", "description_text", "description_mcq"}


@dataclass(frozen=True)
class ResponseSample:
    benchmark: dict[str, Any]
    benchmark_path: Path
    image_path: Path | None
    image_sha256: str
    image_status: str
    dataset: str = "base"
    source: dict[str, Any] | None = None
    extra_image_paths: tuple[Path, ...] = ()

    @property
    def image_paths(self) -> list[Path]:
        return ([self.image_path] if self.image_path else []) + list(self.extra_image_paths)

    @property
    def benchmark_id(self) -> str:
        return str(self.benchmark["benchmark_id"])

    @property
    def profile(self) -> str:
        return str(self.benchmark.get("profile") or self.benchmark_path.parent.name)


def _image_index(run_root: Path) -> dict[tuple[str, str], tuple[Path | None, str, str]]:
    index: dict[tuple[str, str], tuple[Path | None, str, str]] = {}
    for manifest_path in sorted((run_root / "images").glob("*/manifest.json")):
        data = load_json(manifest_path)
        profile = str(data.get("profile", manifest_path.parent.name)) if isinstance(data, dict) else manifest_path.parent.name
        tasks = data.get("tasks", []) if isinstance(data, dict) else []
        for task in tasks if isinstance(tasks, list) else []:
            if not isinstance(task, dict):
                continue
            relative = str(task.get("image_path", ""))
            path = manifest_path.parent / relative if relative else None
            if path is not None and not path.is_file():
                path = None
            value = (path, str(task.get("image_sha256", "")), str(task.get("status", "missing")))
            for benchmark_id in task.get("benchmark_ids", []):
                index[(profile, str(benchmark_id))] = value
    return index


def load_samples(run_root: Path) -> list[ResponseSample]:
    images = _image_index(run_root)
    samples: list[ResponseSample] = []
    for path in sorted((run_root / "benchmark").glob("*/benchmark.json")):
        data = load_json(path)
        rows = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise ValueError(f"{path}: items must be an array")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{path}: benchmark item must be an object")
            benchmark_id = str(row.get("benchmark_id", "")).strip()
            profile = str(row.get("profile") or path.parent.name).strip()
            if not benchmark_id or not str(row.get("question", "")).strip():
                raise ValueError(f"{path}: item lacks benchmark_id or question")
            image_path, image_sha, image_status = images.get((profile, benchmark_id), (None, "", "missing"))
            samples.append(ResponseSample(row, path, image_path, image_sha, image_status))
    if not samples:
        raise FileNotFoundError(f"no benchmark samples found under {run_root / 'benchmark'}")
    return samples


def target_prompt(sample: ResponseSample, mode: str) -> str:
    row = sample.benchmark
    sections: list[str] = []
    if mode.startswith("description_"):
        sections.extend(["Image description:", str(row.get("image_description", "")), ""])
    sections.append(str(row["question"]))
    if mode.endswith("_mcq"):
        options = row.get("options")
        if not isinstance(options, dict) or not options:
            raise ValueError(f"MCQ sample has no options: {sample.benchmark_id}")
        sections.append("")
        sections.extend(f"{label}: {options[label]}" for label in sorted(options))
        sections.extend(["", "Return exactly one option letter: A, B, C, or D."])
    return "\n".join(sections)


def load_response_samples(config: PipelineConfig) -> list[ResponseSample]:
    samples: list[ResponseSample] = []
    for dataset in response_datasets(config):
        if dataset == "base":
            samples.extend(load_samples(config.run_root))
            continue
        for row in load_augmented_samples(config, dataset):
            source = row["source"]
            original = source["benchmark"]
            # Construct the model-facing row explicitly. Reference answers and audits
            # remain in provenance; no baseline MCQ fields are inherited.
            benchmark = {
                "benchmark_id": row["sample_id"], "profile": source["profile"],
                "scenario_question_style": source["style"], "question": row["input"]["text"],
                **{key: original.get(key, "") for key in (
                    "theme", "subdimension", "source_scenario_id", "source_element_id",
                )},
            }
            assets = row["input"]["images"]
            paths = [resolve_artifact(config.run_root, asset["path"]) for asset in assets]
            image_hash = assets[0]["sha256"] if len(assets) == 1 else stable_id(*(a["sha256"] for a in assets), length=64)
            samples.append(ResponseSample(
                benchmark, config.run_root / "augmentations" / dataset / "samples.jsonl",
                paths[0], image_hash, "generated", dataset, source, tuple(paths[1:]),
            ))
    return samples


class ResponseCollector:
    """Send benchmark inputs to the target and persist raw responses only."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        target: OpenAICompatibleClient | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.datasets = response_datasets(config)
        self.target = target or OpenAICompatibleClient(config.models[config.target_model], logger=self.logger)
        self.mode = str(config.response.get("mode", "image_text"))
        self.concurrency = int(config.response.get("concurrency", 2))
        self.continue_on_error = bool(config.response.get("continue_on_error", True))
        if self.mode not in MODES:
            raise ValueError(f"response.mode must be one of: {', '.join(sorted(MODES))}")
        if self.mode.startswith("image_") and not self.target.config.supports_images:
            raise ValueError(f"target model {self.target.config.model} must support images for {self.mode}")
        if self.concurrency < 1:
            raise ValueError("response.concurrency must be positive")

    def _response_path(self, profile: str, dataset: str = "base") -> Path:
        model_slug = "".join(character if character.isalnum() or character in "._-" else "_" for character in self.target.config.model)
        root = self.config.run_root / "responses" / model_slug / self.mode
        if dataset != "base":
            return root / "augmentations" / f"{dataset}.jsonl"
        return root / f"{profile}.jsonl"

    def _sample_key(self, sample: ResponseSample, prompt: str) -> str:
        return stable_id(
            self.config.execution_run_id,
            sample.profile,
            sample.dataset,
            sample.benchmark_id,
            self.target.config.model,
            self.mode,
            prompt,
            sample.image_sha256 if self.mode.startswith("image_") else "",
            length=64,
        )

    def _request(self, sample: ResponseSample) -> dict[str, Any]:
        prompt = target_prompt(sample, self.mode)
        key = self._sample_key(sample, prompt)
        self.logger.info(
            "target request started | mode=%s | profile=%s | benchmark=%s",
            self.mode,
            sample.profile,
            sample.benchmark_id,
        )
        base: dict[str, Any] = {
            "schema_version": "value-eval-response-v1",
            "sample_key": key,
            "run_id": self.config.execution_run_id,
            "benchmark_id": sample.benchmark_id,
            "dataset": sample.dataset,
            "method": sample.dataset if sample.dataset != "base" else "",
            "source_benchmark_id": sample.source["benchmark_id"] if sample.source else sample.benchmark_id,
            "profile": sample.profile,
            "question_style": str(
                sample.benchmark.get("scenario_question_style")
                or sample.benchmark.get("question_style", "")
            ),
            "theme": str(sample.benchmark.get("theme", "")),
            "subdimension": str(sample.benchmark.get("subdimension", "")),
            "source_scenario_id": str(sample.benchmark.get("source_scenario_id", "")),
            "source_element_id": str(sample.benchmark.get("source_element_id", "")),
            "source_benchmark": sample.benchmark_path.relative_to(self.config.run_root).as_posix(),
            "mode": self.mode,
            "target": self.target.config.public_dict(),
            "prompt_sha256": stable_id(prompt, length=64),
            "image_path": (
                sample.image_path.relative_to(self.config.run_root).as_posix()
                if sample.image_path else ""
            ),
            "image_sha256": sample.image_sha256,
            "image_paths": [path.relative_to(self.config.run_root).as_posix() for path in sample.image_paths],
            "image_status": sample.image_status,
            "created_at": utc_now(),
        }
        if self.mode.startswith("image_") and sample.image_path is None:
            self.logger.warning(
                "target request skipped | mode=%s | profile=%s | benchmark=%s | reason=%s",
                self.mode,
                sample.profile,
                sample.benchmark_id,
                sample.image_status,
            )
            return {**base, "status": "skipped_missing_image", "response": "", "error": sample.image_status}
        client = self.target.clone()
        try:
            result = client.chat([
                client.user_message(prompt, sample.image_paths if self.mode.startswith("image_") else [])
            ])
        except FatalModelError:
            raise
        except Exception as exc:
            if not self.continue_on_error:
                raise
            self.logger.exception("target request failed benchmark_id=%s", sample.benchmark_id)
            return {**base, "status": "failed", "response": "", "error": str(exc)}
        return {
            **base,
            "status": "completed",
            "response": result.content,
            "reasoning_content": result.reasoning_content,
            "usage": result.usage,
            "elapsed_sec": result.elapsed_sec,
            "request_id": result.request_id,
            "error": "",
        }

    def run(self, *, force: bool = False, max_items: int = 0) -> dict[str, Any]:
        samples = load_response_samples(self.config)
        if max_items > 0:
            samples = samples[:max_items]
        completed_keys: set[str] = set()
        if not force:
            for profile, dataset in {(sample.profile, sample.dataset) for sample in samples}:
                completed_keys.update(
                    str(row.get("sample_key"))
                    for row in iter_jsonl(self._response_path(profile, dataset))
                    if row.get("status") == "completed"
                )
        pending: list[ResponseSample] = []
        resumed = 0
        for sample in samples:
            prompt = target_prompt(sample, self.mode)
            if self._sample_key(sample, prompt) in completed_keys:
                resumed += 1
            else:
                pending.append(sample)
        counts = {"completed": 0, "failed": 0, "skipped": 0, "resumed": resumed}
        self.logger.info(
            "response collection ready | mode=%s | model=%s | selected=%d | resumed=%d | pending=%d | concurrency=%d",
            self.mode,
            self.target.config.model,
            len(samples),
            resumed,
            len(pending),
            self.concurrency,
        )
        executor = ThreadPoolExecutor(max_workers=self.concurrency)
        futures: dict[Future[dict[str, Any]], ResponseSample] = {
            executor.submit(self._request, sample): sample for sample in pending
        }
        try:
            for future in as_completed(futures):
                try:
                    record = future.result()
                except FatalModelError:
                    for other in futures:
                        other.cancel()
                    raise
                sample = futures[future]
                append_jsonl(self._response_path(sample.profile, sample.dataset), record)
                status = str(record["status"])
                if status == "completed":
                    counts["completed"] += 1
                elif status.startswith("skipped"):
                    counts["skipped"] += 1
                else:
                    counts["failed"] += 1
                processed = counts["completed"] + counts["failed"] + counts["skipped"]
                log = self.logger.info if status == "completed" else self.logger.warning
                log(
                    "target response finished | mode=%s | profile=%s | benchmark=%s | status=%s | progress=%d/%d | elapsed_sec=%s",
                    self.mode,
                    str(record.get("profile", futures[future].profile)),
                    str(record.get("benchmark_id", futures[future].benchmark_id)),
                    status,
                    resumed + processed,
                    len(samples),
                    str(record.get("elapsed_sec", "")),
                )
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        manifest = {
            "schema_version": "value-eval-response-manifest-v1",
            "run_id": self.config.execution_run_id,
            "mode": self.mode,
            "datasets": list(self.datasets),
            "target": self.target.config.public_dict(),
            "sample_count": len(samples),
            "counts": counts,
            "judge_enabled": False,
            "updated_at": utc_now(),
        }
        atomic_write_json(self.config.run_root / "responses" / "manifest.json", manifest)
        return manifest
