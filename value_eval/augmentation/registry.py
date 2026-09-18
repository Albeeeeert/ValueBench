from __future__ import annotations

from importlib import import_module
from typing import Iterable

from .base import AugmentationMethod


METHODS = {"figstep": "value_eval.augmentation.methods.figstep.attack:FigStep"}
METHODS.update({name: f"value_eval.augmentation.methods.{name}.attack:Method" for name in (
    "qr", "camo", "mml_wr", "mml_mirror", "mml_rotate", "himrd", "cs_dj",
    "visual_roleplay", "si", "viscra",
)})


def validate_method_names(names: Iterable[str]) -> None:
    unknown = set(names) - set(METHODS)
    if unknown:
        raise ValueError(f"unknown augmentation method(s): {', '.join(sorted(unknown))}")


def load_method(name: str, config=None) -> AugmentationMethod:
    validate_method_names([name])
    module, class_name = METHODS[name].split(":")
    method = getattr(import_module(module), class_name)()
    return method.bind(config) if config is not None else method
