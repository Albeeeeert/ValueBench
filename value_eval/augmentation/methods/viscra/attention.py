from PIL import ImageDraw


def preprocess_source_image(processor, scene):
    """Keep source resolution, allowing only Qwen's patch-grid alignment."""
    factor = processor.patch_size * processor.merge_size
    width = max(factor, round(scene.width / factor) * factor)
    height = max(factor, round(scene.height / factor) * factor)
    inputs = processor(
        images=[scene], return_tensors="pt",
        min_pixels=factor * factor, max_pixels=width * height,
    )
    grid = inputs["image_grid_thw"][0].tolist()
    if grid != [1, height // processor.patch_size, width // processor.patch_size]:
        raise ValueError("VisCRA image processor changed resolution beyond patch alignment")
    return inputs


def last_token_image_attention(model, inputs, layer):
    """Read the selected eager layer without retaining all layers' attention matrices."""
    import torch
    image_indices = torch.where(inputs["input_ids"][0] == model.config.image_token_id)[0]
    if not image_indices.numel():
        raise ValueError("VisCRA input has no image tokens")
    start, end = int(image_indices[0]), int(image_indices[-1]) + 1
    decoder = getattr(model.model, "language_model", model.model)
    target = decoder.layers[layer].self_attn
    captured = []

    def request_weights(module, args, kwargs):
        # Older Transformers versions suppress weights unless explicitly requested.
        kwargs["output_attentions"] = True
        return args, kwargs

    def capture(module, args, output):
        if len(output) < 2 or output[1] is None:
            raise ValueError("VisCRA requires eager attention tensors")
        # Integer bounds also work when Accelerate places this layer on another GPU.
        captured.append(output[1][0, :, -1, start:end].mean(0).float().cpu())

    pre = target.register_forward_pre_hook(request_weights, with_kwargs=True)
    post = target.register_forward_hook(capture)
    try:
        with torch.no_grad():
            model(**inputs, output_attentions=False, use_cache=False, return_dict=True)
    finally:
        pre.remove()
        post.remove()
    if len(captured) != 1:
        raise ValueError("VisCRA expected exactly one attention capture")
    return captured[0]


def mask_top_window(scene, matrix, parameters):
    """Select the maximum-attention window and map its box onto the original image."""
    import numpy as np
    matrix = np.asarray(matrix, dtype=np.float32).copy()
    rows, cols = matrix.shape
    resolution = f"{scene.width}x{scene.height}"
    size = parameters.get("attention_block_size_by_resolution", {}).get(resolution, parameters["attention_block_size"])
    stride = parameters["attention_stride"]
    if type(size) is not int or type(stride) is not int or size < 1 or stride < 1 or size > min(rows, cols):
        raise ValueError("VisCRA attention grid cannot fit the configured block")
    if parameters["zero_side_columns"]:
        matrix[:, (0, -1)] = 0
    trim = parameters["zero_top_bottom_rows"]
    if type(trim) is not int or trim < 0 or trim*2 >= rows:
        raise ValueError("VisCRA row filter must leave interior rows")
    if trim:
        matrix[:trim] = matrix[-trim:] = 0
    if not np.isfinite(matrix).all():
        raise ValueError("VisCRA attention contains non-finite values")
    integral = np.pad(np.cumsum(np.cumsum(matrix, axis=0), axis=1), ((1,0),(1,0)))
    candidates = []
    for row in range(0, rows-size+1, stride):
        for col in range(0, cols-size+1, stride):
            score = integral[row+size,col+size]-integral[row,col+size]-integral[row+size,col]+integral[row,col]
            candidates.append((-float(score), row, col))
    negative_score, row, col = min(candidates)
    box = [round(col*scene.width/cols), round(row*scene.height/rows),
           round((col+size)*scene.width/cols), round((row+size)*scene.height/rows)]
    masked = scene.copy()
    ImageDraw.Draw(masked).rectangle((box[0], box[1], box[2]-1, box[3]-1), fill=parameters["mask_color"])
    filters = []
    if parameters["zero_side_columns"]:
        filters.append("zero_side_columns")
    if trim:
        filters.append(f"zero_top_bottom_{trim}_rows")
    return masked, {"mask_box":box, "attention_grid_size":[cols,rows], "mask_attention_score":-negative_score,
                    "attention_block_size":size,
                    "attention_spatial_filter":"+".join(filters) or "none"}


class AttentionModel:
    def __init__(self, parameters):
        self.parameters = parameters
        self.model = self.processor = None
        self.tokenizer = None

    def mask(self, scene, phrase):
        import torch
        from transformers import AutoTokenizer, Qwen2VLImageProcessor, Qwen2_5_VLForConditionalGeneration
        p = self.parameters
        if self.model is None:
            # Load only still-image processing; AutoProcessor in transformers 5
            # additionally requires a video backend even for image-only inputs.
            self.processor = Qwen2VLImageProcessor.from_pretrained(p["attention_model_path"], local_files_only=True)
            self.tokenizer = AutoTokenizer.from_pretrained(p["attention_model_path"], local_files_only=True)
            placement = {}
            if p["device"] == "cuda":
                # Preserve the original loader's layer sharding across visible GPUs.
                count = torch.cuda.device_count()
                placement = {"device_map":"balanced" if count > 1 else "auto",
                             "max_memory":{i:"8GiB" for i in range(count)} if count > 1 else None}
            self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
                p["attention_model_path"], local_files_only=True,
                torch_dtype=torch.bfloat16 if p["device"] != "cpu" else torch.float32,
                # Only the decoder attention weights are used for mask selection.
                attn_implementation={"vision_config":"sdpa", "text_config":"eager", "":"eager"}, **placement,
            ).eval()
            if not placement:
                self.model.to(p["device"])
        query = f"Which regions in the image show {phrase}?" + (" Answer:" if p["attention_add_answer"] else "")
        messages = [{"role":"user", "content":[{"type":"image"},{"type":"text","text":query}]}]
        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        image_inputs = preprocess_source_image(self.processor, scene)
        grid = image_inputs["image_grid_thw"][0].tolist()
        merge = self.processor.merge_size
        image_tokens = grid[0]*grid[1]*grid[2] // (merge*merge)
        if text.count("<|image_pad|>") != 1:
            raise ValueError("VisCRA chat template must contain one image placeholder")
        text = text.replace("<|image_pad|>", "<|image_pad|>"*image_tokens)
        token_inputs = self.tokenizer([text], padding=True, return_tensors="pt", add_special_tokens=False)
        inputs = {k:v.to(self.model.device) for k,v in {**token_inputs, **image_inputs}.items()}
        attention = last_token_image_attention(self.model, inputs, p["attention_layer"])
        rows, cols = grid[1]//merge, grid[2]//merge
        if grid[0] != 1 or attention.numel() != rows*cols:
            raise ValueError("VisCRA image-token attention shape mismatch")
        image, meta = mask_top_window(scene, attention.reshape(rows, cols).numpy(), p)
        meta.update({"attention_user_text":query, "attention_model":p["attention_model_path"],
                     "attention_image_size":[grid[2]*self.processor.patch_size, grid[1]*self.processor.patch_size],
                     "attention_prompt_source":"key_phrase", "vision_attention_backend":"sdpa",
                     "text_attention_backend":"eager", "attention_capture":"selected_layer"})
        return image, meta

    def close(self):
        self.model = self.processor = self.tokenizer = None
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
