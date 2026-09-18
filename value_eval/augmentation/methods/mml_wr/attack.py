import re
from PIL import ImageOps
from ...common import ConfiguredMethod, text_canvas
from .prompts import ATTACK_PROMPT


class Method(ConfiguredMethod):
    name = 'mml_wr'

    def generate(self, source, sample_id, output_dir):
        question = source.benchmark["question"]
        p = self.parameters
        metadata = {}
        title = question
        from .transforms import word_replace_transformer
        metadata = word_replace_transformer(question, seed=p["seed"], max_token_changes=p["max_token_changes"])
        title = metadata["replaced_prompt"]
        prompt = ATTACK_PROMPT.format(metadata["restoration_map"], metadata["scrambled_original_words"])
        image, layout = text_canvas(title, self.font_path, p, steps=p["steps"])
        
        return self.result(image, prompt, sample_id, output_dir, {**metadata, **layout})
