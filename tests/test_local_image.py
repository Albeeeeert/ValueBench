from __future__ import annotations

import logging
import os
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from PIL import Image

from value_eval.config import LocalImageConfig, PACKAGE_ROOT, PipelineConfig
from value_eval.image_generation.client import FatalImageApiError, QwenImageClient
from value_eval.image_generation.local_client import FatalLocalImageError, LocalQwenImageClient, inspect_local_image
from value_eval.image_generation.runner import ImageGenerator, probe_image
from value_eval.io_utils import atomic_write_json, load_json
from value_eval.pipeline import Pipeline, inspect_pipeline


class LocalImageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model_path = self.root / "weights"
        self.model_path.mkdir()
        atomic_write_json(self.model_path / "model_index.json", {"_class_name": "QwenImagePipeline"})
        base = PipelineConfig.load(PACKAGE_ROOT / "configs/prepared_scenarios.yaml")
        self.config = replace(
            base,
            root=self.root,
            output_root=self.root / "outputs",
            run_id="local_image_test",
            image_backend="local",
            image={"model": "api-model", "size": "2048*2048", "concurrency": 3,
                   "generate_during_benchmark": True, "api_key_env": "IMAGE_ONLY_KEY"},
            local_image={"model_path": "weights", "generate_during_benchmark": False},
        )
        self.settings = LocalImageConfig.from_mapping(self.config.local_image, self.root)
        self.pipe = Mock()
        self.pipe.side_effect = lambda **kwargs: SimpleNamespace(
            images=[Image.new("RGB", (kwargs["width"], kwargs["height"]), "blue")]
        )
        self.factory = Mock(return_value=self.pipe)
        self.torch = SimpleNamespace(
            bfloat16="bf16", float16="fp16", float32="fp32",
            inference_mode=nullcontext,
            Generator=Mock(side_effect=lambda **kwargs: SimpleNamespace(manual_seed=lambda seed: seed)),
            cuda=SimpleNamespace(
                is_available=lambda: True, device_count=lambda: 8,
                OutOfMemoryError=type("OutOfMemoryError", (RuntimeError,), {}),
                device=lambda *_: nullcontext(), empty_cache=Mock(),
            ),
        )
        self.modules = {
            "torch": self.torch,
            "diffusers": SimpleNamespace(QwenImagePipeline=SimpleNamespace(from_pretrained=self.factory)),
        }
        self.rows = [
            {"benchmark_id": f"test-{style}", "profile": "hh", "shared_image_id": "shared-1",
             "image_description": "A blue cup on a wooden table.", "question": "What color is the cup?"}
            for style in ("awareness", "instruction")
        ]
        atomic_write_json(self.config.run_root / "benchmark/hh/benchmark.json", {"items": self.rows})

    def test_config_selection_defaults_and_relative_path(self) -> None:
        self.assertEqual(self.settings.model_path, str(self.model_path))
        self.assertEqual(self.settings.size, "512*512")
        self.assertEqual(self.settings.num_inference_steps, 50)
        self.assertIs(self.config.active_image, self.config.local_image)
        self.assertFalse(self.config.generate_images_during_benchmark)
        api = replace(self.config, image_backend="api")
        self.assertIs(api.active_image, api.image)
        self.assertTrue(api.generate_images_during_benchmark)

        raw = yaml.safe_load((PACKAGE_ROOT / "configs/prepared_scenarios.yaml").read_text())
        raw.pop("image_backend")
        raw.pop("local_image")
        path = self.root / "legacy.yaml"
        path.write_text(yaml.safe_dump(raw))
        self.assertEqual(PipelineConfig.load(path).image_backend, "api")
        raw["image_backend"] = "invalid"
        path.write_text(yaml.safe_dump(raw))
        with self.assertRaisesRegex(ValueError, "image_backend"):
            PipelineConfig.load(path)
        raw.update(image_backend="local", local_image={"model_path": "weights"})
        raw["run"]["root"] = str(self.root)
        path.write_text(yaml.safe_dump(raw))
        self.assertEqual(PipelineConfig.load(path).image_backend, "local")

    def test_invalid_local_settings_fail_early(self) -> None:
        for change in (
            {"model_path": ""}, {"size": "513*512"}, {"size": "0*512"},
            {"num_inference_steps": 0}, {"concurrency": 2}, {"dtype": "int8"},
            {"cpu_offload": "yes"}, {"true_cfg_scale": float("nan")},
            {"negative_prompt": None}, {"seed": -1}, {"device": "cpu"},
            {"device_map": "unknown"}, {"device_map": "balanced", "cpu_offload": "sequential"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LocalImageConfig.from_mapping({**self.config.local_image, **change}, self.root)

    def test_client_loads_once_and_forwards_parameters(self) -> None:
        client = LocalQwenImageClient(self.settings)
        clone = client.clone()
        self.factory.assert_not_called()
        with patch.dict("sys.modules", self.modules):
            first = client.generate("first prompt")
            clone.generate("second prompt")
            clone.generate("first prompt")
            client.close()
        self.assertEqual(probe_image(first), ("png", 512, 512))
        self.factory.assert_called_once_with(str(self.model_path), torch_dtype="bf16", local_files_only=True)
        self.pipe.enable_sequential_cpu_offload.assert_called_once_with(gpu_id=0)
        self.pipe.to.assert_not_called()
        calls = self.pipe.call_args_list
        self.assertEqual(calls[0].kwargs["num_inference_steps"], 50)
        self.assertEqual(calls[0].kwargs["true_cfg_scale"], 4.0)
        self.assertEqual(calls[0].kwargs["negative_prompt"], " ")
        self.assertEqual(calls[0].kwargs["generator"], calls[2].kwargs["generator"])
        self.assertEqual([call.kwargs["generator"] for call in calls], [42, 42, 42])
        self.torch.cuda.empty_cache.assert_called_once()

    def test_device_and_other_offload_modes(self) -> None:
        for offload in ("none", "model"):
            with self.subTest(offload=offload), patch.dict("sys.modules", self.modules):
                self.pipe.reset_mock()
                client = LocalQwenImageClient(replace(self.settings, cpu_offload=offload, device="cuda:3"))
                client.generate("test")
                client.close()
                if offload == "none":
                    self.pipe.to.assert_called_once_with("cuda:3")
                else:
                    self.pipe.enable_model_cpu_offload.assert_called_once_with(gpu_id=3)

    def test_balanced_loading_uses_automatic_mapping_and_fixed_seed(self) -> None:
        settings = replace(self.settings, cpu_offload="none", device_map="balanced",
                           seed=42, negative_prompt="低分辨率，低画质。")
        with patch.dict("sys.modules", self.modules), patch.object(self.torch.cuda, "device_count", return_value=2):
            client = LocalQwenImageClient(settings)
            client.generate("first prompt")
            client.clone().generate("different prompt")
            client.close()
        self.factory.assert_called_once_with(str(self.model_path), torch_dtype="bf16",
                                             local_files_only=True, device_map="balanced")
        self.pipe.to.assert_not_called()
        self.pipe.enable_model_cpu_offload.assert_not_called()
        self.pipe.enable_sequential_cpu_offload.assert_not_called()
        self.assertEqual([call.kwargs["generator"] for call in self.pipe.call_args_list], [42, 42])
        self.assertTrue(all(call.kwargs["negative_prompt"] == settings.negative_prompt for call in self.pipe.call_args_list))
        self.assertEqual(self.torch.cuda.empty_cache.call_count, 2)

    def test_balanced_loading_accepts_any_positive_visible_gpu_count(self) -> None:
        settings = replace(self.settings, cpu_offload="none", device_map="balanced")
        for count in (1, 2, 8):
            with (self.subTest(count=count), patch.dict("sys.modules", self.modules),
                  patch.object(self.torch.cuda, "device_count", return_value=count)):
                self.factory.reset_mock()
                self.torch.cuda.empty_cache.reset_mock()
                client = LocalQwenImageClient(settings)
                client.generate("test")
                client.close()
                self.factory.assert_called_once_with(
                    str(self.model_path), torch_dtype="bf16", local_files_only=True, device_map="balanced"
                )
                self.assertEqual(self.torch.cuda.empty_cache.call_count, count)
        self.pipe.to.assert_not_called()

    def test_balanced_loading_rejects_unavailable_cuda(self) -> None:
        settings = replace(self.settings, cpu_offload="none", device_map="balanced")
        for count, available in ((0, False), (0, True), (8, False)):
            with (self.subTest(count=count, available=available), patch.dict("sys.modules", self.modules),
                  patch.object(self.torch.cuda, "device_count", return_value=count),
                  patch.object(self.torch.cuda, "is_available", return_value=available)):
                with self.assertRaisesRegex(FatalLocalImageError, "CUDA device unavailable"):
                    LocalQwenImageClient(settings).generate("test")
        self.factory.assert_not_called()

    def test_balanced_preflight_checks_visible_devices_without_loading_weights(self) -> None:
        settings = replace(self.settings, cpu_offload="none", device_map="balanced")
        for count, available in ((0, False), (1, True), (2, True), (8, True), (8, False)):
            with (
                self.subTest(count=count, available=available),
                patch.dict("sys.modules", self.modules),
                patch("value_eval.image_generation.local_client.importlib.util.find_spec", return_value=object()),
                patch.object(self.torch.cuda, "device_count", return_value=count),
                patch.object(self.torch.cuda, "is_available", return_value=available),
            ):
                status = inspect_local_image(settings)
                self.assertEqual(status["ready"], available and count > 0)
                self.assertEqual(status["required_cuda_devices"], [f"cuda:{i}" for i in range(count)])
        self.factory.assert_not_called()

    def test_runner_selects_local_without_api_key_and_reuses_images(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.dict("sys.modules", self.modules):
            runner = ImageGenerator(self.config)
            self.assertIsInstance(runner.client, LocalQwenImageClient)
            result = runner.run()
            self.assertEqual(result["profiles"]["hh"]["completed"], 1)
            self.assertEqual(self.pipe.call_count, 1)
            manifest = load_json(self.config.run_root / "images/hh/manifest.json")
            self.assertEqual(manifest["backend"], "local")
            self.assertEqual(manifest["generation_settings"]["num_inference_steps"], 50)
            self.assertEqual(len(manifest["tasks"][0]["benchmark_ids"]), 2)
            self.assertIsInstance(manifest["tasks"][0]["seed"], int)
            ImageGenerator(self.config).run()
            self.factory.assert_called_once()
            self.assertEqual(self.pipe.call_count, 1)
            # Changes to inactive API settings must not invalidate local images.
            ImageGenerator(replace(self.config, image={"size": "1024*1024"})).run()
            self.assertEqual(self.pipe.call_count, 1)

    def test_changed_generation_settings_invalidate_cache(self) -> None:
        with patch.dict("sys.modules", self.modules):
            ImageGenerator(self.config).run()
            for index, change in enumerate((
                {"num_inference_steps": 40}, {"seed": 17},
                {"true_cfg_scale": 3.0}, {"model_revision": "replaced-weights"},
            ), start=2):
                config = replace(self.config, local_image={**self.config.local_image, **change})
                ImageGenerator(config).run()
                self.assertEqual(self.pipe.call_count, index)

    def test_incremental_generation_resume_and_parameter_change(self) -> None:
        with patch.dict("sys.modules", self.modules):
            runner = ImageGenerator(self.config)
            runner.submit_items("hh", self.rows)
            runner.finish_incremental()
            self.assertEqual(self.pipe.call_count, 1)
            resumed = ImageGenerator(self.config)
            resumed.submit_items("hh", self.rows)
            resumed.finish_incremental()
            self.assertEqual(self.pipe.call_count, 1)
            changed = ImageGenerator(replace(self.config, local_image={**self.config.local_image, "seed": 99}))
            changed.submit_items("hh", self.rows)
            changed.finish_incremental()
            self.assertEqual(self.pipe.call_count, 2)

    def test_local_does_not_reuse_legacy_api_manifest(self) -> None:
        with patch.dict("sys.modules", self.modules):
            ImageGenerator(self.config).run()
            path = self.config.run_root / "images/hh/manifest.json"
            manifest = load_json(path)
            manifest.pop("generation_fingerprint")
            manifest.pop("backend")
            atomic_write_json(path, manifest)
            ImageGenerator(self.config).run()
            self.assertEqual(self.pipe.call_count, 2)

    def test_api_selection_ignores_local_settings_and_reuses_legacy_manifest(self) -> None:
        api = replace(self.config, image_backend="api", local_image={"num_inference_steps": -1})
        buffer = BytesIO()
        Image.new("RGB", (2048, 2048), "blue").save(buffer, format="PNG")
        with (
            patch.dict(os.environ, {"IMAGE_ONLY_KEY": "test"}),
            patch.object(QwenImageClient, "generate", return_value=buffer.getvalue()) as generate,
            patch.object(LocalQwenImageClient, "generate") as local_generate,
        ):
            ImageGenerator(api).run()
            path = api.run_root / "images/hh/manifest.json"
            manifest = load_json(path)
            manifest.pop("generation_fingerprint")
            manifest.pop("backend")
            atomic_write_json(path, manifest)
            ImageGenerator(api).run()
            generate.assert_called_once()
            local_generate.assert_not_called()

    def test_switching_backends_invalidates_cache_even_with_same_model_and_size(self) -> None:
        buffer = BytesIO()
        Image.new("RGB", (512, 512), "blue").save(buffer, format="PNG")
        api = replace(self.config, image_backend="api", image={**self.config.image,
                      "model": self.settings.model, "size": self.settings.size})
        with (
            patch.dict(os.environ, {"IMAGE_ONLY_KEY": "test"}),
            patch.dict("sys.modules", self.modules),
            patch.object(QwenImageClient, "generate", return_value=buffer.getvalue()) as generate,
        ):
            ImageGenerator(self.config).run()
            ImageGenerator(api).run()
            ImageGenerator(self.config).run()
            generate.assert_called_once()
            self.assertEqual(self.pipe.call_count, 2)

    def test_unavailable_cuda_device_keeps_original_error(self) -> None:
        config = replace(self.config, local_image={**self.config.local_image, "device": "cuda:99"})
        with patch.dict("sys.modules", self.modules), self.assertRaisesRegex(FatalLocalImageError, "CUDA device unavailable"):
            ImageGenerator(config).run()
        self.torch.cuda.empty_cache.assert_not_called()

    def test_loading_failure_stops_stage_and_saves_progress(self) -> None:
        self.factory.side_effect = RuntimeError("bad weights")
        with patch.dict("sys.modules", self.modules), self.assertRaisesRegex(FatalLocalImageError, "bad weights"):
            ImageGenerator(self.config).run()
        manifest = load_json(self.config.run_root / "images/hh/manifest.json")
        self.assertEqual(manifest["tasks"][0]["status"], "aborted")
        self.factory.assert_called_once()

    def test_cuda_oom_is_fatal_for_incremental_and_regular_runs(self) -> None:
        self.pipe.side_effect = self.torch.cuda.OutOfMemoryError("test OOM")
        for incremental in (False, True):
            with self.subTest(incremental=incremental), patch.dict("sys.modules", self.modules):
                runner = ImageGenerator(self.config)
                with self.assertRaisesRegex(FatalLocalImageError, "CUDA memory"):
                    if incremental:
                        runner.submit_items("hh", self.rows)
                        runner.finish_incremental()
                    else:
                        runner.run()
                manifest = load_json(self.config.run_root / "images/hh/manifest.json")
                self.assertEqual(manifest["tasks"][0]["status"], "aborted")

    def test_api_fatal_error_stops_incremental_and_regular_runs(self) -> None:
        config = replace(self.config, image_backend="api")
        for incremental in (False, True):
            with (
                self.subTest(incremental=incremental),
                patch.dict(os.environ, {"IMAGE_ONLY_KEY": "test"}),
                patch.object(QwenImageClient, "generate", side_effect=FatalImageApiError("API unavailable")),
            ):
                runner = ImageGenerator(config)
                with self.assertRaisesRegex(FatalImageApiError, "API unavailable"):
                    if incremental:
                        runner.submit_items("hh", self.rows)
                        runner.finish_incremental()
                    else:
                        runner.run()
                manifest = load_json(config.run_root / "images/hh/manifest.json")
                self.assertEqual(manifest["tasks"][0]["status"], "aborted")

    def test_preflight_only_requires_selected_backend_key(self) -> None:
        with patch("value_eval.pipeline.inspect_local_image", return_value={"ready": False}):
            local = inspect_pipeline(self.config)
        self.assertNotIn("IMAGE_ONLY_KEY", local["required_api_key_envs"])
        self.assertEqual(local["image_backend"], "local")
        api = inspect_pipeline(replace(self.config, image_backend="api"))
        self.assertIn("IMAGE_ONLY_KEY", api["required_api_key_envs"])
        self.assertIsNone(api["local_image_status"])

    def test_local_incremental_switch_is_used_by_pipeline(self) -> None:
        config = replace(self.config, local_image={**self.config.local_image, "generate_during_benchmark": True})
        with (
            patch("value_eval.pipeline.ImageGenerator") as images,
            patch("value_eval.pipeline.BenchmarkGenerator") as benchmark,
        ):
            benchmark.return_value.run.return_value = {}
            Pipeline(config, logger=logging.getLogger("test-local-image")).run_benchmark()
            self.assertIs(benchmark.call_args.kwargs["on_items_committed"], images.return_value.submit_items)
            images.return_value.finish_incremental.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
