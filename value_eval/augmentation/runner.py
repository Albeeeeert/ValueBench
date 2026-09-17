from __future__ import annotations

import fcntl
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml
from PIL import Image

from ..config import PipelineConfig
from ..io_utils import atomic_write_json, atomic_write_text, load_json, load_json_if_exists, sha256_file, stable_id, utc_now
from .base import AugmentationMethod, AugmentationSource, fingerprint, resolve_artifact
from .registry import load_method, validate_method_names


def selected_methods(config: PipelineConfig, requested: Sequence[str] | None = None) -> tuple[str, ...]:
    enabled = config.augmentation_methods
    validate_method_names(enabled)
    if requested is None:
        return enabled
    validate_method_names(requested)
    disabled = set(requested) - set(enabled)
    if disabled:
        raise ValueError(f"augmentation method(s) not enabled: {', '.join(sorted(disabled))}")
    return tuple(dict.fromkeys(requested))


def validate_generation_scope(config: PipelineConfig) -> None:
    if selected_methods(config) and ("hh" not in config.profiles or "instruction" not in config.styles):
        raise ValueError("augmentation requires HH-instruction: generation.profiles must include hh and generation.styles must include instruction")


def response_datasets(config: PipelineConfig) -> tuple[str, ...]:
    raw = config.response.get("datasets", ["base", "enabled_augmentations"])
    if not isinstance(raw, list) or any(not isinstance(name, str) for name in raw):
        raise ValueError("response.datasets must be a list")
    result: list[str] = []
    for name in raw:
        if name == "enabled_augmentations":
            result.extend(selected_methods(config))
        elif name == "base":
            result.append(name)
        else:
            selected_methods(config, [name])
            result.append(name)
    if not result:
        raise ValueError("response.datasets selects no datasets")
    mode = str(config.response.get("mode", "image_text"))
    for name in result:
        if name != "base" and mode not in load_method(name).supported_response_modes:
            raise ValueError(f"augmentation {name} does not support response.mode={mode}; use image_text")
    return tuple(dict.fromkeys(result))


def load_sources(run_root: Path, *, require_images: bool = False) -> list[AugmentationSource]:
    path = run_root / "benchmark" / "hh" / "benchmark.json"
    if not path.is_file():
        raise ValueError(f"augmentation requires existing HH-instruction benchmark: {path}")
    payload = load_json(path)
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: items must be an array")
    image_index: dict[str, tuple[Path, str]] = {}
    if require_images:
        image_root = run_root / "images" / "hh"
        manifest = load_json_if_exists(image_root / "manifest.json", {})
        for task in manifest.get("tasks", []):
            if task.get("status") not in {"completed", "generated", "reused"} or not task.get("image_path"):
                continue
            image_path = resolve_artifact(image_root, task["image_path"])
            if image_path.is_file() and sha256_file(image_path) == task.get("image_sha256"):
                for bid in task.get("benchmark_ids", []):
                    image_index[str(bid)] = (image_path, task["image_sha256"])
    sources: list[AugmentationSource] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: item must be an object")
        if row.get("scenario_question_style") != "instruction":
            continue
        if row.get("profile", "hh") != "hh" or row.get("risk_combination_type", "H-H") != "H-H":
            raise ValueError(f"{path}: conflicting HH source profile")
        bid = str(row.get("benchmark_id", "")).strip()
        if not bid or not str(row.get("question", "")).strip() or bid in seen:
            raise ValueError(f"{path}: missing/duplicate ID or empty question: {bid!r}")
        seen.add(bid)
        image, image_sha = image_index.get(bid, (None, ""))
        if require_images and image is None:
            raise ValueError(f"augmentation requires a valid original image for hh/{bid}")
        sources.append(AugmentationSource(row, path.relative_to(run_root).as_posix(), image, image_sha))
    if not sources:
        raise ValueError(f"augmentation requires HH-instruction; no matching samples in {path}")
    return sources


def source_key(method: AugmentationMethod, source: AugmentationSource) -> str:
    return fingerprint({
        "schema": "augmentation-v1", "method": method.fingerprint,
        "source": source.provenance(),
        "original_image_sha256": source.image_sha256 if method.requires_original_image else "",
    })


def valid_sample(run_root: Path, row: dict[str, Any]) -> bool:
    try:
        if not row["sample_id"] or not row["input"]["text"].strip() or not row["input"]["images"]:
            return False
        for asset in row["input"]["images"]:
            path = resolve_artifact(run_root, asset["path"])
            if sha256_file(path) != asset["sha256"]:
                return False
            with Image.open(path) as img:
                if img.size != (asset["width"], asset["height"]) or img.format != asset["format"]:
                    return False
                img.verify()
        return True
    except (KeyError, TypeError, ValueError, OSError):
        return False


