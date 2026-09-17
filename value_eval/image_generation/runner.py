from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from ..config import LocalImageConfig, PipelineConfig
from ..io_utils import atomic_write_json, load_json, sha256_bytes, stable_id, utc_now
from ..schemas import ImageTask
from .client import (
    FatalImageApiError,
    ImageModerationError,
    QwenImageClient,
    QwenImageConfig,
)
from .local_client import FatalLocalImageError, LocalQwenImageClient


def _expected_size(value: str) -> tuple[int, int]:
    parts = value.lower().replace("x", "*").split("*", 1)
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise ValueError("image.size must look like 2048*2048")
    return int(parts[0]), int(parts[1])


def probe_image(data: bytes) -> tuple[str, int, int]:
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = str(image.format or "").lower()
            width, height = image.size
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ValueError(f"generated data is not a valid image: {exc}") from exc
    if image_format not in ("png", "jpeg"):
        raise ValueError(f"unsupported image format: {image_format}")
    return image_format, width, height


def _tasks_from_rows(
    rows: list[Any],
    *,
    default_profile: str,
    owner: str,
) -> list[ImageTask]:
    grouped: dict[str, ImageTask] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{owner}: benchmark item must be an object")
        benchmark_id = str(row.get("benchmark_id", "")).strip()
        prompt = str(row.get("image_description", "")).strip()
        profile = str(row.get("profile") or default_profile).strip()
        shared = str(row.get("shared_image_id", "")).strip()
        if not benchmark_id or not prompt or not profile:
            raise ValueError(f"{owner}: item lacks benchmark_id, profile, or image_description")
        group = "shared:" + shared if shared else "prompt:" + stable_id(prompt, length=64)
        if group in grouped:
            if grouped[group].prompt != prompt:
                raise ValueError(f"shared image has conflicting prompts: {shared}")
            grouped[group].benchmark_ids.append(benchmark_id)
        else:
            grouped[group] = ImageTask(
                image_key=stable_id(profile, group),
                prompt=prompt,
                benchmark_ids=[benchmark_id],
                profile=profile,
                shared_image_id=shared,
            )
    return list(grouped.values())


def discover_tasks(benchmark_root: Path) -> dict[str, list[ImageTask]]:
    tasks_by_profile: dict[str, list[ImageTask]] = {}
    for benchmark_path in sorted(benchmark_root.glob("*/benchmark.json")):
        data = load_json(benchmark_path)
        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            raise ValueError(f"{benchmark_path}: items must be an array")
        tasks_by_profile[benchmark_path.parent.name] = _tasks_from_rows(
            items,
            default_profile=benchmark_path.parent.name,
            owner=str(benchmark_path),
        )
    if not tasks_by_profile:
        raise FileNotFoundError(f"no */benchmark.json found under {benchmark_root}")
    return tasks_by_profile


