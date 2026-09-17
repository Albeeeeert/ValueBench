from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import PipelineConfig


SCHEMA_VERSION = "scenario-preparation-v2"
FINGERPRINT_ALGORITHM = "python-ast-without-logging-v1"
_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}


class _SemanticCode(ast.NodeTransformer):
    """Remove standalone logging calls while retaining content-affecting logic."""

    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:  # noqa: N802
        value = node.value
        if isinstance(value, ast.Call) and _is_logger_method(value.func):
            return None
        return self.generic_visit(node)


def _is_logger_method(node: ast.AST) -> bool:
    if not isinstance(node, ast.Attribute) or node.attr not in _LOG_METHODS:
        return False
    current: ast.AST = node.value
    while isinstance(current, ast.Attribute):
        if current.attr == "logger":
            return True
        current = current.value
    return isinstance(current, ast.Name) and current.id == "logger"


def content_fingerprint(
    config: PipelineConfig,
    source_rows: list[dict[str, Any]],
) -> str:
    """为输入、提示词、模型公开参数和构建代码计算可复现指纹。"""
    source = config.scenario_source
    code_root = Path(__file__).resolve().parent
    code_files = (
        code_root / "excel_reader.py",
        code_root / "taxonomy.py",
        code_root / "taxonomy_pipeline.py",
        code_root / "scenario_splitter.py",
        code_root / "prompts.py",
        code_root / "schema.py",
        code_root / "element_builder.py",
        code_root / "element_pipeline.py",
        code_root / "validator.py",
        code_root.parent / "clients" / "openai_compat.py",
    )
    aliases = (
        source.translation_model,
        source.taxonomy_model,
        source.stage1_model,
        source.element_model,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
        "source_rows": source_rows,
        "sheet_name": source.sheet_name,
        "chunk_by": source.chunk_by,
        "chunk_size": source.chunk_size,
        "min_elements": source.min_elements,
        "fallback_on_llm_error": source.fallback_on_llm_error,
        "models": {alias: config.models[alias].public_dict() for alias in aliases},
        "code_hashes": {
            path.relative_to(code_root.parent).as_posix(): _semantic_python_hash(path)
            for path in code_files
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _semantic_python_hash(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    normalized = _SemanticCode().visit(tree)
    ast.fix_missing_locations(normalized)
    encoded = ast.dump(normalized, annotate_fields=True, include_attributes=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
