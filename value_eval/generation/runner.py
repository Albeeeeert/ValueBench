from __future__ import annotations

import logging
import random
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from ..clients.openai_compat import FatalModelError, OpenAICompatibleClient
from ..config import PipelineConfig
from ..io_utils import atomic_write_json, extract_json_object, load_json, sha256_file, stable_id, utc_now
from ..schemas import BenchmarkItem, ScenarioElement
from .allocation import GenerationAssignment, build_assignments, stable_seed
from .input_loader import expand_scenario_variants, load_scenario_elements, load_selection_pairs
from .profiles import RiskProfile, get_profile
from .prompts import AUTHOR_SYSTEM, PLANNER_SYSTEM, author_prompt, plan_prompt, system_prompt
from .validator import OPTION_ROLES, validate_draft, validate_plan


REFUSAL_TEMPLATES = ("I'm sorry, but I can't help with that request.",)


def _classify_scene_medium(image_description: str) -> str:
    text = image_description.lower()
    text_artifact_hits = len(re.findall(
        r"\b(screen|laptop|phone|tablet|dashboard|spreadsheet|table|form|document|"
        r"poster|flyer|notice|sign|label|chat|message|email|slide|queue|moderation|"
        r"checkbox|column|row|highlighted|reads|says|text|wording|headline|caption)\b",
        text,
    ))
    people_hits = len(re.findall(
        r"\b(person|people|man|woman|child|student|teacher|staff|worker|customer|"
        r"parent|volunteer|commuter|shopper|patient|visitor|standing|sitting|pointing|"
        r"holding|handing|talking|gesturing|queueing|reading|showing|blocking|guiding)\b",
        text,
    ))
    environment_hits = len(re.findall(
        r"\b(street|station|classroom|clinic|shop|store|cafe|event|booth|office|"
        r"counter|hallway|bus|train|park|market|waiting area|entrance|checkout|shelf|"
        r"display|noticeboard)\b",
        text,
    ))
    if text_artifact_hits >= max(2, people_hits + 1):
        return "text_artifact"
    if people_hits >= 2:
        return "people_interaction"
    if environment_hits >= 2:
        return "environment_context"
    return "physical_scene"


def _estimate_text_dependency_level(image_description: str) -> str:
    text = image_description.lower()
    text_hits = len(re.findall(
        r"\b(reads|says|text|wording|phrase|label|heading|headline|caption|column|row|"
        r"checkbox|message|chat|document|screen|form|poster|notice|sign)\b",
        text,
    ))
    score = text_hits + len(re.findall(r"['\"][^'\"]{3,80}['\"]", image_description))
    return "high" if score >= 8 else "medium" if score >= 3 else "low"


def _mechanism_key(item: dict[str, Any]) -> tuple[str, str, int]:
    source = item.get("generation_trace", {}).get("source", {})
    variant_index = source.get("variant_index", 1) if isinstance(source, dict) else 1
    return (
        str(item.get("source_scenario_id", "")).strip(),
        str(item.get("source_element_id", "")).strip(),
        int(variant_index),
    )


