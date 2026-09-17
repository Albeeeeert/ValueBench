#!/usr/bin/env bash
set -Eeuo pipefail

PACKAGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
CONFIG_PATH="${VALUE_EVAL_CONFIG:-configs/excel_to_benchmark_figstep_check.yaml}"

if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  printf 'Value_eval requires Python 3.10+; set PYTHON_BIN to a compatible interpreter.\n' >&2
  exit 11
fi

cd "${PACKAGE_ROOT}"
CUDA_VISIBLE_DEVICES=0,1 "${PYTHON_BIN}" -m value_eval run-all --config "${CONFIG_PATH}" "$@"
