from ...common import ConfiguredMethod, source_image, stack, prepare_phrase, phrase_panel
from .prompts import PREPARATION_PROMPT


class Method(ConfiguredMethod):
    name = "si"
    requires_original_image = True
    uses_auxiliary = True

    def generate(self, source, sample_id, output_dir):
        scene = source_image(source)
        n = self.parameters["blocks_per_side"]
        if type(n) is not int or n < 1 or scene.width % n or scene.height % n:
            raise ValueError("SI scene dimensions must be divisible by blocks_per_side")
        data, auxiliary = prepare_phrase(self, source, sample_id, output_dir, PREPARATION_PROMPT)
        w, h = scene.width // n, scene.height // n
        blocks = [scene.crop((c*w, r*h, (c+1)*w, (r+1)*h)) for r in range(n) for c in range(n)]
        order = list(range(n*n))
        self.rng(source, "scene").shuffle(order)
        if len(order) > 1 and order == list(range(n*n)):
            order = order[1:] + order[:1]
        shuffled = scene.copy()
        for i, block in enumerate(order):
            shuffled.paste(blocks[block], ((i % n)*w, (i // n)*h))
        prompt = source.benchmark["question"]
        if self.parameters["shuffle_prompt"]:
            words = prompt.split()
            mixed = words.copy()
            self.rng(source, "prompt").shuffle(mixed)
            if len(words) > 1 and mixed == words:
                mixed = mixed[1:] + mixed[:1]
            prompt = " ".join(mixed)
        panel, layout = phrase_panel(self, data["key_phrase"], scene.width)
        return self.result(stack(shuffled, panel), prompt, sample_id, output_dir,
                           {**data, "auxiliary": auxiliary, "block_order": order, "typography": layout,
                            "source_image_size": list(scene.size), "prompt_word_order_shuffled": self.parameters["shuffle_prompt"]})
