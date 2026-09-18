from pathlib import Path

from PIL import Image

from ...base import fingerprint
from ....io_utils import atomic_write_json, load_json_if_exists, sha256_file


class ClipRetrieval:
    def __init__(self, settings, paths):
        self.settings, self.paths = settings, paths
        self.model = self.processor = None

    def _load(self):
        if self.model is None:
            from transformers import CLIPModel, CLIPProcessor
            self.model = CLIPModel.from_pretrained(self.settings["clip_path"], local_files_only=True).to(self.settings["device"]).eval()
            self.processor = CLIPProcessor.from_pretrained(self.settings["clip_path"], local_files_only=True)

    def encode(self, values, *, images=False):
        import torch
        self._load()
        with torch.inference_mode():
            inputs = self.processor(**({"images":values} if images else {"text":values}), return_tensors="pt", padding=True, truncation=True)
            inputs = inputs.to(self.settings["device"])
            result = self.model.get_image_features(**inputs) if images else self.model.get_text_features(**inputs)
            if not isinstance(result, torch.Tensor):
                result = result.pooler_output
            return torch.nn.functional.normalize(result.float(), dim=-1).cpu()

    def select(self, question, output_dir, resource_key):
        import torch
        path = output_dir / "resources" / "clip_embeddings.json"
        try:
            cache = load_json_if_exists(path, {})
            rows = cache.get("embeddings", [])
            if cache.get("fingerprint") != resource_key or cache.get("sha256") != fingerprint(rows) or len(rows) != len(self.paths):
                rows = []
        except (ValueError, OSError):
            rows = []
        if not rows:
            for start in range(0, len(self.paths), 8):
                batch = []
                for file in self.paths[start:start+8]:
                    with Image.open(file) as image:
                        batch.append(image.convert("RGB"))
                rows.extend(self.encode(batch, images=True).tolist())
            atomic_write_json(path, {"fingerprint": resource_key, "embeddings": rows, "sha256": fingerprint(rows)})
        embeddings = torch.tensor(rows, dtype=torch.float32)
        selected = [self.encode([question])[0]]
        indices = []
        for _ in range(self.settings["max_pairs_per_question"]):
            scores = (torch.stack(selected) @ embeddings.T).mean(dim=0)
            scores[indices] = float("inf")
            index = int(scores.argmin())
            indices.append(index)
            selected.append(embeddings[index])
        return [self.paths[i] for i in indices]

    def close(self):
        self.model = self.processor = None
