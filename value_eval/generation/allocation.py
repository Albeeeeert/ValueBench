from __future__ import annotations

import hashlib
import random
from collections import Counter
from dataclasses import dataclass

from .prompts import INSTRUCTION_FAMILIES


SCENE_TYPES = (
    "text_artifact",
    "people_interaction",
    "physical_scene",
    "environment_context",
)
VISUAL_EVIDENCE_MODES = ("non_text", "minimal_text", "text_supported")


@dataclass(frozen=True)
class GenerationAssignment:
    scene_type: str
    evidence_mode: str
    max_visible_text_words: int
    instruction_family: str


def stable_seed(base_seed: int, value: str) -> int:
    digest = hashlib.sha256(f"{base_seed}:{value}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def allocate_ratio_sequence(
    total: int,
    names: tuple[str, ...],
    ratios: dict[str, float],
    seed: int,
) -> list[str]:
    raw_counts = {name: total * ratios[name] for name in names}
    counts = {name: int(raw_counts[name]) for name in names}
    remaining = total - sum(counts.values())
    remainders = sorted(
        names,
        key=lambda name: (raw_counts[name] - counts[name], -names.index(name)),
        reverse=True,
    )
    for name in remainders[:remaining]:
        counts[name] += 1
    sequence = [name for name in names for _ in range(counts[name])]
    random.Random(seed).shuffle(sequence)
    return sequence


def build_assignments(
    total: int,
    *,
    random_seed: int,
    scene_ratios: dict[str, float],
    evidence_ratios: dict[str, float],
    visible_text_word_limits: dict[str, int],
) -> list[GenerationAssignment]:
    evidence = allocate_ratio_sequence(
        total,
        VISUAL_EVIDENCE_MODES,
        evidence_ratios,
        stable_seed(random_seed, "visual-evidence-allocation"),
    )
    scenes = allocate_ratio_sequence(
        total,
        SCENE_TYPES,
        scene_ratios,
        stable_seed(random_seed, "scene-allocation"),
    )
    incompatible = [
        index
        for index, (scene, mode) in enumerate(zip(scenes, evidence))
        if scene == "text_artifact" and mode == "non_text"
    ]
    compatible = [
        index
        for index, (scene, mode) in enumerate(zip(scenes, evidence))
        if scene != "text_artifact" and mode != "non_text"
    ]
    for bad_index, good_index in zip(incompatible, compatible):
        scenes[bad_index], scenes[good_index] = scenes[good_index], scenes[bad_index]
    for bad_index in [
        index
        for index, (scene, mode) in enumerate(zip(scenes, evidence))
        if scene == "text_artifact" and mode == "non_text"
    ]:
        counts = Counter(scenes)
        scenes[bad_index] = min(
            (name for name in SCENE_TYPES if name != "text_artifact"),
            key=lambda name: (counts[name], SCENE_TYPES.index(name)),
        )
    return [
        GenerationAssignment(
            scene_type=scene,
            evidence_mode=mode,
            max_visible_text_words=visible_text_word_limits[mode],
            instruction_family=INSTRUCTION_FAMILIES[index % len(INSTRUCTION_FAMILIES)],
        )
        for index, (scene, mode) in enumerate(zip(scenes, evidence))
    ]
