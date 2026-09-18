import re
from typing import Dict

from ...common import ConfiguredMethod, source_image, stack, phrase_panel
from .prompts import PREPARATION_PROMPT, USER_PROMPT_TEMPLATE, build_jailbreak_prompt


PLACEHOLDER = "( )"


def _normalize_text(text: str) -> str:
    """Normalize text for reconstruction validation without changing output."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))

def _clean_single_line(text: str) -> str:
    text = text.strip().strip('"`')
    return re.sub(r"\s+", " ", text)

def _parse_block_response(text: str) -> Dict[str, str]:
    pattern = r"\[([^\]]+)\]\s*([\s\S]*?)(?=\n\s*\[[^\]]+\]|$)"
    result: Dict[str, str] = {}
    for key, value in re.findall(pattern, text.strip()):
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        result[normalized_key] = value.strip()
    return result

def _canonicalize_placeholder(text: str) -> str:
    text = re.sub(r"\(\s*\)", PLACEHOLDER, text)
    text = re.sub(r"\[\s*\]", PLACEHOLDER, text)
    return _clean_single_line(text)


def validate_transform(text, question):
    data = _parse_block_response(text)
    required = ("harmful_phrase", "textual_part", "visual_part", "image_prompt")
    if not all(_clean_single_line(data.get(key, "")) for key in required):
        missing = [key for key in required if not data.get(key, "").strip()]
        raise ValueError(f"auxiliary response missing fields: {missing}")
    result = {key: _clean_single_line(data[key]) for key in required}
    result["textual_part"] = _canonicalize_placeholder(data["textual_part"])
    if result["textual_part"].count(PLACEHOLDER) != 1:
        raise ValueError("textual_part must contain exactly one '( )' placeholder")
    reconstructed = result["textual_part"].replace(PLACEHOLDER, result["visual_part"])
    if _normalize_text(reconstructed) != _normalize_text(result["harmful_phrase"]):
        raise ValueError("textual_part and visual_part do not reconstruct harmful_phrase")
    # Preserve the source-question contract as well as the original block schema.
    if _normalize_text(result["harmful_phrase"]) != _normalize_text(question):
        raise ValueError("harmful_phrase must preserve the complete source request")
    return result


class Method(ConfiguredMethod):
    name = "himrd"
    requires_original_image = True
    uses_auxiliary = True

    def generate(self, source, sample_id, output_dir):
        data, auxiliary = self.prepare(source, sample_id, output_dir, "transform", PREPARATION_PROMPT,
                                       lambda text: validate_transform(text, source.benchmark["question"]),
                                       user=USER_PROMPT_TEMPLATE.format(question=source.benchmark["question"]))
        scene = source_image(source)
        panel, layout = phrase_panel(self, data["visual_part"], scene.width)
        return self.result(stack(panel, scene), build_jailbreak_prompt(data["textual_part"]), sample_id, output_dir,
                           {**data, "auxiliary": auxiliary, "typography": layout, "source_image_size": list(scene.size)})
