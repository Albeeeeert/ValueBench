from ...common import ConfiguredMethod, source_image, stack, prepare_phrase, phrase_panel
from .prompts import PREPARATION_PROMPT


class Method(ConfiguredMethod):
    name = "qr"
    requires_original_image = True
    uses_auxiliary = True

    def generate(self, source, sample_id, output_dir):
        data, auxiliary = prepare_phrase(self, source, sample_id, output_dir, PREPARATION_PROMPT)
        scene = source_image(source)
        panel, layout = phrase_panel(self, data["key_phrase"], scene.width)
        return self.result(stack(scene, panel), data["rephrased_question"], sample_id, output_dir,
                           {**data, "auxiliary": auxiliary, "typography": layout, "source_image_size": list(scene.size)})
