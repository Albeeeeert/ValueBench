from pathlib import Path

from ...common import ConfiguredMethod, source_image, stack, prepare_phrase, phrase_panel
from ....io_utils import sha256_file
from .prompts import PREPARATION_PROMPT
from .attention import AttentionModel


class Method(ConfiguredMethod):
    name = "viscra"
    requires_original_image = True
    uses_auxiliary = True

    def bind(self, config):
        super().bind(config)
        self.snapshot["attention_model_config_sha256"] = sha256_file(Path(self.parameters["attention_model_path"]) / "config.json")
        self._refresh_fingerprint()
        return self

    def generate(self, source, sample_id, output_dir):
        data, auxiliary = prepare_phrase(self, source, sample_id, output_dir, PREPARATION_PROMPT)
        scene = source_image(source)
        with self._lock:
            if not self._resources:
                self._resources.append(AttentionModel(self.parameters))
            masked, attention = self._resources[0].mask(scene, data["key_phrase"])
        panel, layout = phrase_panel(self, data["key_phrase"], scene.width)
        prompt = self.parameters["attention_query_template"].format(
            color=self.parameters["mask_color"], query=data["rephrased_question"],
        ) + "\n\nShow your reasoning ability."
        return self.result(stack(masked, panel), prompt, sample_id, output_dir,
                           {**data, "auxiliary":auxiliary, **attention, "typography":layout,
                            "input_prompt_source":"rephrased_question", "input_prompt":data["rephrased_question"],
                            "source_image_size":list(scene.size)})
