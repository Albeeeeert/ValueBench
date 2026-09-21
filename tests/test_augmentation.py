from __future__ import annotations

import contextlib
import io
import json
import logging
import shutil
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml
from PIL import Image, ImageChops, ImageDraw, ImageFont

from value_eval.augmentation.base import AugmentationSource
from value_eval.augmentation.methods.figstep.attack import FigStep
from value_eval.augmentation.runner import (
    AugmentationRunner, load_augmented_samples, load_sources, method_lock,
    publish_dataset, response_datasets, validate_generation_scope,
)
from value_eval.cli import main
from value_eval.config import PACKAGE_ROOT, PipelineConfig
from value_eval.generation.runner import BenchmarkGenerator
from value_eval.image_generation.runner import ImageGenerator
from value_eval.io_utils import atomic_write_json, load_json, sha256_file
from value_eval.pipeline import Pipeline
from value_eval.response_collection.runner import ResponseCollector, load_response_samples, target_prompt
from tests.test_pipeline import FakeChatClient, FakeImageClient


class AugmentationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = replace(
            PipelineConfig.load(PACKAGE_ROOT / "configs/prepared_scenarios.yaml"),
            output_root=self.root / "outputs", log_root=self.root / "logs", run_id="test",
            image_backend="api", augmentation={"enabled": True, "method": ["figstep"]},
        )
        for profile in ("hh", "bh"):
            rows = []
            for i in range(2):
                for style in ("awareness", "instruction"):
                    rows.append({
                        "benchmark_id": f"same-id-{i}-{style}",
                        "scenario_question_style": style, "risk_combination_type": "H-H" if profile == "hh" else "B-H",
                        "question": f"Describe three ways to organize a community library. Example {i}.",
                        "image_description": f"Books on a shelf {i}", "shared_image_id": f"pair-{i}",
                        "source_scenario_id": f"scenario-{i}", "source_element_id": "element",
                        "theme": "Community", "options": {"A": "REFERENCE_OPTION"},
                        "answer": "REFERENCE_ANSWER", "risk_audit": {"secret": "REFERENCE_AUDIT"},
                    })
            atomic_write_json(self.config.run_root / "benchmark" / profile / "benchmark.json", {"items": rows})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_augmentation(self, **kwargs):
        return AugmentationRunner(self.config).run(**kwargs)

    def test_partition_layout_provenance_and_legacy_pixels(self) -> None:
        result = self.run_augmentation()
        self.assertEqual(result["methods"]["figstep"]["sample_count"], 2)
        self.assertEqual(result["dataset"]["sample_count"], 10)
        self.assertEqual(result["dataset"]["status"], "partial")  # base images not generated
        directory = self.config.run_root / "augmentations/figstep"
        self.assertEqual([p.name for p in directory.iterdir() if p.is_dir()], ["images"])
        rows = load_augmented_samples(self.config, "figstep")
        self.assertTrue(all(r["source"]["profile"] == "hh" and r["source"]["style"] == "instruction" for r in rows))
        self.assertEqual(len(list(directory.glob("images/*.png"))), 2)
        method = FigStep()
        for row in rows:
            with Image.open(self.config.run_root / row["input"]["images"][0]["path"]) as actual:
                self.assertEqual(actual.size, (760, 760))
            self.assertEqual(row["metadata"]["layout_mode"], "auto_fit")
        # Explicit fixed-layout settings can still reproduce the original method.
        raw = yaml.safe_load(yaml.safe_dump(method.snapshot))
        raw["parameters"].update({"auto_fit": False, "image_width": 760, "image_height": 760,
                                  "wrap_width": 70, "font_path": str(method.font_path)})
        legacy_config = self.root / "legacy.yaml"
        legacy_config.write_text(yaml.safe_dump(raw))
        legacy = FigStep(legacy_config).generate(
            AugmentationSource(rows[0]["source"]["benchmark"], "benchmark/hh/benchmark.json"),
            "legacy", self.root / "legacy",
        )[0]
        text = textwrap.fill(rows[0]["source"]["benchmark"]["question"], width=70) + "\n1. \n2. \n3. "
        expected = Image.new("RGB", (760, 760), "#FFFFFF")
        ImageDraw.Draw(expected).text((20, 10), text, spacing=11, font=ImageFont.truetype(str(method.font_path), 18), fill="#000000")
        with Image.open(legacy.image_paths[0]) as actual:
            self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_auto_fit_preserves_long_text_and_numbers(self) -> None:
        method = FigStep()
        question = " ".join(["Readers borrow books and share ideas."] * 80)
        source = AugmentationSource({"benchmark_id": "long", "question": question}, "benchmark/hh/benchmark.json")
        result = method.generate(source, "long", self.root / "auto-long")[0]
        meta = result.metadata
        self.assertGreaterEqual(meta["font_size"], 10)
        self.assertLess(meta["font_size"], 18)
        self.assertLess(meta["line_spacing"], method.parameters["spacing"])
        suffix = "\n1. \n2. \n3. "
        self.assertTrue(meta["rendered_text"].endswith(suffix))
        self.assertEqual("".join(meta["rendered_text"][:-len(suffix)].split()), "".join(question.split()))
        with Image.open(result.image_paths[0]) as img:
            self.assertEqual(img.size, (760, 760))
            ink = ImageChops.difference(img, Image.new("RGB", img.size, "white")).getbbox()
            self.assertGreaterEqual(ink[0], 20)
            self.assertGreaterEqual(ink[1], 10)
            self.assertLessEqual(ink[2], 740)
            self.assertLessEqual(ink[3], 750)
        # The next larger size must not fit: the chosen size is maximal.
        raw = yaml.safe_load(yaml.safe_dump(method.snapshot))
        raw["parameters"].update({"font_path": str(method.font_path), "min_font_size": meta["font_size"] + 1})
        path = self.root / "larger.yaml"
        path.write_text(yaml.safe_dump(raw))
        with self.assertRaisesRegex(ValueError, "overflows"):
            FigStep(path).generate(source, "larger", self.root / "larger")

    def test_auto_fit_uses_pixel_width_and_preserves_paragraphs(self) -> None:
        method = FigStep()
        question = "W" * 150 + "\n\n" + "i" * 300
        result = method.generate(
            AugmentationSource({"benchmark_id": "wide", "question": question}, "benchmark/hh/benchmark.json"),
            "wide", self.root / "auto-wide",
        )[0]
        text = result.metadata["rendered_text"][:-len("\n1. \n2. \n3. ")]
        self.assertEqual("".join(text.split()), "".join(question.split()))
        self.assertIn("\n\n", text)
        font = ImageFont.truetype(str(method.font_path), result.metadata["font_size"])
        for line in text.split("\n"):
            self.assertLessEqual(font.getlength(line), 720)
        wide_lines = [line for line in text.split("\n") if line.startswith("W")]
        narrow_lines = [line for line in text.split("\n") if line.startswith("i")]
        self.assertLess(len(wide_lines[0]), len(narrow_lines[0]))
        self.assertFalse(result.metadata["font_size_reduced"])

    def test_resume_corruption_and_changed_source(self) -> None:
        self.run_augmentation()
        rows = load_augmented_samples(self.config, "figstep")
        image = self.config.run_root / rows[0]["input"]["images"][0]["path"]
        mtime = image.stat().st_mtime_ns
        result = self.run_augmentation()
        self.assertEqual(result["methods"]["figstep"]["counts"], {"generated": 0, "resumed": 2, "failed": 0})
        self.assertEqual(image.stat().st_mtime_ns, mtime)
        image.write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            load_augmented_samples(self.config, "figstep")
        self.assertEqual(self.run_augmentation()["methods"]["figstep"]["counts"]["generated"], 1)
        source_path = self.config.run_root / "benchmark/hh/benchmark.json"
        payload = load_json(source_path)
        payload["items"][1]["question"] += " Include a timetable."
        atomic_write_json(source_path, payload)
        with self.assertRaisesRegex(ValueError, "stale"):
            load_augmented_samples(self.config, "figstep")
        self.assertEqual(self.run_augmentation()["methods"]["figstep"]["counts"]["generated"], 1)
        self.assertEqual(self.run_augmentation(force=True)["methods"]["figstep"]["counts"]["generated"], 2)

    def test_partial_failure_and_repair(self) -> None:
        first = self.run_augmentation(max_items=1)
        self.assertEqual(first["methods"]["figstep"]["status"], "partial")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            load_augmented_samples(self.config, "figstep")
        original = FigStep.generate

        def fail_second(method, source, sample_id, output_dir):
            if source.source_id.startswith("same-id-1"):
                raise ValueError("injected render failure")
            return original(method, source, sample_id, output_dir)

        with patch.object(FigStep, "generate", fail_second):
            with self.assertRaisesRegex(RuntimeError, "failed samples"):
                self.run_augmentation()
        manifest = load_json(self.config.run_root / "augmentations/figstep/manifest.json")
        self.assertEqual(manifest["status"], "failed")
        repaired = self.run_augmentation()["methods"]["figstep"]
        self.assertEqual(repaired["counts"], {"generated": 1, "resumed": 1, "failed": 0})
        self.assertEqual(repaired["status"], "completed")

    def test_method_parameters_invalidate_cache_and_concurrency_does_not(self) -> None:
        self.run_augmentation()
        original = FigStep()
        raw = yaml.safe_load(yaml.safe_dump(original.snapshot))
        raw["parameters"]["font_path"] = str(original.font_path)
        path = self.root / "method.yaml"
        # Keep path representation constant while changing runtime vs rendering settings.
        path.write_text(yaml.safe_dump(raw))
        first = FigStep(path)
        raw["runtime"]["concurrency"] = 1
        path.write_text(yaml.safe_dump(raw))
        self.assertEqual(first.fingerprint, FigStep(path).fingerprint)
        raw["parameters"]["font_size"] = 20
        path.write_text(yaml.safe_dump(raw))
        changed = FigStep(path)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        with patch("value_eval.augmentation.runner.load_method", return_value=changed):
            with self.assertRaisesRegex(ValueError, "stale"):
                load_augmented_samples(self.config, "figstep")
            self.assertEqual(self.run_augmentation()["methods"]["figstep"]["counts"]["generated"], 2)

    def test_missing_scope_errors_and_disabled_methods(self) -> None:
        for change in ({"profiles": ("bh",)}, {"styles": ("awareness",)}):
            bad = replace(self.config, **change)
            with self.assertRaisesRegex(ValueError, "HH-instruction"):
                validate_generation_scope(bad)
            pipeline = Pipeline(bad, logger=logging.getLogger("test"))
            with patch.object(pipeline, "run_benchmark") as benchmark:
                with self.assertRaisesRegex(ValueError, "HH-instruction"):
                    pipeline.run_all()
                benchmark.assert_not_called()
        validate_generation_scope(self.config)  # all four categories are allowed
        self.run_augmentation()
        disabled = replace(self.config, augmentation={"enabled": False, "method": ["figstep"]})
        self.assertEqual(response_datasets(disabled), ("base",))
        self.assertEqual(len(load_response_samples(disabled)), 8)
        self.assertEqual(set(publish_dataset(disabled)["subsets"]), {"base"})
        with self.assertRaisesRegex(ValueError, "not enabled"):
            AugmentationRunner(disabled).run(methods=["figstep"])
        path = self.config.run_root / "benchmark/hh/benchmark.json"
        atomic_write_json(path, {"items": [r for r in load_json(path)["items"] if r["scenario_question_style"] == "awareness"]})
        with self.assertRaisesRegex(ValueError, "no matching"):
            self.run_augmentation()

    def test_responses_are_separate_and_contain_only_method_input(self) -> None:
        ImageGenerator(self.config, client=FakeImageClient()).run()
        self.assertEqual(self.run_augmentation()["dataset"]["status"], "completed")
        target = FakeChatClient(self.config.models[self.config.target_model])
        samples = load_response_samples(self.config)
        self.assertEqual(len(samples), 10)
        for sample in samples:
            if sample.dataset == "figstep":
                self.assertEqual(target_prompt(sample, "image_text"), FigStep().prompt)
                self.assertNotIn("REFERENCE", target_prompt(sample, "image_text"))
                self.assertNotIn("community library", target_prompt(sample, "image_text"))
        collector = ResponseCollector(self.config, target=target)
        result = collector.run()
        self.assertEqual(result["counts"]["completed"], 10)
        self.assertEqual(collector.run()["counts"]["resumed"], 10)
        self.assertEqual(len(list((self.config.run_root / "responses").rglob("figstep.jsonl"))), 1)
        for mode in ("image_mcq", "description_text", "description_mcq"):
            with self.assertRaisesRegex(ValueError, "does not support"):
                response_datasets(replace(self.config, response={**self.config.response, "mode": mode}))
        # ImageGenerator records reused status on restart; dataset must remain ready.
        ImageGenerator(self.config, client=FakeImageClient()).run()
        self.assertEqual(publish_dataset(self.config)["status"], "completed")

    def test_cli_uses_actual_benchmark_without_generation_or_gpu_checks(self) -> None:
        raw = yaml.safe_load((PACKAGE_ROOT / "configs/excel_to_benchmark.yaml").read_text())
        raw["run"].update({"root": str(PACKAGE_ROOT), "id": "test", "output_root": str(self.config.output_root), "log_root": str(self.root / "logs"), "env_file": None})
        raw["augmentation"] = {"enabled": True, "method": ["figstep"]}
        # Current YAML profiles need not reproduce the historical benchmark scope.
        raw["generation"]["profiles"] = ["bh"]
        path = self.root / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        with patch("value_eval.pipeline.inspect_local_image", side_effect=AssertionError("GPU preflight must not run")):
            with patch("value_eval.pipeline.BenchmarkGenerator", side_effect=AssertionError("must not regenerate benchmark")):
                for flags in (["--dry-run"], []):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main(["generate-augmentations", "--config", str(path), "--method", "figstep", *flags]), 0)
                    self.assertIn("figstep", output.getvalue())

    def test_invalid_config_and_assets_and_overflow(self) -> None:
        raw = yaml.safe_load((PACKAGE_ROOT / "configs/prepared_scenarios.yaml").read_text())
        path = self.root / "config.yaml"
        for value in ({"enabled": True, "method": "figstep"}, {"enabled": True, "method": ["typo"]}, {"enabled": "false"}, {"methods": {"figstep": True}}):
            raw["augmentation"] = value
            path.write_text(yaml.safe_dump(raw))
            with self.assertRaises(ValueError):
                PipelineConfig.load(path)
        method = FigStep()
        source = AugmentationSource({"benchmark_id": "long", "question": "a long instruction " * 1000}, "benchmark/hh/benchmark.json")
        with self.assertRaisesRegex(ValueError, "overflows"):
            method.generate(source, "long", self.root / "overflow")
        self.assertFalse((self.root / "overflow/images/long.png").exists())
        copied = self.root / "copied-augmentation"
        shutil.copytree(PACKAGE_ROOT / "value_eval/augmentation", copied)
        copied_config = copied / "methods/figstep/config.yaml"
        portable = FigStep(copied_config)
        self.assertEqual(portable.font_path, copied / "assets/fonts/ARIAL.TTF")
        raw_method = yaml.safe_load(copied_config.read_text())
        raw_method["parameters"]["font_path"] = "missing-font.ttf"
        copied_config.write_text(yaml.safe_dump(raw_method))
        with self.assertRaises(OSError):
            FigStep(copied_config)
        with method_lock(self.root / "lock"):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with method_lock(self.root / "lock"):
                    pass

    def test_original_image_dependency_and_publication_integrity(self) -> None:
        with self.assertRaisesRegex(ValueError, "original image"):
            load_sources(self.config.run_root, require_images=True)
        ImageGenerator(self.config, client=FakeImageClient()).run()
        ImageGenerator(self.config, client=FakeImageClient()).run()
        sources = load_sources(self.config.run_root, require_images=True)
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(source.image_path.is_file() for source in sources))
        self.run_augmentation()
        path = self.config.run_root / "augmentations/figstep/samples.jsonl"
        path.write_text("{}\n")
        with self.assertRaisesRegex(ValueError, "samples.jsonl"):
            load_augmented_samples(self.config, "figstep")
        self.assertEqual(self.run_augmentation()["methods"]["figstep"]["counts"]["resumed"], 2)
        self.assertEqual(len(load_augmented_samples(self.config, "figstep")), 2)

    def test_single_instruction_run_and_empty_list(self) -> None:
        config = replace(self.config, run_id="single", profiles=("hh",), styles=("instruction",), share_image_across_styles=False)
        source = load_json(self.config.run_root / "benchmark/hh/benchmark.json")
        source["items"] = [row for row in source["items"] if row["scenario_question_style"] == "instruction"]
        atomic_write_json(config.run_root / "benchmark/hh/benchmark.json", source)
        validate_generation_scope(config)
        self.assertEqual(config.execution_run_id, "single--instruction")
        self.assertEqual(AugmentationRunner(config).run()["methods"]["figstep"]["sample_count"], 2)
        empty = replace(config, augmentation={"enabled": True, "method": []})
        self.assertEqual(response_datasets(empty), ("base",))
        with self.assertRaisesRegex(ValueError, "no augmentation methods"):
            AugmentationRunner(empty).run()

    def test_offline_full_pipeline_and_resume(self) -> None:
        config = replace(self.config, run_id="full", response={**self.config.response, "enabled": False})
        real_generator = BenchmarkGenerator

        def generator(*args, **kwargs):
            return real_generator(*args, planner=FakeChatClient(config.models[config.planner_model]), author=FakeChatClient(config.models[config.author_model]), **kwargs)

        real_images = ImageGenerator

        def images(*args, **kwargs):
            return real_images(*args, client=FakeImageClient(), **kwargs)

        with patch("value_eval.pipeline.BenchmarkGenerator", side_effect=generator), patch("value_eval.pipeline.ImageGenerator", side_effect=images):
            pipeline = Pipeline(config, logger=logging.getLogger("test"))
            result = pipeline.run_all()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["stages"]["dataset"]["sample_count"], 10)
            self.assertEqual(result["stages"]["responses"]["status"], "skipped")
            resumed = pipeline.run_all()
            self.assertEqual(resumed["stages"]["augmentations"]["figstep"]["counts"]["resumed"], 2)


if __name__ == "__main__":
    unittest.main()
