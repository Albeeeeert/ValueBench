from __future__ import annotations

import inspect
import json
import os
import random
import tempfile
import threading
from pathlib import Path

import PIL
import yaml
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .base import AugmentationMethod, AugmentationResult, fingerprint
from .auxiliary import Auxiliary, model_settings
from .methods.figstep.attack import _wrap_pixels
from ..io_utils import atomic_write_json, load_json_if_exists, sha256_file


def save_image(image, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".render-", suffix=".png", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            image.save(handle, format="PNG")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def source_image(source):
    if source.image_path is None:
        raise ValueError("method requires the original HH image")
    with Image.open(source.image_path) as img:
        return ImageOps.exif_transpose(img).convert("RGB")


def stack(*images):
    if len({im.width for im in images}) != 1:
        raise ValueError("stacked panels must have the same width")
    canvas = Image.new("RGB", (images[0].width, sum(im.height for im in images)), "white")
    top = 0
    for im in images:
        canvas.paste(im, (0, top))
        top += im.height
    return canvas


def text_panel(text, width, font_path, *, font_size=48, reference_width=512,
               padding=16, spacing=8, bg="white", fg="black", align="left"):
    scale = width / reference_width
    size, pad, gap = max(1, round(font_size * scale)), max(1, round(padding * scale)), max(1, round(spacing * scale))
    font = ImageFont.truetype(str(font_path), size)
    text = _wrap_pixels(text, font, width - 2 * pad)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    box = probe.multiline_textbbox((0, 0), text, font=font, spacing=gap, align=align)
    panel = Image.new("RGB", (width, box[3] - box[1] + 2 * pad), bg)
    x = pad if align == "left" else (width - (box[2] - box[0])) // 2
    ImageDraw.Draw(panel).multiline_text((x - box[0], pad - box[1]), text, font=font, spacing=gap, fill=fg, align=align)
    return panel, {"rendered_text": text, "font_size": size, "size": list(panel.size)}


def text_canvas(text, font_path, p, *, steps=3):
    width, height = p.get("image_width", 760), p.get("image_height", 760)
    margin = p.get("margin", 20)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for size in range(p.get("font_size", 18), p.get("min_font_size", 10) - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        gap = max(1, round(11 * size / 18))
        wrapped = _wrap_pixels(text, font, width - 2 * margin) + "".join(f"\n{i}. " for i in range(1, steps + 1))
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=gap)
        if box[2] - box[0] <= width - 2 * margin and box[3] - box[1] <= height - 2 * margin:
            draw.multiline_text((margin - box[0], margin - box[1]), wrapped, font=font, spacing=gap, fill="black")
            return image, {"rendered_text": wrapped, "font_size": size}
    raise ValueError("augmentation text overflows canvas at minimum font size")


def prepare_phrase(method, source, sid, output_dir, prompt):
    from .auxiliary import parse_json, strings
    return method.prepare(source, sid, output_dir, "phrase", prompt,
                          lambda text: strings(parse_json(text), ("key_phrase", "phrase_type", "rephrased_question")))


def phrase_panel(method, phrase, width):
    p = method.parameters
    return text_panel(phrase, width, method.font_path, **{k:p[k] for k in ("font_size", "reference_width", "padding", "spacing")})


class ConfiguredMethod(AugmentationMethod):
    uses_auxiliary = False
    uses_image_model = False

    def __init__(self, config_path=None):
        self.directory = Path(inspect.getfile(type(self))).parent
        path = Path(config_path) if config_path else self.directory / "config.yaml"
        self.snapshot = yaml.safe_load(path.read_text())
        if self.snapshot.get("name") != self.name:
            raise ValueError(f"invalid method config for {self.name}")
        self.version = str(self.snapshot["version"])
        self.parameters = self.snapshot["parameters"]
        self.concurrency = self.snapshot.get("runtime", {}).get("concurrency", 1)
        if type(self.concurrency) is not int or self.concurrency < 1:
            raise ValueError("method concurrency must be positive")
        self.font_path = (path.parent / self.parameters["font_path"]).resolve()
        ImageFont.truetype(str(self.font_path), 18)
        self.config = None
        self.auxiliary = None
        self._resources = []
        self._lock = threading.RLock()
        self._refresh_fingerprint()

    def _refresh_fingerprint(self):
        files = [*self.directory.glob("*.py"), Path(__file__), Path(__file__).with_name("auxiliary.py"), Path(__file__).parent / "methods/figstep/attack.py"]
        self.fingerprint = fingerprint({
            "config": {k:v for k,v in self.snapshot.items() if k != "runtime"},
            "font": sha256_file(self.font_path), "pillow": PIL.__version__,
            "code": {str(p.relative_to(Path(__file__).parent)): sha256_file(p) for p in files},
        })

    def bind(self, config):
        self.config = config
        if self.uses_auxiliary:
            models = model_settings(config, self.snapshot["auxiliary"])
            self.snapshot["resolved_auxiliary"] = [m.public_dict() for m in models]
            self.auxiliary = Auxiliary(models, self.snapshot["auxiliary"])
        if self.uses_image_model:
            from ..config import _redact_mapping
            self.snapshot["resolved_image"] = {"backend": config.image_backend, **_redact_mapping(config.active_image)}
        self._refresh_fingerprint()
        return self

    def rng(self, source, salt=""):
        return random.Random(fingerprint([self.parameters.get("seed", 42), source.source_id, source.benchmark["question"], salt]))

    def prepare(self, source, sid, output_dir, stage, system, validator, *, user=None):
        if self.auxiliary is None:
            raise ValueError(f"{self.name} needs pipeline model settings")
        user = source.benchmark["question"] if user is None else user
        key = fingerprint([self.fingerprint, source.benchmark["question"], stage, system, user])
        path = output_dir / "preparation" / f"{sid}-{stage}.json"
        try:
            cached = load_json_if_exists(path, {})
            if cached.get("fingerprint") == key and cached.get("sha256") == fingerprint(cached["data"]):
                return cached["data"], cached["metadata"]
        except (KeyError, ValueError, OSError):
            pass
        data, metadata = self.auxiliary.request(system, user, validator)
        atomic_write_json(path, {"fingerprint": key, "data": data, "sha256": fingerprint(data), "metadata": metadata})
        return data, metadata

    def result(self, image, prompt, sid, output_dir, metadata):
        return [AugmentationResult(prompt, (save_image(image, output_dir / "images" / f"{sid}.png"),), metadata)]

    def close(self):
        for resource in self._resources:
            resource.close()
        self._resources.clear()
