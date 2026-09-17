from __future__ import annotations

import os
import tempfile
import textwrap
from functools import lru_cache
from pathlib import Path

import PIL
import yaml
from PIL import Image, ImageColor, ImageDraw, ImageFont

from ...base import AugmentationMethod, AugmentationResult, AugmentationSource, fingerprint
from ....io_utils import sha256_file


def _wrap_pixels(text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    """Wrap at word boundaries, splitting an oversized word without dropping text."""
    @lru_cache(maxsize=4096)
    def fits(value: str) -> bool:
        left, _, right, _ = font.getbbox(value)
        return max(font.getlength(value), right - left) <= width

    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if fits(candidate):
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            while word and not fits(word):
                low, high = 0, len(word)
                while low < high:
                    middle = (low + high + 1) // 2
                    if fits(word[:middle]):
                        low = middle
                    else:
                        high = middle - 1
                if low == 0:
                    raise ValueError("a character is wider than the available canvas")
                lines.append(word[:low])
                word = word[low:]
            line = word
        lines.append(line)
    return "\n".join(lines)


class FigStep(AugmentationMethod):
    name = "figstep"
    requires_original_image = False

    def __init__(self, config_path: Path | None = None) -> None:
        path = config_path or Path(__file__).with_name("config.yaml")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("name") != self.name:
            raise ValueError("invalid FigStep config")
        self.snapshot = raw
        self.version = str(raw["version"])
        p = raw["parameters"]
        required = {"font_path", "font_size", "wrap_width", "image_width", "image_height",
                    "margin_x", "margin_y", "spacing", "steps", "bg", "fg", "attack_prompt"}
        optional = {"auto_fit", "min_font_size"}
        if not isinstance(p, dict) or not required <= set(p) or set(p) - required - optional:
            raise ValueError(f"FigStep requires {', '.join(sorted(required))}; optional: auto_fit, min_font_size")
        for name in ("font_size", "image_width", "image_height", "steps", "margin_x", "margin_y", "spacing", "wrap_width"):
            minimum = 1 if name in {"font_size", "image_width", "image_height", "steps"} else 0
            if type(p[name]) is not int or p[name] < minimum:
                raise ValueError(f"FigStep {name} must be an integer >= {minimum}")
        # Legacy method configs retain their fixed layout unless explicitly enabled.
        p = {"auto_fit": False, "min_font_size": min(10, p["font_size"]), **p}
        self.parameters = raw["parameters"] = p
        if not isinstance(p["auto_fit"], bool):
            raise ValueError("FigStep auto_fit must be true or false")
        if type(p["min_font_size"]) is not int or not 1 <= p["min_font_size"] <= p["font_size"]:
            raise ValueError("FigStep min_font_size must be between 1 and font_size")
        if p["image_width"] <= 2 * p["margin_x"] or p["image_height"] <= 2 * p["margin_y"]:
            raise ValueError("FigStep margins must leave a positive drawing area")
        self.concurrency = raw.get("runtime", {}).get("concurrency", 8)
        if type(self.concurrency) is not int or self.concurrency < 1:
            raise ValueError("FigStep runtime.concurrency must be a positive integer")
        self.font_path = (path.parent / p["font_path"]).resolve()
        ImageFont.truetype(str(self.font_path), p["font_size"])
        for name in ("bg", "fg"):
            ImageColor.getrgb(p[name])
        numbers = [str(i) for i in range(1, p["steps"] + 1)]
        numbering = numbers[0] if len(numbers) == 1 else (
            " and ".join(numbers) if len(numbers) == 2 else ", ".join(numbers[:-1]) + ", and " + numbers[-1]
        )
        self.prompt = str(p["attack_prompt"]).format(numbers=numbering)
        if not self.prompt.strip():
            raise ValueError("FigStep attack_prompt must not be empty")
        self.fingerprint = fingerprint({
            "method": self.name, "version": self.version, "parameters": p,
            "font_sha256": sha256_file(self.font_path), "pillow": PIL.__version__,
            "implementation_sha256": sha256_file(Path(__file__)),
        })

    def _layout(self, question: str, draw: ImageDraw.ImageDraw) -> tuple:
        p = self.parameters
        question = question.rstrip("\n")
        suffix = "".join(f"\n{i}. " for i in range(1, p["steps"] + 1))
        if not p["auto_fit"]:
            text = textwrap.fill(question, width=p["wrap_width"]) if p["wrap_width"] else question
            text += suffix
            font = ImageFont.truetype(str(self.font_path), p["font_size"])
            xy = (p["margin_x"], p["margin_y"])
            bbox = draw.multiline_textbbox(xy, text, font=font, spacing=p["spacing"])
            if min(bbox[:2]) < 0 or bbox[2] > p["image_width"] - p["margin_x"] or bbox[3] > p["image_height"] - p["margin_y"]:
                raise ValueError(f"FigStep text overflows canvas: bbox={bbox}; enable auto_fit or adjust method config")
            return text, font, xy, p["spacing"], bbox

        width = p["image_width"] - 2 * p["margin_x"]
        height = p["image_height"] - 2 * p["margin_y"]
        for size in range(p["font_size"], p["min_font_size"] - 1, -1):
            font = ImageFont.truetype(str(self.font_path), size)
            spacing = round(p["spacing"] * size / p["font_size"])
            try:
                text = _wrap_pixels(question, font, width) + suffix
            except ValueError:
                continue
            bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
            if bbox[2] - bbox[0] <= width and bbox[3] - bbox[1] <= height:
                xy = (p["margin_x"] - bbox[0], p["margin_y"] - bbox[1])
                bounds = draw.multiline_textbbox(xy, text, font=font, spacing=spacing)
                return text, font, xy, spacing, bounds
        raise ValueError(
            f"FigStep text overflows canvas even at min_font_size={p['min_font_size']}; "
            "increase canvas size or lower min_font_size in method config"
        )

    def generate(self, source: AugmentationSource, sample_id: str, output_dir: Path) -> list[AugmentationResult]:
        p = self.parameters
        img = Image.new("RGB", (p["image_width"], p["image_height"]), p["bg"])
        draw = ImageDraw.Draw(img)
        text, font, xy, spacing, bbox = self._layout(str(source.benchmark["question"]), draw)
        draw.text(xy=xy, text=text, spacing=spacing, font=font, fill=p["fg"])
        image_dir = output_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        destination = image_dir / f"{sample_id}.png"
        descriptor, temporary = tempfile.mkstemp(prefix=".figstep-", suffix=".png", dir=image_dir)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                img.save(handle, format="PNG")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return [AugmentationResult(self.prompt, (destination,), {
            "rendered_text": text, "text_bbox": list(bbox),
            "layout_mode": "auto_fit" if p["auto_fit"] else "fixed",
            "font_size": font.size, "line_spacing": spacing,
            "line_count": len(text.split("\n")), "font_size_reduced": font.size < p["font_size"],
        })]