class ImageGenerator:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        client: QwenImageClient | LocalQwenImageClient | None = None,
        logger: logging.Logger | None = None,
        incremental_force: bool = False,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        raw = config.active_image
        api_key_env = str(raw.get("api_key_env", "DASHSCOPE_API_KEY"))
        if client is None and config.image_backend == "local":
            client = LocalQwenImageClient(
                LocalImageConfig.from_mapping(raw, config.root), logger=self.logger
            )
        if client is None:
            api_key = os.getenv(api_key_env, "").strip()
            if not api_key:
                raise RuntimeError(f"missing API key environment variable: {api_key_env}")
            image_config = QwenImageConfig(
                api_key=api_key,
                model=str(raw.get("model", "qwen-image-2.0")),
                endpoint=str(raw.get("endpoint", QwenImageConfig.endpoint)),
                task_endpoint=str(raw.get("task_endpoint", QwenImageConfig.task_endpoint)),
                size=str(raw.get("size", "2048*2048")),
                prompt_extend=bool(raw.get("prompt_extend", False)),
                async_call=bool(raw.get("async_call", False)),
                timeout_sec=int(raw.get("timeout_sec", 120)),
                max_retries=int(raw.get("max_retries", 3)),
                rate_limit_retries=int(raw.get("rate_limit_retries", 6)),
            )
            client = QwenImageClient(image_config, logger=self.logger)
        self.client = client
        self.incremental_force = incremental_force
        self.concurrency = int(raw.get("concurrency", 1 if config.image_backend == "local" else 3))
        if self.concurrency < 1:
            raise ValueError("image.concurrency must be positive")
        if config.image_backend == "local" and self.concurrency != 1:
            raise ValueError("local_image.concurrency must be 1")
        self.generation_settings = (
            client.generation_settings() if isinstance(client, LocalQwenImageClient) else {
                "backend": config.image_backend,
                "model": client.config.model,
                "size": client.config.size,
                "prompt_extend": bool(raw.get("prompt_extend", False)),
            }
        )
        self.generation_fingerprint = stable_id(
            json.dumps(self.generation_settings, sort_keys=True, ensure_ascii=False), length=64
        )
        self._incremental_lock = threading.Lock()
        self._incremental_executor: ThreadPoolExecutor | None = None
        self._incremental_tasks: dict[str, dict[str, ImageTask]] = {}
        self._incremental_futures: dict[Future[None], tuple[str, str]] = {}
        self._incremental_fatal_errors: list[FatalImageApiError | FatalLocalImageError] = []

    def close(self) -> None:
        if isinstance(self.client, LocalQwenImageClient):
            self.client.close()

    def _manifest_matches(self, previous: Any) -> bool:
        if not isinstance(previous, dict):
            return False
        if previous.get("model") != self.client.config.model or previous.get("size") != self.client.config.size:
            return False
        fingerprint = previous.get("generation_fingerprint")
        if fingerprint:
            return fingerprint == self.generation_fingerprint
        # Legacy manifests were produced by the API backend only.
        return self.config.image_backend == "api" and previous.get("backend", "api") == "api"

    def _profile_root(self, profile: str) -> Path:
        return self.config.run_root / "images" / profile

    def _manifest(self, profile: str) -> Path:
        return self._profile_root(profile) / "manifest.json"

    def _save(self, profile: str, tasks: list[ImageTask]) -> None:
        payload = {
            "schema_version": "value-eval-image-manifest-v1",
            "run_id": self.config.execution_run_id,
            "profile": profile,
            "model": self.client.config.model,
            "size": self.client.config.size,
            "backend": self.config.image_backend,
            "generation_settings": self.generation_settings,
            "generation_fingerprint": self.generation_fingerprint,
            "updated_at": utc_now(),
            "task_count": len(tasks),
            "completed_count": sum(task.status in ("generated", "reused") for task in tasks),
            "failed_count": sum(task.status in ("failed", "moderated", "aborted") for task in tasks),
            "tasks": [task.as_dict() for task in tasks],
        }
        atomic_write_json(self._manifest(profile), payload)

    @staticmethod
    def _task_from_manifest(row: dict[str, Any], profile: str) -> ImageTask:
        return ImageTask(
            image_key=str(row.get("image_key", "")),
            prompt=str(row.get("prompt", "")),
            benchmark_ids=[str(value) for value in row.get("benchmark_ids", [])],
            profile=str(row.get("profile", profile)),
            shared_image_id=str(row.get("shared_image_id", "")),
            status=str(row.get("status", "pending")),
            attempts=int(row.get("attempts", 0)),
            image_path=str(row.get("image_path", "")),
            image_sha256=str(row.get("image_sha256", "")),
            image_model=str(row.get("image_model", "")),
            image_format=str(row.get("image_format", "")),
            width=int(row.get("width", 0)),
            height=int(row.get("height", 0)),
            request_ids=[str(value) for value in row.get("request_ids", [])],
            seed=int(row["seed"]) if row.get("seed") is not None else None,
            error=str(row.get("error", "")),
        )

    def _load_incremental_tasks(self, profile: str) -> dict[str, ImageTask]:
        if profile in self._incremental_tasks:
            return self._incremental_tasks[profile]
        tasks: dict[str, ImageTask] = {}
        path = self._manifest(profile)
        if path.is_file() and not self.incremental_force:
            previous = load_json(path)
            if self._manifest_matches(previous):
                for row in previous.get("tasks", []):
                    if not isinstance(row, dict) or not row.get("image_key"):
                        continue
                    task = self._task_from_manifest(row, profile)
                    tasks[task.image_key] = task
        self._incremental_tasks[profile] = tasks
        return tasks

    def _saved_task_is_valid(self, profile: str, task: ImageTask) -> bool:
        if task.status not in ("generated", "reused") or not task.image_path:
            return False
        path = self._profile_root(profile) / task.image_path
        if not path.is_file():
            return False
        try:
            data = path.read_bytes()
            image_format, width, height = probe_image(data)
        except (OSError, ValueError):
            return False
        return (
            (width, height) == _expected_size(self.client.config.size)
            and sha256_bytes(data) == task.image_sha256
            and image_format == task.image_format
        )

    def _save_incremental_profile(self, profile: str) -> None:
        tasks = self._incremental_tasks.get(profile, {})
        self._save(profile, list(tasks.values()))

    def _submit_incremental_task(self, incoming: ImageTask) -> None:
        profile = incoming.profile
        submitted: Future[None] | None = None
        with self._incremental_lock:
            if self._incremental_fatal_errors:
                raise self._incremental_fatal_errors[0]
            tasks = self._load_incremental_tasks(profile)
            task = tasks.get(incoming.image_key)
            if task is not None:
                if task.prompt != incoming.prompt or task.shared_image_id != incoming.shared_image_id:
                    raise ValueError(f"image task key collision: {incoming.image_key}")
                task.benchmark_ids = list(dict.fromkeys([*task.benchmark_ids, *incoming.benchmark_ids]))
                if self._saved_task_is_valid(profile, task):
                    task.status = "reused"
                    task.error = ""
                    self._save_incremental_profile(profile)
                    self.logger.info(
                        "image task reused | profile=%s | key=%s | completed=%d/%d",
                        profile,
                        task.image_key,
                        sum(item.status in ("generated", "reused") for item in tasks.values()),
                        len(tasks),
                    )
                    return
                if any(key == incoming.image_key for _, key in self._incremental_futures.values()):
                    return
                task.status = "pending"
                task.error = ""
            else:
                task = incoming
                tasks[task.image_key] = task
            if self._incremental_executor is None:
                self._incremental_executor = ThreadPoolExecutor(max_workers=self.concurrency)
            self._save_incremental_profile(profile)
            future = self._incremental_executor.submit(self._generate, profile, task)
            self._incremental_futures[future] = (profile, task.image_key)
            submitted = future
            task_count = len(tasks)
        if submitted is not None:
            self.logger.info(
                "image task submitted | profile=%s | key=%s | tasks=%d",
                profile,
                task.image_key,
                task_count,
            )
            submitted.add_done_callback(
                lambda future, saved_profile=profile: self._incremental_task_done(future, saved_profile)
            )

    def _incremental_task_done(self, future: Future[None], profile: str) -> None:
        to_cancel: list[Future[None]] = []
        task_key = "unknown"
        status = "cancelled" if future.cancelled() else "unknown"
        completed = 0
        total = 0
        with self._incremental_lock:
            if not future.cancelled():
                error = future.exception()
                if isinstance(error, (FatalImageApiError, FatalLocalImageError)):
                    self._incremental_fatal_errors.append(error)
                    to_cancel = [other for other in self._incremental_futures if other is not future]
            saved = self._incremental_futures.pop(future, None)
            if saved is not None:
                task_key = saved[1]
            tasks = self._incremental_tasks.get(profile, {})
            task = tasks.get(task_key)
            if task is not None:
                if future.cancelled():
                    task.status, task.error = "aborted", "cancelled after a fatal image generation error"
                status = task.status
            completed = sum(
                item.status in ("generated", "reused") for item in tasks.values()
            )
            total = len(tasks)
            self._save_incremental_profile(profile)
        # Future.cancel() invokes callbacks, so it must run outside the manifest lock.
        for other in to_cancel:
            other.cancel()
        log = self.logger.info if status in ("generated", "reused") else self.logger.warning
        log(
            "image task finished | profile=%s | key=%s | status=%s | completed=%d/%d",
            profile,
            task_key,
            status,
            completed,
            total,
        )

    def submit_items(self, profile: str, items: list[dict[str, Any]]) -> None:
        for task in _tasks_from_rows(
            items,
            default_profile=profile,
            owner=f"incremental benchmark/{profile}",
        ):
            self._submit_incremental_task(task)

    def submit_benchmark_root(self, benchmark_root: Path) -> None:
        for tasks in discover_tasks(benchmark_root).values():
            for task in tasks:
                self._submit_incremental_task(task)

    def finish_incremental(self, *, raise_fatal: bool = True) -> dict[str, Any]:
        with self._incremental_lock:
            futures = dict(self._incremental_futures)
            executor = self._incremental_executor
            fatal = self._incremental_fatal_errors[0] if self._incremental_fatal_errors else None
            self._incremental_fatal_errors.clear()
        try:
            for future in as_completed(futures):
                try:
                    future.result()
                except CancelledError:
                    continue
                except (FatalImageApiError, FatalLocalImageError) as exc:
                    fatal = fatal or exc
                    for other in futures:
                        if other is not future:
                            other.cancel()
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            self._incremental_executor = None
            self.close()
        with self._incremental_lock:
            fatal = fatal or next(iter(self._incremental_fatal_errors), None)
            self._incremental_fatal_errors.clear()
            for profile in self._incremental_tasks:
                self._save_incremental_profile(profile)
            summary = {
                "enabled": True,
                "profiles": {
                    profile: {
                        "manifest": str(self._manifest(profile)),
                        "task_count": len(tasks),
                        "completed": sum(
                            task.status in ("generated", "reused") for task in tasks.values()
                        ),
                        "failed": sum(
                            task.status in ("failed", "moderated", "aborted")
                            for task in tasks.values()
                        ),
                    }
                    for profile, tasks in self._incremental_tasks.items()
                },
            }
        if fatal is not None and raise_fatal:
            raise fatal
        return summary

    def _restore(self, profile: str, tasks: list[ImageTask], *, force: bool) -> None:
        manifest_path = self._manifest(profile)
        if force or not manifest_path.is_file():
            return
        previous = load_json(manifest_path)
        if not self._manifest_matches(previous):
            return
        old_tasks = {
            str(row.get("image_key")): row
            for row in previous.get("tasks", [])
            if isinstance(row, dict) and row.get("image_key")
        }
        expected = _expected_size(self.client.config.size)
        for task in tasks:
            old = old_tasks.get(task.image_key)
            if not old or old.get("status") not in ("generated", "reused"):
                continue
            path = self._profile_root(profile) / str(old.get("image_path", ""))
            if not path.is_file():
                continue
            data = path.read_bytes()
            image_format, width, height = probe_image(data)
            if (width, height) != expected or sha256_bytes(data) != old.get("image_sha256"):
                continue
            if stable_id(task.prompt, length=64) != stable_id(str(old.get("prompt", "")), length=64):
                continue
            task.status = "reused"
            task.attempts = int(old.get("attempts", 0))
            task.image_path = str(path.relative_to(self._profile_root(profile)))
            task.image_sha256 = sha256_bytes(data)
            task.image_model = str(old.get("image_model", self.client.config.model))
            task.image_format, task.width, task.height = image_format, width, height
            task.request_ids = [str(value) for value in old.get("request_ids", [])]
            task.seed = int(old["seed"]) if old.get("seed") is not None else None

    def _generate(self, profile: str, task: ImageTask) -> None:
        task.attempts += 1
        try:
            client = self.client.clone()
            if isinstance(client, LocalQwenImageClient):
                task.seed = client.config.seed
            data = client.generate(task.prompt)
            image_format, width, height = probe_image(data)
            if (width, height) != _expected_size(client.config.size):
                raise ValueError(f"expected {client.config.size}, received {width}x{height}")
            suffix = "jpg" if image_format == "jpeg" else image_format
            image_dir = self._profile_root(profile) / "files"
            image_dir.mkdir(parents=True, exist_ok=True)
            target = image_dir / f"{task.image_key}.{suffix}"
            descriptor, temporary = tempfile.mkstemp(prefix=task.image_key + ".", dir=image_dir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            task.status = "generated"
            task.image_path = str(target.relative_to(self._profile_root(profile)))
            task.image_sha256 = sha256_bytes(data)
            task.image_model = client.config.model
            task.image_format, task.width, task.height = image_format, width, height
            task.request_ids = list(client.request_ids)
            task.error = ""
        except ImageModerationError as exc:
            task.status, task.error = "moderated", str(exc)
        except (FatalImageApiError, FatalLocalImageError) as exc:
            task.status, task.error = "aborted", str(exc)
            raise
        except Exception as exc:
            task.status, task.error = "failed", str(exc)
            self.logger.exception("image task failed key=%s", task.image_key)

    def run(self, *, force: bool = False, max_items: int = 0) -> dict[str, Any]:
        try:
            return self._run(force=force, max_items=max_items)
        finally:
            self.close()

    def _run(self, *, force: bool, max_items: int) -> dict[str, Any]:
        discovered = discover_tasks(self.config.run_root / "benchmark")
        summary: dict[str, Any] = {"profiles": {}}
        for profile, tasks in discovered.items():
            if max_items > 0:
                tasks = tasks[:max_items]
            self._restore(profile, tasks, force=force)
            self._save(profile, tasks)
            pending = [task for task in tasks if task.status != "reused"]
            executor = ThreadPoolExecutor(max_workers=self.concurrency)
            futures: dict[Future[None], ImageTask] = {
                executor.submit(self._generate, profile, task): task for task in pending
            }
            try:
                for future in as_completed(futures):
                    try:
                        future.result()
                    except (FatalImageApiError, FatalLocalImageError):
                        for other, task in futures.items():
                            if other.cancel():
                                task.status, task.error = "aborted", "cancelled after a fatal image generation error"
                        self._save(profile, tasks)
                        raise
                    self._save(profile, tasks)
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
                self._save(profile, tasks)
            completed = sum(task.status in ("generated", "reused") for task in tasks)
            failed = len(tasks) - completed
            self._save(profile, tasks)
            summary["profiles"][profile] = {
                "manifest": str(self._manifest(profile)),
                "completed": completed,
                "failed": failed,
            }
        return summary
