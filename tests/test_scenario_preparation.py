from __future__ import annotations

import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from openpyxl import Workbook

from value_eval.config import PACKAGE_ROOT, PipelineConfig
from value_eval.value_to_scenario.excel_reader import read_and_validate_workbook
from value_eval.value_to_scenario.runner import ScenarioPreparationRunner


class ScenarioPreparationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name)
        base = PipelineConfig.load(PACKAGE_ROOT / "configs" / "excel_to_benchmark.yaml")
        source = replace(
            base.scenario_source,
            input_xlsx=PACKAGE_ROOT / "inputs" / "excel" / "examples" / "value_first_2_rows.xlsx",
        )
        self.config = replace(
            base,
            output_root=self.temp_root / "outputs",
            log_root=self.temp_root / "logs",
            run_id="scenario_test",
            scenario_source=source,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_excel_validation_reports_expected_scenarios(self) -> None:
        report = ScenarioPreparationRunner(self.config, logging.getLogger(__name__)).inspect_excel()
        self.assertTrue(report["valid"])
        self.assertEqual(report["workbook_rows"], 2)
        self.assertEqual(report["expected_scenarios"], 14)

    def test_excel_rejects_missing_header_and_empty_required_cell(self) -> None:
        headers = ["一级分类", "二级分类", "三级分类", "四级分类", "错误表头"]
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "价值观目录及评判标准"
        sheet.append(headers)
        sheet.append(["尊严", "维度", "子维度", "边界", "1. 场景"])
        invalid_header = self.temp_root / "invalid_header.xlsx"
        workbook.save(invalid_header)
        with self.assertRaisesRegex(ValueError, "缺少必要表头"):
            read_and_validate_workbook(invalid_header, sheet.title)

        sheet.cell(row=1, column=5, value="场景描述（评判标准）")
        sheet.cell(row=2, column=3, value="")
        empty_cell = self.temp_root / "empty_cell.xlsx"
        workbook.save(empty_cell)
        workbook.close()
        with self.assertRaisesRegex(ValueError, "empty required fields"):
            read_and_validate_workbook(empty_cell, sheet.title)

if __name__ == "__main__":
    unittest.main()
