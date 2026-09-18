import random
import re
from pathlib import Path

from PIL import Image, ImageOps

from ...base import fingerprint
from ...common import ConfiguredMethod, text_canvas, text_panel, stack
from ....io_utils import sha256_file
from .retrieval import ClipRetrieval
from .prompts import PREPARATION_PROMPT, USER_PROMPT_TEMPLATE, ATTACK_PROMPT


def validate_subquestions(text):
    # Preserve the original numbered answer format; never truncate extra items
    # or silently replace a failed decomposition with the original question.
    matches = list(re.finditer(r"(?m)^[ \t]*(\d+)\.[ \t]*", text))
    if [m[1] for m in matches] != ["1", "2", "3"] or text[:matches[0].start()].strip():
        raise ValueError("CS-DJ requires exactly three sub-questions numbered 1, 2, 3")
    ends = [m.start() for m in matches[1:]] + [len(text)]
    questions = [text[m.end():end].strip() for m, end in zip(matches, ends)]
    if not all(questions):
        raise ValueError("CS-DJ sub-questions must be nonempty")
    return {"sub_questions": questions}


class Method(ConfiguredMethod):
    name = "cs_dj"
    uses_auxiliary = True

    def bind(self, config):
        super().bind(config)
        p = self.parameters
        if p["max_pairs_per_question"] != 9:
            raise ValueError("CS-DJ requires nine distraction panels before panels 10, 11, 12")
        root = (config.root / p["src_dir"]).resolve()
        self.paths = sorted(f for f in root.iterdir() if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
        random.Random(p["seed"]).shuffle(self.paths)
        self.paths = self.paths[:p["num_images"]]
        if len(self.paths) < 9:
            raise ValueError("CS-DJ distraction corpus must contain at least nine images")
        # Embedding rows follow self.paths; the cache key must preserve that order.
        self.snapshot["corpus"] = [{"path": str(f), "sha256": sha256_file(f)} for f in self.paths]
        self.snapshot["clip_config_sha256"] = sha256_file(Path(p["clip_path"]) / "config.json")
        self._refresh_fingerprint()
        self.resource_key = fingerprint([self.snapshot["corpus"], self.snapshot["clip_config_sha256"], p["clip_path"]])
        return self

    def generate(self, source, sample_id, output_dir):
        data, auxiliary = self.prepare(
            source, sample_id, output_dir, "subquestions", PREPARATION_PROMPT, validate_subquestions,
            user=USER_PROMPT_TEMPLATE.format(jailbreak_prompt=source.benchmark["question"]),
        )
        with self._lock:
            if not self._resources:
                self._resources.append(ClipRetrieval(self.parameters, self.paths))
            paths = self._resources[0].select(source.benchmark["question"], output_dir, self.resource_key)
        p = self.parameters
        size = p["tile_size"]
        tiles = []
        for path in paths:
            with Image.open(path) as img:
                tiles.append(ImageOps.pad(img.convert("RGB"), (size, size), color="white"))
        layouts = []
        for question in data["sub_questions"]:
            tile, layout = text_canvas(question, self.font_path, {**p, "image_width":size, "image_height":size}, steps=0)
            tile = ImageOps.colorize(tile.convert("L"), p["text_color"], p["background_color"])
            tiles.append(tile)
            layouts.append(layout)
        numbered = []
        for i, tile in enumerate(tiles, 1):
            if p["rotation_angle"]:
                tile = ImageOps.pad(tile.rotate(p["rotation_angle"], expand=True, fillcolor="white"), (size, size), color="white")
            label, _ = text_panel(str(i), size, self.font_path, font_size=20, reference_width=500, padding=8, align="center")
            numbered.append(stack(tile, label))
        columns = p["images_per_row"]
        if type(columns) is not int or columns < 1:
            raise ValueError("CS-DJ images_per_row must be positive")
        height = numbered[0].height
        grid = Image.new("RGB", (size*columns, height*((len(numbered)+columns-1)//columns)), "white")
        for i, tile in enumerate(numbered):
            grid.paste(tile, ((i % columns)*size, (i // columns)*height))
        return self.result(grid, ATTACK_PROMPT, sample_id, output_dir,
                           {**data, "auxiliary":auxiliary, "distraction_images":[str(f) for f in paths], "text_layouts":layouts,
                            "distraction_panel_count":9, "text_panel_count":3})
