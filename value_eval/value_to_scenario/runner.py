from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..io_utils import load_json_if_exists, sha256_file, utc_now
from .element_pipeline import build_elements
from .excel_reader import read_and_validate_workbook
from .fingerprint import content_fingerprint
from .publisher import publish_directory
from .scenario_splitter import inspect_source_numbering, split_and_write_scenarios
from .taxonomy_pipeline import prepare_taxonomy
from .validator import validate_and_manifest, write_report


@dataclass(frozen=True)
class PreparationOptions:
    dry_run: bool = False
    force: bool = False
    publish: bool = True
    retry_fallbacks: bool = False


class ScenarioPreparationRunner:
    """把中文价值观工作簿构建成 Plan-Game 可直接消费的场景数据集。"""

    def __init__(self, config: PipelineConfig, logger: logging.Logger) -> None:
        self.config = config
        self.source = config.scenario_source
        self.logger = logger

    def inspect_excel(self) -> dict[str, Any]:
        rows = read_and_validate_workbook(self.source.input_xlsx, self.source.sheet_name)
        scenario_count, anomalies = inspect_source_numbering(rows)
        return {
            "schema_version": "scenario-excel-validation-v1",
            "valid": True,
            "input_xlsx": self._portable_path(self.source.input_xlsx),
            "sheet_name": self.source.sheet_name,
            "workbook_rows": len(rows),
            "expected_scenarios": scenario_count,
            "source_numbering_anomalies": anomalies,
            "network_requests_made": False,
        }

    def run(self, options: PreparationOptions) -> dict[str, Any]:
        if self.source.mode != "excel":
            raise ValueError("prepare-scenarios requires scenario_source.mode: excel")
        rows = read_and_validate_workbook(self.source.input_xlsx, self.source.sheet_name)
        scenario_count, _ = inspect_source_numbering(rows)
        fingerprint = content_fingerprint(self.config, rows)
        if options.dry_run:
            report = self.inspect_excel()
            report.update({"fingerprint": fingerprint, "published": False})
            return report

        if options.publish and not options.force:
            cached = self._published_cache(fingerprint, scenario_count, options.retry_fallbacks)
            if cached is not None:
                self.logger.info("scenario preparation reused | scenarios=%d", scenario_count)
                return cached

        stage_root = self.config.run_root / "scenario_preparation"
        build_root = stage_root / "work" / fingerprint
        taxonomy_root = build_root / "taxonomy"
        output_dir = build_root / "scenario_elements"
        taxonomy = prepare_taxonomy(
            cfg=self.config,
            source_rows=rows,
            taxonomy_root=taxonomy_root,
            logger=self.logger,
            force=options.force,
        )
        split = split_and_write_scenarios(
            translated_rows=taxonomy.translated_rows,
            extracted_records=taxonomy.records,
            taxonomy_root=taxonomy_root,
        )
        if len(split.records) != scenario_count:
            raise ValueError(
                f"scenario count changed after translation: {scenario_count} -> {len(split.records)}"
            )
        element_status = build_elements(
            cfg=self.config,
            records=split.records,
            output_dir=output_dir,
            cache_dir=build_root / "element_cache",
            logger=self.logger,
            force=options.force,
            retry_fallbacks=options.retry_fallbacks,
        )
        report = validate_and_manifest(
            output_dir=output_dir,
            expected_records=split.records,
            fingerprint=fingerprint,
            anomalies=split.anomalies,
            element_status=element_status,
            min_elements=self.source.min_elements,
        )
        report.update({
            "created_at": utc_now(),
            "input_xlsx": self._portable_path(self.source.input_xlsx),
            "workbook_rows": len(rows),
            "dimension_count": split.dimension_count,
            "taxonomy_source": taxonomy.source,
            "published": False,
        })
        write_report(report, build_root / "manifest.json", build_root / "report.md")
        if not report["valid"]:
            raise ValueError(f"scenario build validation failed: {report['errors']}")

        if options.publish:
            archived = publish_directory(
                source=output_dir,
                target=self.config.prepared_scenario_dir,
                archive_root=stage_root / "archive",
                fingerprint=fingerprint,
            )
            report["published"] = True
            report["archived_previous_dataset"] = bool(archived)
            write_report(
                report,
                self.config.prepared_scenario_manifest,
                stage_root / "report.md",
            )
        return report

    def _published_cache(
        self, fingerprint: str, scenario_count: int, retry_fallbacks: bool
    ) -> dict[str, Any] | None:
        """Keep an unchanged published manifest stable for benchmark checkpoints."""
        report = load_json_if_exists(self.config.prepared_scenario_manifest, None)
        if not isinstance(report, dict) or (
            report.get("fingerprint") != fingerprint
            or report.get("valid") is not True
            or report.get("published") is not True
        ):
            return None
        entries = report.get("entries")
        if (
            not isinstance(entries, list)
            or len(entries) != scenario_count
            or not all(isinstance(entry, dict) for entry in entries)
        ):
            return None
        if retry_fallbacks and any(
            entry.get("generation_method") == "heuristic_fallback" for entry in entries
        ):
            return None
        expected = {
            str(entry.get("canonical_relative_path", "")): entry.get("sha256")
            for entry in entries
        }
        actual = {
            path.name: sha256_file(path)
            for path in self.config.prepared_scenario_dir.glob("*.json")
            if path.is_file()
        }
        if len(expected) != scenario_count or actual != expected:
            return None
        return report

    def _portable_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.config.root).as_posix()
        except ValueError:
            return f"external/{path.name}"
