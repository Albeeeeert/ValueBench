import re
from PIL import Image, ImageOps
from ...common import ConfiguredMethod, text_canvas
from .prompts import ATTACK_PROMPT


class Method(ConfiguredMethod):
    name = 'mml_rotate'

    def generate(self, source, sample_id, output_dir):
        question = source.benchmark["question"]
        p = self.parameters
        metadata = {}
        title = question
        words = re.findall(r"\b\w+\b", question)
        self.rng(source).shuffle(words)
        metadata["scrambled_original_words"] = words
        prompt = ATTACK_PROMPT.format(words)
        image, layout = text_canvas(title, self.font_path, p, steps=p["steps"])
        image = image.transpose(Image.Transpose.ROTATE_180)
        return self.result(image, prompt, sample_id, output_dir, {**metadata, **layout})
