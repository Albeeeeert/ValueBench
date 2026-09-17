from __future__ import annotations

from importlib import import_module
from typing import Iterable

from .base import AugmentationMethod


METHODS = {"figstep": "value_eval.augmentation.methods.figstep.attack:FigStep"}


def validate_method_names(names: Iterable[str]) -> None:
    unknown = set(names) - set(METHODS)
    if unknown:
        raise ValueError(f"unknown augmentation method(s): {', '.join(sorted(unknown))}")


def load_method(name: str) -> AugmentationMethod:
    validate_method_names([name])
    module, class_name = METHODS[name].split(":")
    return getattr(import_module(module), class_name)()
