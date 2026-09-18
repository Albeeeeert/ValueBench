from dataclasses import asdict
from types import SimpleNamespace

from ...common import ConfiguredMethod, source_image
from .transforms import (
    KEYWORD_EXTRACTION_SYSTEM_PROMPT, PAPER_PROMPT_TEMPLATE,
    _parse_extracted_keywords, _keyword_tokens, _mask_keywords,
    _build_cross_modal_clues, _render_clue_image,
)


class Method(ConfiguredMethod):
    name = "camo"
    requires_original_image = True
    uses_auxiliary = True

    def generate(self, source, sample_id, output_dir):
        question = source.benchmark["question"]
        def validate(text):
            extracted = _parse_extracted_keywords(text, question)
            _keyword_tokens(question, extracted)
            return {"keywords": [asdict(k) for k in extracted]}
        data, auxiliary = self.prepare(
            source, sample_id, output_dir, "keywords", KEYWORD_EXTRACTION_SYSTEM_PROMPT, validate,
            user=f"[Request]\n{question}\n[/Request]",
        )
        from .transforms import ExtractedKeyword
        extracted = [ExtractedKeyword(**k) for k in data["keywords"]]
        p = self.parameters
        if not 0 < p["character_masking_ratio"] <= 1:
            raise ValueError("CAMO character_masking_ratio must be in (0, 1]")
        masked, tokens = _mask_keywords(question, _keyword_tokens(question, extracted), p["character_masking_ratio"], p["mask_character"])
        math, clues = _build_cross_modal_clues(tokens, self.rng(source))
        scene = source_image(source)
        scale = scene.width / p["reference_width"]
        settings = SimpleNamespace(**{**p, "font_path": str(self.font_path), "image_width": scene.width,
                                       "image_height": scene.height, "clue_font_size": max(1, round(p["clue_font_size"]*scale)),
                                       "minimum_clue_font_size": max(1, round(p["minimum_clue_font_size"]*scale))})
        image, layout = _render_clue_image(scene, clues, settings)
        prompt = PAPER_PROMPT_TEMPLATE.format(math_section="\n".join(f"- Solve: {q['expression']}" for q in math), masked_text=masked)
        return self.result(image, prompt, sample_id, output_dir,
                           {**data, "auxiliary": auxiliary, "masked_text": masked, "math_questions": math,
                            "image_clues": clues, "layout": layout, "source_image_size": list(scene.size)})
