from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .config import PACKAGE_ROOT, PipelineConfig, load_env_file
from .pipeline import Pipeline, inspect_pipeline
from .value_to_scenario import PreparationOptions


DEFAULT_CONFIG = PACKAGE_ROOT / "configs" / "prepared_scenarios.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="value-eval",
        description="Build Plan-Game single-author benchmarks, generate images, and collect raw model responses.",
    )
    parser.add_argument(
        "command",
        choices=(
            "validate-excel",
            "prepare-scenarios",
            "validate-scenarios",
            "validate-input",
            "generate-benchmark",
            "generate-images",
            "collect-responses",
            "run-all",
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, help="dotenv or PowerShell env file; overrides run.env_file")
    parser.add_argument("--profile", action="append", choices=("hh", "bh"))
    parser.add_argument(
        "--style",
        choices=("both", "awareness", "instruction"),
        help="Override generation.styles for benchmark generation.",
    )
    parser.add_argument(
        "--response-mode",
        choices=("image_text", "image_mcq", "description_text", "description_mcq"),
        help="Override response.mode for response collection without editing the YAML file.",
    )
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument(
        "--variants-per-scenario",
        type=int,
        default=None,
        help="Override the number of independent generation variants per scenario; 0 keeps element-once mode.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-fallbacks", action="store_true")
    parser.add_argument("--no-publish", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_items is not None and args.max_items < 0:
        parser.error("--max-items must be zero or positive")
    if args.variants_per_scenario is not None and args.variants_per_scenario < 0:
        parser.error("--variants-per-scenario must be zero or positive")
    config = PipelineConfig.load(args.config)
    if args.variants_per_scenario is not None:
        config = replace(config, variants_per_scenario=args.variants_per_scenario)
    if args.profile:
        config = replace(config, profiles=tuple(dict.fromkeys(args.profile)))
    if args.style:
        styles = (
            ("awareness", "instruction")
            if args.style == "both"
            else (args.style,)
        )
        config = replace(
            config,
            styles=styles,
            share_image_across_styles=args.style == "both",
        )
    if args.response_mode:
        config = replace(config, response={**config.response, "mode": args.response_mode})
    if (
        args.max_items is not None
        and args.command in ("generate-benchmark", "run-all")
        and len(config.styles) == 2
        and args.max_items % 2
    ):
        parser.error("--max-items must be even in both mode")
    if args.max_items is not None and args.command in ("generate-benchmark", "run-all"):
        config = replace(config, max_items_per_profile=args.max_items)
    env_file = args.env_file.resolve() if args.env_file else config.env_file
    load_env_file(env_file)

    read_only = args.command.startswith("validate-") or args.dry_run
    logger = logging.getLogger("value_eval.preflight") if read_only else None
    pipeline = Pipeline(config, logger=logger)
    if args.command == "validate-excel":
        result = pipeline.inspect_excel()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "prepare-scenarios":
        result = pipeline.prepare_scenarios(PreparationOptions(
            dry_run=args.dry_run,
            force=args.force,
            publish=not args.no_publish,
            retry_fallbacks=args.retry_fallbacks,
        ))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-scenarios":
        result = pipeline.validate_scenarios()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-input" or args.dry_run:
        result = inspect_pipeline(config)
        result["command"] = args.command
        result["dry_run"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["model_capability_ready"] else 1

    if args.command == "generate-benchmark":
        result = pipeline.run_benchmark(force=args.force)
    elif args.command == "generate-images":
        result = pipeline.run_images(force=args.force, max_items=args.max_items or 0)
    elif args.command == "collect-responses":
        result = pipeline.run_responses(force=args.force, max_items=args.max_items or 0)
    else:
        if args.no_publish and config.scenario_source.mode == "excel":
            parser.error("run-all cannot use --no-publish in excel mode")
        result = pipeline.run_all(
            force=args.force,
            retry_fallbacks=args.retry_fallbacks,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
