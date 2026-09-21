from io import BytesIO
import json

from PIL import Image

from ...base import fingerprint
from ...common import ConfiguredMethod, text_panel, stack, save_image
from ...auxiliary import parse_json
from ....image_generation.runner import ImageGenerator, probe_image
from ....image_generation.client import ImageModerationError
from ....io_utils import atomic_write_json, load_json_if_exists, sha256_file
from ....schemas import ImageTask
from .prompts import ROLE_SYSTEM_PROMPT
from .validation import _validate_role_plan, _build_attack_prompt


class Method(ConfiguredMethod):
    name = "visual_roleplay"
    uses_auxiliary = True
    uses_image_model = True

    def portrait(self, plan, sid, output_dir):
        key = fingerprint([self.fingerprint, plan["visual_prompt"]])
        path = output_dir / "preparation" / f"{sid}-portrait.png"
        record = path.with_suffix(".json")
        try:
            previous = load_json_if_exists(record, {})
            if previous.get("fingerprint") == key and sha256_file(path) == previous.get("sha256"):
                with Image.open(path) as img:
                    return img.convert("RGB")
        except (ValueError, OSError):
            pass
        with self._lock:
            if not self._resources:
                self._resources.append(ImageGenerator(self.config))
            task = ImageTask(sid, plan["visual_prompt"], [sid], "visual_roleplay")
            if previous.get("fingerprint") == key and isinstance(previous.get("task"), dict):
                saved = previous["task"]
                if saved.get("prompt") == task.prompt:
                    for field in ("attempts", "request_ids", "effective_prompt", "moderation_retry", "status", "error"):
                        if field in saved:
                            setattr(task, field, saved[field])

            def checkpoint():
                atomic_write_json(record, {"fingerprint": key, "task": task.as_dict()})

            try:
                data = self._resources[0].generate_bytes(task, checkpoint)
            except Exception as exc:
                task.status = "moderated" if isinstance(exc, ImageModerationError) else "failed"
                task.error = str(exc)
                checkpoint()
                raise
        _, width, height = probe_image(data)
        expected = tuple(map(int, self.config.active_image["size"].split("*")))
        if (width, height) != expected:
            raise ValueError(f"portrait size {(width, height)} does not match image backend {expected}")
        with Image.open(BytesIO(data)) as img:
            portrait = img.convert("RGB")
        save_image(portrait, path)
        task.status = "generated"
        atomic_write_json(record, {"fingerprint": key, "sha256": sha256_file(path), "size": list(portrait.size),
                                   "task": task.as_dict()})
        return portrait

    def generate(self, source, sample_id, output_dir):
        plan, auxiliary = self.prepare(source, sample_id, output_dir, "role", ROLE_SYSTEM_PROMPT,
                                       lambda text: _validate_role_plan(parse_json(text), source.benchmark["question"]),
                                       user=json.dumps({"request": source.benchmark["question"]}, ensure_ascii=False))
        portrait = self.portrait(plan, sample_id, output_dir)
        p = self.parameters
        common = {k:p[k] for k in ("reference_width", "padding", "spacing")}
        role = f"{plan['character_name']} — {plan['role_title']}. {plan['role_description']}"
        top, top_meta = text_panel("ROLE\n" + role, portrait.width, self.font_path, font_size=p["role_font_size"], bg="#1F2633", fg="white", **common)
        bottom, bottom_meta = text_panel("REQUEST\n" + source.benchmark["question"], portrait.width, self.font_path, font_size=p["request_font_size"], **common)
        return self.result(stack(top, portrait, bottom), _build_attack_prompt(), sample_id, output_dir,
                           {"role_plan": plan, "auxiliary": auxiliary, "role_panel": top_meta, "request_panel": bottom_meta,
                            "portrait_size": list(portrait.size), "image_backend": self.config.image_backend})