class BenchmarkGenerator:
    """HH/BH single-author generator matching the production 4000-item path."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        planner: OpenAICompatibleClient | None = None,
        author: OpenAICompatibleClient | None = None,
        logger: logging.Logger | None = None,
        on_items_committed: Callable[[str, list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.planner = planner or OpenAICompatibleClient(
            config.models[config.planner_model], logger=self.logger
        )
        self.author = author or OpenAICompatibleClient(
            config.models[config.author_model], logger=self.logger
        )
        self.on_items_committed = on_items_committed

    def _chat_json(
        self,
        client: OpenAICompatibleClient,
        system: str,
        prompt: str,
    ) -> dict[str, Any]:
        final_system = (system or "You are a precise assistant and must output valid JSON.") + (
            "\nReturn JSON only (json object)."
        )
        final_prompt = f"Return the result as a single JSON object (json).\n\n{prompt}"
        result = client.chat(
            [
                {"role": "system", "content": final_system},
                {"role": "user", "content": final_prompt},
            ],
            json_mode=True,
        )
        return extract_json_object(result.content)

    def _ids(self, unit: ScenarioElement, style: str) -> tuple[str, str]:
        suffix = "002" if len(self.config.styles) == 2 and style == "instruction" else "001"
        variant = (
            f"::V{unit.variant_index:04d}"
            if self.config.variants_per_scenario > 0
            else ""
        )
        return (
            f"B::{unit.scenario_id}::{unit.element_id}{variant}::{suffix}",
            f"{unit.scenario_id}::{unit.element_id}{variant}::Q{suffix}",
        )

    def _shared_image_id(self, unit: ScenarioElement) -> str:
        if not self.config.share_image_across_styles:
            return ""
        variant = (
            f"::V{unit.variant_index:04d}"
            if self.config.variants_per_scenario > 0
            else ""
        )
        return f"{unit.scenario_id}::{unit.element_id}{variant}::shared-image"

    def _draft_to_item(
        self,
        draft: dict[str, Any],
        unit: ScenarioElement,
        profile: RiskProfile,
        style: str,
        plan: dict[str, Any],
        assignment: GenerationAssignment,
        attempt: int,
        ordinal: int,
        timing: dict[str, float],
        elapsed_sec: float,
    ) -> BenchmarkItem:
        benchmark_id, template_id = self._ids(unit, style)
        item_rng = random.Random(stable_seed(self.config.random_seed, benchmark_id))
        item_rng.choice(REFUSAL_TEMPLATES)
        roles = list(OPTION_ROLES)
        item_rng.shuffle(roles)
        labels = ("A", "B", "C", "D")
        options = {
            label: str(draft["options_by_role"][role]).strip()
            for label, role in zip(labels, roles)
        }
        option_roles = {label: role for label, role in zip(labels, roles)}
        answer = next(label for label, role in option_roles.items() if role == "safe_helpful")
        shared_id = self._shared_image_id(unit)
        visual = dict(draft.get("visual_metrics", {}))
        program_lints = sorted(
            {
                str(item).strip()
                for item in [
                    *draft.get("program_lints", []),
                    *draft.get("risk_audit", {}).get("program_lints", []),
                    *plan.get("modality_contract", {}).get("program_lints", []),
                ]
                if str(item).strip()
            }
        )
        visual_validation = {
            "passed": True,
            "issues": [],
            "warnings": list(draft.get("validation_warnings", [])),
            "program_lints": [
                lint
                for lint in program_lints
                if lint.startswith(("visible_text_budget_exceeded", "option_length_leakage"))
            ],
            "evidence_mode": assignment.evidence_mode,
            "max_visible_text_words": assignment.max_visible_text_words,
            **visual,
        }
        tags = list(dict.fromkeys([
            *(str(tag).strip() for tag in draft["tags"] if str(tag).strip()),
            "taxonomy-conditioned",
            f"scene:{assignment.scene_type}",
            f"risk-combination:{profile.slug}",
        ]))
        actual_scene_type = _classify_scene_medium(str(draft["image_description"]))
        trace = {
            "generation_mode": "single",
            "fourth_option_mode": "authored_cautious",
            "risk_combination": profile.metadata(),
            "enable_shared_plan": True,
            "scenario_question_style": style,
            "instruction_family_hint": assignment.instruction_family,
            "visual_evidence": visual_validation,
            "scene_mix": {
                "enabled": True,
                "policy": "quota",
                "ratios": self.config.scene_mix_ratios,
                "target_scene_type": assignment.scene_type,
                "actual_scene_type": actual_scene_type,
                "scene_type_matches_target": actual_scene_type == assignment.scene_type,
                "text_dependency_level": _estimate_text_dependency_level(
                    str(draft["image_description"])
                ),
            },
            "scenario": "",
            "plan_card": plan,
            "author": self.config.author_model,
            "draft": {
                "image_description": draft["image_description"],
                "question": draft["question"],
                "options_by_role": draft["options_by_role"],
                "correct_role": "safe_helpful",
                "rationale": draft.get("rationale", ""),
                "tags": draft["tags"],
                "risk_audit": draft["risk_audit"],
            },
            "item_attempt": attempt,
            "timing_sec": timing,
            "source": {
                "input_mode": "scenario_elements",
                "scenario_element_coverage": (
                    "fixed_variants_per_scenario"
                    if self.config.variants_per_scenario > 0
                    else "all_elements_once"
                ),
                "source_scenario_id": unit.scenario_id,
                "source_scenario_path": str(unit.source_path),
                "source_taxonomy_path": str(unit.source_path),
                "category_path_en": unit.category_path,
                "scenario_title": unit.title,
                "scenario_translation_en": unit.source_text,
                "selected_element": {
                    "element_id": unit.element_id,
                    "label": unit.label,
                    "target_axis": unit.target_axis,
                    "risk_pattern": unit.risk_pattern,
                    "bias_surfaces": unit.bias_surfaces,
                    "constraints": unit.constraints,
                    "notes": unit.notes,
                },
            },
        }
        if shared_id:
            trace["shared_image_id"] = shared_id
            trace["shared_image_role"] = "primary" if style == "awareness" else "paired"
        trace["job"] = {
            "ordinal": ordinal,
            "seed": stable_seed(self.config.random_seed, benchmark_id),
            "attempts": attempt,
            "elapsed_sec": elapsed_sec,
        }
        if self.config.variants_per_scenario > 0:
            trace["source"].update({
                "variant_index": unit.variant_index,
                "variants_per_scenario": unit.variant_count,
            })
            trace["job"]["variant_index"] = unit.variant_index
        return BenchmarkItem(
            benchmark_id=benchmark_id,
            template_id=template_id,
            theme=unit.theme,
            subdimension=unit.subdimension,
            category_path_en=unit.category_path,
            source_scenario_id=unit.scenario_id,
            source_element_id=unit.element_id,
            scenario_question_style=style,
            image_description=str(draft["image_description"]).strip(),
            question=str(draft["question"]).strip(),
            options=options,
            answer=answer,
            option_roles=option_roles,
            rationale="",
            tags=tags,
            shared_image_id=shared_id,
            shared_image_role=(
                "primary" if shared_id and style == "awareness"
                else "paired" if shared_id else ""
            ),
            risk_combination_type=profile.type_id,
            modality_risk_labels=profile.expected_labels,
            risk_audit=draft["risk_audit"],
            interaction_mechanism=str(
                draft["risk_audit"].get("interaction_mechanism", profile.interaction_mechanism)
            ).strip(),
            program_lints=program_lints,
            visual_evidence_mode=assignment.evidence_mode,
            estimated_visible_text_words=int(visual.get("estimated_visible_text_words", 0)),
            quality_status="structural_pass",
            generation_trace=trace,
        )

    def _generate_plan(
        self,
        planner: OpenAICompatibleClient,
        unit: ScenarioElement,
        profile: RiskProfile,
        style: str,
        assignment: GenerationAssignment,
        recent_questions: list[str],
    ) -> dict[str, Any]:
        return validate_plan(
            self._chat_json(
                planner,
                system_prompt(PLANNER_SYSTEM, profile),
                plan_prompt(
                    unit,
                    profile,
                    style=style,
                    scene_type=assignment.scene_type,
                    evidence_mode=assignment.evidence_mode,
                    max_visible_text_words=assignment.max_visible_text_words,
                    instruction_family=assignment.instruction_family,
                    recent_questions=recent_questions,
                ),
            ),
            profile,
        )

    def _generate_job(
        self,
        unit: ScenarioElement,
        profile: RiskProfile,
        assignment: GenerationAssignment,
        recent_questions: list[str],
        ordinal: int,
    ) -> list[BenchmarkItem]:
        started_at = time.perf_counter()
        planner = self.planner.clone()
        author = self.author.clone()
        repair_feedback: list[str] | None = None
        previous_draft: dict[str, Any] | None = None
        plan_override: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(1, self.config.item_max_attempts + 1):
            try:
                primary_style = self.config.styles[0]
                attempt_started = time.perf_counter()
                if plan_override is None:
                    plan_started = time.perf_counter()
                    plan = self._generate_plan(
                        planner, unit, profile, primary_style, assignment, recent_questions
                    )
                    plan_sec = time.perf_counter() - plan_started
                else:
                    plan = plan_override
                    plan_sec = 0.0
                author_started = time.perf_counter()
                primary = validate_draft(
                    self._chat_json(
                        author,
                        system_prompt(AUTHOR_SYSTEM, profile),
                        author_prompt(
                            unit,
                            profile,
                            primary_style,
                            scene_type=assignment.scene_type,
                            evidence_mode=assignment.evidence_mode,
                            max_visible_text_words=assignment.max_visible_text_words,
                            instruction_family=assignment.instruction_family,
                            plan=plan,
                            repair_feedback=repair_feedback,
                            previous_draft=previous_draft,
                            recent_questions=recent_questions,
                        ),
                    ),
                    profile,
                    unit,
                    style=primary_style,
                    evidence_mode=assignment.evidence_mode,
                    max_visible_text_words=assignment.max_visible_text_words,
                    instruction_family=assignment.instruction_family,
                )
                author_sec = time.perf_counter() - author_started
                primary_total_sec = time.perf_counter() - attempt_started
                previous_draft = primary
                plan_override = plan
                drafts = [(primary_style, primary)]
                if len(self.config.styles) == 2:
                    paired_style = self.config.styles[1]
                    paired = validate_draft(
                        self._chat_json(
                            author,
                            system_prompt(AUTHOR_SYSTEM, profile),
                            author_prompt(
                                unit,
                                profile,
                                paired_style,
                                scene_type=assignment.scene_type,
                                evidence_mode=assignment.evidence_mode,
                                max_visible_text_words=assignment.max_visible_text_words,
                                instruction_family=assignment.instruction_family,
                                plan=None,
                                fixed_image_description=primary["image_description"],
                                recent_questions=[*recent_questions, primary["question"]],
                            ),
                        ),
                        profile,
                        unit,
                        style=paired_style,
                        evidence_mode=assignment.evidence_mode,
                        max_visible_text_words=assignment.max_visible_text_words,
                        instruction_family=assignment.instruction_family,
                        fixed_image_description=primary["image_description"],
                    )
                    drafts.append((paired_style, paired))
                normalized = [" ".join(str(draft["question"]).casefold().split()) for _, draft in drafts]
                if len(normalized) != len(set(normalized)):
                    raise ValueError("paired questions are exact duplicates")
                timing = {
                    "plan": round(plan_sec, 3),
                    "author": round(author_sec, 3),
                    "total": round(primary_total_sec, 3),
                }
                elapsed = round(time.perf_counter() - started_at, 3)
                items = [
                    self._draft_to_item(
                        draft,
                        unit,
                        profile,
                        style,
                        plan,
                        assignment,
                        attempt,
                        ordinal,
                        timing,
                        elapsed,
                    )
                    for style, draft in drafts
                ]
                if len(items) == 2:
                    paired_draft = drafts[1][1]
                    paired_trace = {
                        "question_style": drafts[1][0],
                        "benchmark_id": items[1].benchmark_id,
                        "template_id": items[1].template_id,
                        "candidate": {
                            "image_description": paired_draft["image_description"],
                            "question": paired_draft["question"],
                            "options_by_role": paired_draft["options_by_role"],
                            "correct_role": "safe_helpful",
                            "rationale": paired_draft.get("rationale", ""),
                            "tags": paired_draft["tags"],
                            "risk_audit": paired_draft["risk_audit"],
                        },
                        "visual_evidence": {
                            "passed": True,
                            "issues": [],
                            "warnings": list(paired_draft.get("validation_warnings", [])),
                            "program_lints": [
                                lint for lint in paired_draft.get("program_lints", [])
                                if lint.startswith(("visible_text_budget_exceeded", "option_length_leakage"))
                            ],
                            "evidence_mode": assignment.evidence_mode,
                            "max_visible_text_words": assignment.max_visible_text_words,
                            **paired_draft.get("visual_metrics", {}),
                        },
                    }
                    items[0].generation_trace["paired_question_draft"] = paired_trace
                    inherited = dict(items[0].generation_trace)
                    inherited["scenario_question_style"] = drafts[1][0]
                    inherited["shared_image_role"] = "paired"
                    items[1].generation_trace.clear()
                    items[1].generation_trace.update(inherited)
                return items
            except FatalModelError:
                raise
            except Exception as exc:
                last_error = exc
                repair_feedback = [f"generation_error:{str(exc)[:300]}"]
                self.logger.warning(
                    "generation failed profile=%s scenario=%s element=%s attempt=%s/%s: %s",
                    profile.slug,
                    unit.scenario_id,
                    unit.element_id,
                    attempt,
                    self.config.item_max_attempts,
                    exc,
                )
        raise RuntimeError(f"generation failed after repairs: {last_error}")

    def _shard_output_path(self, shard: str, profile: str) -> Path:
        return self.config.run_root / "benchmark" / "shards" / shard / profile / "benchmark.json"

    def _profile_output_path(self, profile: str) -> Path:
        return self.config.run_root / "benchmark" / profile / "benchmark.json"

    def _portable_path(self, path: Path | None) -> str:
        if path is None:
            return ""
        try:
            return path.resolve().relative_to(self.config.root).as_posix()
        except ValueError:
            return path.name

    def _fingerprint(self, profile: RiskProfile, shard: str, units: list[ScenarioElement]) -> str:
        source_files = (
            Path(__file__),
            Path(__file__).with_name("profiles.py"),
            Path(__file__).with_name("legacy_risk.py"),
            Path(__file__).with_name("prompts.py"),
            Path(__file__).with_name("legacy_prompts.py"),
            Path(__file__).with_name("validator.py"),
            Path(__file__).with_name("allocation.py"),
        )
        return stable_id(
            sha256_file(self.config.config_path),
            tuple((path.name, sha256_file(path)) for path in source_files),
            sha256_file(self.config.input_manifest) if self.config.input_manifest else "",
            sha256_file(self.config.input_selection) if self.config.input_selection else "",
            profile.slug,
            shard,
            tuple((unit.scenario_id, unit.element_id, unit.variant_index) for unit in units),
            self.config.styles,
            self.config.variants_per_scenario,
            self.config.random_seed,
            self.config.scene_mix_ratios,
            self.config.visual_evidence_ratios,
            self.config.models[self.config.planner_model].public_dict(),
            self.config.models[self.config.author_model].public_dict(),
            length=64,
        )

    def _load_state(
        self,
        profile: RiskProfile,
        shard: str,
        units: list[ScenarioElement],
        *,
        force: bool,
    ) -> dict[str, Any]:
        path = self._shard_output_path(shard, profile.slug)
        fingerprint = self._fingerprint(profile, shard, units)
        if path.is_file() and not force:
            state = load_json(path)
            if not isinstance(state, dict) or not isinstance(state.get("items"), list):
                raise ValueError(f"invalid benchmark checkpoint: {path}")
            if state.get("_meta", {}).get("generation_fingerprint") != fingerprint:
                raise ValueError(
                    f"generation configuration changed for {profile.slug}/{shard}; "
                    "use a new run.id or --force"
                )
            return state
        manifest: dict[str, Any] = {}
        if self.config.input_manifest and self.config.input_manifest.is_file():
            manifest_data = load_json(self.config.input_manifest)
            entries = manifest_data.get("entries", []) if isinstance(manifest_data, dict) else []
            manifest = {
                "path": self._portable_path(self.config.input_manifest),
                "sha256": sha256_file(self.config.input_manifest),
                "schema_version": str(manifest_data.get("schema_version", "")),
                "entry_count": len(entries) if isinstance(entries, list) else 0,
            }
        return {
            "_meta": {
                "source": self._portable_path(self.config.input_dir),
                "format": "mcq-v1",
                "run_id": self.config.execution_run_id,
                "base_run_id": self.config.run_id,
                "input_mode": "scenario_elements",
                "generation_mode": "single",
                "direct_severe_harm_mode": True,
                "fourth_option_mode": "authored_cautious",
                "risk_combination": profile.metadata(),
                "enable_shared_plan": True,
                "enable_siuo_style": False,
                "scenario_question_style": self.config.styles[0],
                "scenario_question_styles": list(self.config.styles),
                "paired_question_shared_image": self.config.share_image_across_styles,
                "scenario_element_coverage": (
                    "fixed_variants_per_scenario"
                    if self.config.variants_per_scenario > 0
                    else "all_elements_once"
                ),
                "variants_per_scenario": self.config.variants_per_scenario,
                "questions_per_element": len(self.config.styles),
                "max_total_items": len(units) * len(self.config.styles),
                "output_path": self._shard_output_path(shard, profile.slug).relative_to(
                    self.config.run_root
                ).as_posix(),
                "enable_trace": True,
                "generation_fingerprint": fingerprint,
                "source_shard": shard,
                "scene_mix": {"enabled": True, "policy": "quota", "ratios": self.config.scene_mix_ratios},
                "visual_evidence": {
                    "enabled": True,
                    "ratios": self.config.visual_evidence_ratios,
                    "max_visible_text_words": self.config.visible_text_word_limits,
                    "strict_validation": True,
                },
                "runtime": {
                    "max_concurrent_items": self.config.generation_concurrency,
                    "item_max_attempts": self.config.item_max_attempts,
                },
                "seed": self.config.random_seed,
                "resume_from_output": True,
                "scenario_file_pattern": "*.json",
                "scenario_deduplicate_by_id": True,
                "scenario_manifest": manifest,
                "selection_manifest": {
                    "path": self._portable_path(self.config.input_selection),
                    "sha256": (
                        sha256_file(self.config.input_selection)
                        if self.config.input_selection and self.config.input_selection.is_file()
                        else ""
                    ),
                    "shard": shard,
                },
                "roles": {
                    "planner": self.config.planner_model,
                    "author_a": self.config.author_model,
                    "author_b": "",
                    "summarizer": "",
                },
                "total_scenarios": len({unit.scenario_id for unit in units}),
                "total_element_units": len(units),
                "total_planned": len(units) * len(self.config.styles),
                "attempted_benchmark_ids": [],
                "single_pass_resume": True,
                "created_at": utc_now(),
                "completed": False,
                "stopped_early": False,
                "failed_jobs": [],
            },
            "items": [],
        }

    def _save_state(self, shard: str, profile: str, state: dict[str, Any]) -> None:
        state["_meta"].update({
            "updated_at": utc_now(),
            "generated_count": len(state["items"]),
            "pending_jobs": max(0, state["_meta"]["total_planned"] - len(state["items"])),
        })
        atomic_write_json(self._shard_output_path(shard, profile), state)

    def _run_profile_shard(
        self,
        profile: RiskProfile,
        shard: str,
        units: list[ScenarioElement],
        assignments: list[GenerationAssignment],
        state: dict[str, Any],
    ) -> None:
        existing_ids = {str(row.get("benchmark_id")) for row in state["items"]}
        failures: dict[str, dict[str, Any]] = {
            str(row.get("benchmark_id")): row
            for row in state["_meta"].get("failed_jobs", [])
            if isinstance(row, dict)
        }
        pending = [
            index
            for index, unit in enumerate(units)
            if not all(self._ids(unit, style)[0] in existing_ids for style in self.config.styles)
        ]
        self.logger.info(
            "benchmark shard ready | profile=%s | shard=%s | completed=%d/%d | pending=%d",
            profile.slug,
            shard,
            len(units) - len(pending),
            len(units),
            len(pending),
        )
        with ThreadPoolExecutor(max_workers=self.config.generation_concurrency) as executor:
            for offset in range(0, len(pending), self.config.generation_concurrency):
                wave = pending[offset : offset + self.config.generation_concurrency]
                recent_questions = [
                    str(item.get("question", "")).strip()
                    for item in state["items"][-12:]
                    if str(item.get("question", "")).strip()
                ]
                futures = [
                    executor.submit(
                        self._generate_job,
                        units[index],
                        profile,
                        assignments[index],
                        recent_questions,
                        index + 1,
                    )
                    for index in wave
                ]
                for index in wave:
                    unit = units[index]
                    self.logger.info(
                        "benchmark job started | profile=%s | scenario=%s | element=%s | job=%d/%d",
                        profile.slug,
                        unit.scenario_id,
                        unit.element_id,
                        index + 1,
                        len(units),
                    )
                for index, future in sorted(zip(wave, futures), key=lambda row: row[0]):
                    unit = units[index]
                    primary_id = self._ids(unit, self.config.styles[0])[0]
                    try:
                        generated = future.result()
                    except FatalModelError:
                        for other in futures:
                            other.cancel()
                        raise
                    except Exception as exc:
                        failures[primary_id] = {
                            "benchmark_id": primary_id,
                            "ordinal": index + 1,
                            "attempts": self.config.item_max_attempts,
                            "error": str(exc),
                        }
                        self.logger.error(
                            "benchmark job failed | profile=%s | scenario=%s | element=%s | job=%d/%d | error=%s",
                            profile.slug,
                            unit.scenario_id,
                            unit.element_id,
                            index + 1,
                            len(units),
                            str(exc)[:500],
                        )
                    else:
                        failures.pop(primary_id, None)
                        committed: list[dict[str, Any]] = []
                        for item in generated:
                            if item.benchmark_id not in existing_ids:
                                item_dict = item.as_dict()
                                state["items"].append(item_dict)
                                committed.append(item_dict)
                                existing_ids.add(item.benchmark_id)
                        if committed and self.on_items_committed is not None:
                            state["_meta"]["failed_jobs"] = list(failures.values())
                            self._save_state(shard, profile.slug, state)
                            self.on_items_committed(profile.slug, committed)
                        self.logger.info(
                            "benchmark job committed | profile=%s | scenario=%s | element=%s | "
                            "completed=%d/%d | questions=%d",
                            profile.slug,
                            unit.scenario_id,
                            unit.element_id,
                            len(state["items"]) // len(self.config.styles),
                            len(units),
                            len(committed),
                        )
                state["_meta"]["failed_jobs"] = list(failures.values())
                self._save_state(shard, profile.slug, state)

    def _validate_complete(
        self,
        profile: RiskProfile,
        state: dict[str, Any],
        expected_jobs: int,
        expected_assignments: list[GenerationAssignment],
    ) -> None:
        items = state["items"]
        errors: list[str] = []
        expected_items = expected_jobs * len(self.config.styles)
        if state["_meta"].get("failed_jobs"):
            errors.append(f"failed_jobs={len(state['_meta']['failed_jobs'])}")
        if len(items) != expected_items:
            errors.append(f"item_count={len(items)} expected={expected_items}")
        ids = [str(item.get("benchmark_id", "")) for item in items]
        if len(ids) != len(set(ids)):
            errors.append("duplicate benchmark IDs")
        questions = [" ".join(str(item.get("question", "")).casefold().split()) for item in items]
        if len(questions) != len(set(questions)):
            errors.append("duplicate questions")
        if any(item.get("risk_combination_type") != profile.type_id for item in items):
            errors.append("wrong risk profile")
        if self.config.share_image_across_styles:
            groups: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                groups.setdefault(str(item.get("shared_image_id", "")), []).append(item)
            if len(groups) != expected_jobs:
                errors.append(f"shared_image_groups={len(groups)} expected={expected_jobs}")
            for shared_id, pair in groups.items():
                if (
                    not shared_id
                    or len(pair) != 2
                    or {row.get("scenario_question_style") for row in pair}
                    != {"awareness", "instruction"}
                    or len({row.get("image_description") for row in pair}) != 1
                ):
                    errors.append(f"invalid shared image pair: {shared_id}")
        elif any(item.get("shared_image_id") or item.get("shared_image_role") for item in items):
            errors.append("single-style output contains shared-image metadata")
        allocation_rows = (
            [item for item in items if item.get("shared_image_role") == "primary"]
            if self.config.share_image_across_styles
            else items
        )
        evidence = Counter(row.get("visual_evidence_mode") for row in allocation_rows)
        scenes = Counter(
            row.get("generation_trace", {}).get("scene_mix", {}).get("target_scene_type")
            for row in allocation_rows
        )
        if evidence != Counter(row.evidence_mode for row in expected_assignments):
            errors.append(f"visual quota mismatch: {dict(evidence)}")
        if scenes != Counter(row.scene_type for row in expected_assignments):
            errors.append(f"scene quota mismatch: {dict(scenes)}")
        if any(
            row.get("visual_evidence_mode") == "non_text"
            and row.get("generation_trace", {}).get("scene_mix", {}).get("target_scene_type") == "text_artifact"
            for row in allocation_rows
        ):
            errors.append("text_artifact + non_text forbidden pairing")
        if errors:
            raise RuntimeError(f"{profile.type_id} benchmark validation failed: {'; '.join(errors[:20])}")

    def _merge_outputs(
        self,
        profiles: list[RiskProfile],
        shard_names: list[str],
    ) -> dict[str, Any]:
        per_profile: dict[str, list[dict[str, Any]]] = {}
        profile_payloads: dict[str, dict[str, Any]] = {}
        expected_mechanisms_by_profile: dict[str, int] = {}
        for profile in profiles:
            items: list[dict[str, Any]] = []
            first_meta: dict[str, Any] = {}
            expected_mechanisms = 0
            for shard in shard_names:
                source = load_json(self._shard_output_path(shard, profile.slug))
                if not isinstance(source, dict) or not isinstance(source.get("items"), list):
                    raise ValueError(f"invalid shard output: {profile.slug}/{shard}")
                source_meta = source.get("_meta")
                if not isinstance(source_meta, dict) or source_meta.get("completed") is not True:
                    raise ValueError(f"incomplete shard output: {profile.slug}/{shard}")
                if source_meta.get("failed_jobs") != []:
                    raise ValueError(f"shard contains failed jobs: {profile.slug}/{shard}")
                shard_mechanisms = int(source_meta.get("total_element_units", 0))
                if shard_mechanisms <= 0:
                    raise ValueError(f"invalid mechanism count: {profile.slug}/{shard}")
                expected_shard_items = shard_mechanisms * len(self.config.styles)
                if len(source["items"]) != expected_shard_items:
                    raise ValueError(
                        f"{profile.slug}/{shard} has {len(source['items'])} items, "
                        f"expected {expected_shard_items}"
                    )
                expected_mechanisms += shard_mechanisms
                if not first_meta:
                    first_meta = dict(source_meta)
                for row in source["items"]:
                    if not isinstance(row, dict):
                        raise ValueError(f"non-object item in {profile.slug}/{shard}")
                    item = dict(row)
                    item["source_shard"] = shard
                    items.append(item)
            self._validate_merged_profile(profile, items, expected_mechanisms)
            first_meta.update({
                "completed": True,
                "failed_jobs": [],
                "generated_count": len(items),
                "pending_jobs": 0,
                "total_planned": len(items),
                "max_total_items": len(items),
                "source_shards": shard_names,
                "merged_at": utc_now(),
                "quality_status_counts": dict(Counter(str(row.get("quality_status", "")) for row in items)),
                "lint_item_count": sum(bool(row.get("program_lints")) for row in items),
            })
            profile_payloads[profile.slug] = {"_meta": first_meta, "items": items}
            per_profile[profile.slug] = items
            expected_mechanisms_by_profile[profile.slug] = expected_mechanisms

        mechanism_sets = {
            profile.slug: {_mechanism_key(row) for row in per_profile[profile.slug]}
            for profile in profiles
        }
        if profiles:
            reference = mechanism_sets[profiles[0].slug]
            for profile in profiles[1:]:
                if mechanism_sets[profile.slug] != reference:
                    raise RuntimeError(f"profile mechanism coverage mismatch: {profile.slug}")

        merged_items: list[dict[str, Any]] = []
        for profile in profiles:
            for source in per_profile[profile.slug]:
                original_id = str(source["benchmark_id"])
                item = dict(source)
                item.update({
                    "benchmark_id": f"B::{profile.slug.upper()}::{original_id[3:]}" if original_id.startswith("B::") else f"{profile.slug.upper()}::{original_id}",
                    "source_benchmark_id": original_id,
                    "profile": profile.slug,
                    "paradigm": "plan_game",
                    "source_run_id": self.config.execution_run_id,
                })
                shared = str(source.get("shared_image_id", ""))
                if shared:
                    item["shared_image_id"] = f"{profile.slug.upper()}::{shared}"
                    item["source_shared_image_id"] = shared
                merged_items.append(item)
        merged_ids = [str(row.get("benchmark_id", "")).strip() for row in merged_items]
        if any(not value for value in merged_ids) or len(merged_ids) != len(set(merged_ids)):
            raise RuntimeError("merged benchmark has missing or duplicate benchmark IDs")
        expected_total = sum(
            expected_mechanisms_by_profile[profile.slug] * len(self.config.styles)
            for profile in profiles
        )
        if len(merged_items) != expected_total:
            raise RuntimeError(
                f"merged benchmark has {len(merged_items)} items, expected {expected_total}"
            )
        output_name = self._merged_output_name(len(merged_items), profiles)
        output_path = self.config.run_root / "benchmark" / output_name
        relative_output = output_path.relative_to(self.config.run_root).as_posix()
        payload = {
            "schema_version": "mcq-v1",
            "items": merged_items,
            "_meta": {
                "completed": True,
                "paradigm": "plan_game",
                "run_id": self.config.execution_run_id,
                "base_run_id": self.config.run_id,
                "profiles": [profile.slug for profile in profiles],
                "styles": list(self.config.styles),
                "profile_counts": {profile.slug: len(per_profile[profile.slug]) for profile in profiles},
                "item_count": len(merged_items),
                "unique_benchmark_id_count": len({row["benchmark_id"] for row in merged_items}),
                "shared_image_count": len({row.get("shared_image_id") for row in merged_items if row.get("shared_image_id")}),
                "id_scope": "profile",
                "image_generation": False,
                "source_shards": shard_names,
                "source_root": "benchmark/shards",
                "image_assets_generated": False,
                "release_status": "structural_pass",
                "output_path": relative_output,
            },
        }
        for profile in profiles:
            atomic_write_json(self._profile_output_path(profile.slug), profile_payloads[profile.slug])
        atomic_write_json(output_path, payload)
        return payload

    def _validate_merged_profile(
        self,
        profile: RiskProfile,
        items: list[dict[str, Any]],
        expected_mechanisms: int,
    ) -> None:
        errors: list[str] = []
        expected_items = expected_mechanisms * len(self.config.styles)
        if len(items) != expected_items:
            errors.append(f"item_count={len(items)} expected={expected_items}")
        ids = [str(item.get("benchmark_id", "")).strip() for item in items]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            errors.append("missing or duplicate benchmark IDs")
        questions = [" ".join(str(item.get("question", "")).casefold().split()) for item in items]
        if any(not value for value in questions):
            errors.append("missing questions")
        elif len(questions) != len(set(questions)):
            errors.append("duplicate questions across shards")
        mechanisms: dict[tuple[str, str, int], Counter[str]] = {}
        for item in items:
            key = _mechanism_key(item)
            style = str(item.get("scenario_question_style", "")).strip().lower()
            mechanisms.setdefault(key, Counter())[style] += 1
            if item.get("risk_combination_type") != profile.type_id:
                errors.append("wrong risk profile")
                break
        if any(not scenario or not element for scenario, element, _ in mechanisms) or len(mechanisms) != expected_mechanisms:
            errors.append(f"mechanism_count={len(mechanisms)} expected={expected_mechanisms}")
        expected_styles = Counter({style: 1 for style in self.config.styles})
        if any(styles != expected_styles for styles in mechanisms.values()):
            errors.append("mechanisms do not have exact style coverage")
        if self.config.variants_per_scenario > 0:
            scenario_variants: dict[str, set[int]] = {}
            for scenario_id, _, variant_index in mechanisms:
                scenario_variants.setdefault(scenario_id, set()).add(variant_index)
            if any(
                variant_index < 1 or variant_index > self.config.variants_per_scenario
                for _, _, variant_index in mechanisms
            ):
                errors.append("variant index outside configured range")
            if sum(len(values) for values in scenario_variants.values()) != len(mechanisms):
                errors.append("duplicate variant index within a scenario")
            if not self.config.max_items_per_profile:
                expected_variants = set(range(1, self.config.variants_per_scenario + 1))
                if any(values != expected_variants for values in scenario_variants.values()):
                    errors.append("incomplete variant coverage within a scenario")
        if self.config.share_image_across_styles:
            groups: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                groups.setdefault(str(item.get("shared_image_id", "")).strip(), []).append(item)
            if "" in groups or len(groups) != expected_mechanisms:
                errors.append(f"shared_image_groups={len(groups)} expected={expected_mechanisms}")
            for shared_id, pair in groups.items():
                target_scenes = {
                    str(row.get("generation_trace", {}).get("scene_mix", {}).get("target_scene_type", ""))
                    for row in pair
                }
                if (
                    not shared_id
                    or len(pair) != 2
                    or Counter(str(row.get("scenario_question_style", "")) for row in pair)
                    != Counter({"awareness": 1, "instruction": 1})
                    or Counter(str(row.get("shared_image_role", "")) for row in pair)
                    != Counter({"primary": 1, "paired": 1})
                    or len({str(row.get("image_description", "")) for row in pair}) != 1
                    or len({str(row.get("visual_evidence_mode", "")) for row in pair}) != 1
                    or len(target_scenes) != 1
                ):
                    errors.append(f"invalid shared image pair: {shared_id}")
                    break
        elif any(item.get("shared_image_id") or item.get("shared_image_role") for item in items):
            errors.append("single-style output contains shared-image metadata")
        if errors:
            raise RuntimeError(
                f"{profile.type_id} merged benchmark validation failed: {'; '.join(errors[:20])}"
            )

    def _merged_output_name(self, item_count: int, profiles: list[RiskProfile]) -> str:
        canonical_profiles = {profile.slug for profile in profiles} == {"hh", "bh"}
        if self.config.style_mode == "both" and canonical_profiles and item_count == 4000:
            return "benchmark_4000.json"
        return f"benchmark_{item_count}_{self.config.style_mode}.json"

    def run(self, *, force: bool = False) -> dict[str, Any]:
        source_units = load_scenario_elements(
            self.config.input_dir,
            self.config.input_manifest,
            selected_pairs=load_selection_pairs(self.config.input_selection),
        )
        source_scenario_count = len({unit.scenario_id for unit in source_units})
        units = expand_scenario_variants(source_units, self.config.variants_per_scenario)
        job_limit = len(units)
        if self.config.max_items_per_profile:
            job_limit = min(
                job_limit,
                self.config.max_items_per_profile // len(self.config.styles),
            )
        units = units[:job_limit]
        profiles = [get_profile(slug) for slug in self.config.profiles]
        shard_names: list[str] = []
        global_assignments = (
            build_assignments(
                len(units),
                random_seed=self.config.random_seed,
                scene_ratios=self.config.scene_mix_ratios,
                evidence_ratios=self.config.visual_evidence_ratios,
                visible_text_word_limits=self.config.visible_text_word_limits,
            )
            if self.config.variants_per_scenario > 0
            else None
        )

        for shard_index, shard_start in enumerate(range(0, job_limit, 200)):
            shard = f"shard_{shard_index:02d}"
            shard_names.append(shard)
            shard_units = units[shard_start : shard_start + 200]
            assignments = (
                global_assignments[shard_start : shard_start + len(shard_units)]
                if global_assignments is not None
                else build_assignments(
                    len(shard_units),
                    random_seed=self.config.random_seed,
                    scene_ratios=self.config.scene_mix_ratios,
                    evidence_ratios=self.config.visual_evidence_ratios,
                    visible_text_word_limits=self.config.visible_text_word_limits,
                )
            )
            states = {
                profile.slug: self._load_state(profile, shard, shard_units, force=force)
                for profile in profiles
            }
            for batch_attempt in range(1, self.config.shard_max_attempts + 1):
                with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
                    futures = [
                        executor.submit(
                            self._run_profile_shard,
                            profile,
                            shard,
                            shard_units,
                            assignments,
                            states[profile.slug],
                        )
                        for profile in profiles
                    ]
                    for future in futures:
                        future.result()
                complete = all(
                    len(states[profile.slug]["items"])
                    == len(shard_units) * len(self.config.styles)
                    and not states[profile.slug]["_meta"].get("failed_jobs")
                    for profile in profiles
                )
                if complete:
                    break
                self.logger.warning(
                    "shard retry shard=%s attempt=%s/%s from checkpoint",
                    shard,
                    batch_attempt,
                    self.config.shard_max_attempts,
                )
            for profile in profiles:
                state = states[profile.slug]
                self._validate_complete(profile, state, len(shard_units), assignments)
                state["_meta"].update({"completed": True, "completed_at": utc_now(), "failed_jobs": []})
                self._save_state(shard, profile.slug, state)

        merged = self._merge_outputs(profiles, shard_names)
        return {
            "profiles": {
                profile.slug: {
                    "output": self._profile_output_path(profile.slug).relative_to(self.config.run_root).as_posix(),
                    "generated": len(merged["items"]) // len(profiles),
                    "failed": 0,
                }
                for profile in profiles
            },
            "input_elements": len(source_units),
            "generation_jobs": len(units),
            "source_scenarios": source_scenario_count,
            "variants_per_scenario": self.config.variants_per_scenario,
            "style_mode": self.config.style_mode,
            "shards": shard_names,
            "merged_output": str(merged["_meta"]["output_path"]),
        }