def valid_entry(run_root: Path, entry: Any, expected: str) -> bool:
    return (
        isinstance(entry, dict) and entry.get("status") == "completed"
        and entry.get("fingerprint") == expected and bool(entry.get("samples"))
        and all(valid_sample(run_root, row) for row in entry["samples"])
    )


@contextmanager
def method_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".generation.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"augmentation is already running: {directory.name}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_augmented_samples(config: PipelineConfig, name: str) -> list[dict[str, Any]]:
    selected_methods(config, [name])
    method = load_method(name)
    sources = load_sources(config.run_root, require_images=method.requires_original_image)
    directory = config.run_root / "augmentations" / name
    manifest = load_json_if_exists(directory / "manifest.json", {})
    if manifest.get("status") != "completed" or manifest.get("method_fingerprint") != method.fingerprint:
        raise ValueError(f"augmentation {name} is missing, incomplete or stale; run generate-augmentations")
    entries = manifest.get("entries", {})
    if set(entries) != {source.source_id for source in sources}:
        raise ValueError(f"augmentation {name} source coverage changed; run generate-augmentations")
    rows: list[dict[str, Any]] = []
    for source in sources:
        entry = entries[source.source_id]
        if not valid_entry(config.run_root, entry, source_key(method, source)):
            raise ValueError(f"augmentation {name} has stale or corrupt sample {source.source_id}; run generate-augmentations")
        rows.extend(entry["samples"])
    samples_path = directory / "samples.jsonl"
    if not samples_path.is_file() or sha256_file(samples_path) != manifest.get("samples_sha256"):
        raise ValueError(f"augmentation {name} samples.jsonl is missing or corrupt; run generate-augmentations")
    published = [json.loads(line) for line in samples_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if published != rows or len({row["sample_id"] for row in rows}) != len(rows):
        raise ValueError(f"augmentation {name} sample publication mismatch")
    return rows


def publish_dataset(config: PipelineConfig) -> dict[str, Any]:
    # Local import avoids a dependency cycle with the response loader.
    from ..response_collection.runner import load_samples

    base = load_samples(config.run_root)
    base_ready = 0
    for sample in base:
        if sample.image_path and sample.image_status in {"completed", "generated", "reused"}:
            if sample.image_sha256 and sha256_file(sample.image_path) == sample.image_sha256:
                base_ready += 1
    subsets: dict[str, Any] = {
        "base": {
            "sample_count": len(base), "ready_count": base_ready,
            "status": "completed" if base_ready == len(base) else "partial",
            "benchmark_files": sorted({s.benchmark_path.relative_to(config.run_root).as_posix() for s in base}),
            "image_manifests": sorted(p.relative_to(config.run_root).as_posix() for p in (config.run_root / "images").glob("*/manifest.json")),
        }
    }
    for name in selected_methods(config):
        try:
            rows = load_augmented_samples(config, name)
            subsets[name] = {
                "status": "completed", "sample_count": len(rows), "ready_count": len(rows),
                "samples": f"augmentations/{name}/samples.jsonl",
                "manifest": f"augmentations/{name}/manifest.json",
            }
        except (ValueError, OSError) as exc:
            subsets[name] = {"status": "unavailable", "sample_count": 0, "ready_count": 0, "error": str(exc)}
    result = {
        "schema_version": "value-eval-dataset-manifest-v1", "run_id": config.execution_run_id,
        "status": "completed" if all(s["status"] == "completed" for s in subsets.values()) else "partial",
        "sample_count": sum(s["sample_count"] for s in subsets.values()),
        "ready_count": sum(s["ready_count"] for s in subsets.values()),
        "subsets": subsets, "updated_at": utc_now(),
    }
    atomic_write_json(config.run_root / "dataset" / "manifest.json", result)
    return result


class AugmentationRunner:
    def __init__(self, config: PipelineConfig, *, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    def inspect(self, *, methods: Sequence[str] | None = None, max_items: int = 0) -> dict[str, Any]:
        names = selected_methods(self.config, methods)
        if not names:
            raise ValueError("no augmentation methods enabled; set augmentation.enabled=true and method: [figstep]")
        result: dict[str, Any] = {}
        for name in names:
            method = load_method(name)
            sources = load_sources(self.config.run_root, require_images=method.requires_original_image)
            result[name] = {
                "source_count": len(sources), "selected_source_count": min(max_items or len(sources), len(sources)),
                "requires_original_image": method.requires_original_image,
                "method_fingerprint": method.fingerprint, "parameters": method.snapshot,
            }
        return {"run_id": self.config.execution_run_id, "methods": result, "network_requests_made": False}

    def _generate(self, method: AugmentationMethod, source: AugmentationSource, directory: Path) -> dict[str, Any]:
        sid = f"{method.name}-{stable_id('hh', source.source_id)}"
        outputs = method.generate(source, sid, directory)
        if not outputs:
            raise ValueError("method returned no augmented samples")
        samples = []
        for index, output in enumerate(outputs):
            assets = []
            for path in output.image_paths:
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("method wrote an image outside its output directory")
                with Image.open(path) as img:
                    assets.append({
                        "path": path.relative_to(self.config.run_root).as_posix(),
                        "sha256": sha256_file(path), "width": img.width, "height": img.height, "format": img.format,
                    })
                    img.verify()
            row = {
                "schema_version": "value-eval-augmentation-v1",
                "sample_id": sid if len(outputs) == 1 else f"{sid}-{index + 1}",
                "method": method.name, "method_version": method.version,
                "source": source.provenance(),
                "input": {"text": output.prompt, "images": assets},
                "metadata": output.metadata,
            }
            if not valid_sample(self.config.run_root, row):
                raise ValueError("method returned invalid text or images")
            samples.append(row)
        return {"status": "completed", "fingerprint": source_key(method, source), "samples": samples}

    def _run_method(self, method: AugmentationMethod, *, force: bool, max_items: int) -> dict[str, Any]:
        sources = load_sources(self.config.run_root, require_images=method.requires_original_image)
        selected = sources[:max_items] if max_items else sources
        directory = self.config.run_root / "augmentations" / method.name
        with method_lock(directory):
            path = directory / "manifest.json"
            previous = load_json_if_exists(path, {}).get("entries", {})
            selected_ids = {source.source_id for source in selected}
            entries = {
                source.source_id: previous[source.source_id]
                for source in sources
                if not (force and source.source_id in selected_ids)
                and valid_entry(self.config.run_root, previous.get(source.source_id), source_key(method, source))
            }
            resumed = sum(source.source_id in entries for source in selected)
            counts = {"generated": 0, "resumed": resumed, "failed": 0}
            manifest = {
                "schema_version": "value-eval-augmentation-manifest-v1",
                "run_id": self.config.execution_run_id, "method": method.name, "method_version": method.version,
                "method_fingerprint": method.fingerprint, "source_count": len(sources),
                "selected_source_count": len(selected), "status": "building", "entries": entries,
                "counts": counts, "updated_at": utc_now(),
            }
            atomic_write_json(path, manifest)
            atomic_write_text(directory / "config.snapshot.yaml", yaml.safe_dump(method.snapshot, allow_unicode=True, sort_keys=False))
            self.logger.info("augmentation starting | method=%s | sources=%d | selected=%d | resumed=%d", method.name, len(sources), len(selected), resumed)
            with ThreadPoolExecutor(max_workers=method.concurrency) as executor:
                futures = {
                    executor.submit(self._generate, method, source, directory): source
                    for source in selected if source.source_id not in entries
                }
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        entries[source.source_id] = future.result()
                        counts["generated"] += 1
                    except Exception as exc:
                        counts["failed"] += 1
                        entries[source.source_id] = {"status": "failed", "fingerprint": source_key(method, source), "error": str(exc)}
                        self.logger.exception("augmentation failed | method=%s | source=%s", method.name, source.source_id)
                    manifest["updated_at"] = utc_now()
                    atomic_write_json(path, manifest)
            # Stable source order keeps exports and response sample selection deterministic.
            rows = [row for source in sources for row in entries.get(source.source_id, {}).get("samples", [])]
            sample_path = directory / "samples.jsonl"
            atomic_write_text(sample_path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))
            completed = sum(entry["status"] == "completed" for entry in entries.values())
            manifest.update({
                "status": "failed" if counts["failed"] else ("completed" if completed == len(sources) else "partial"),
                "completed_source_count": completed, "sample_count": len(rows),
                "samples_sha256": sha256_file(sample_path), "updated_at": utc_now(),
            })
            atomic_write_json(path, manifest)
            self.logger.info("augmentation finished | method=%s | status=%s | counts=%s", method.name, manifest["status"], counts)
            return {key: value for key, value in manifest.items() if key != "entries"}

    def run(self, *, methods: Sequence[str] | None = None, force: bool = False, max_items: int = 0) -> dict[str, Any]:
        if max_items < 0:
            raise ValueError("max_items must be non-negative")
        names = selected_methods(self.config, methods)
        self.inspect(methods=names, max_items=max_items)
        results = {}
        try:
            for name in names:
                results[name] = self._run_method(load_method(name), force=force, max_items=max_items)
        finally:
            dataset = publish_dataset(self.config)
        if any(result["status"] == "failed" for result in results.values()):
            raise RuntimeError("augmentation has failed samples; inspect augmentations/<method>/manifest.json and rerun")
        return {"methods": results, "dataset": dataset}
