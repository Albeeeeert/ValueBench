# 数据契约

## 场景文件

每个 JSON 表示一个场景，可包含多个机制元素：

```json
{
  "schema_version": "scenario-element-plan-v1",
  "scenario_id": "row001_s01",
  "source_path": "by_dimension/dignity/example/scenarios.json",
  "category_path_en": ["Dignity", "Dimension", "Subdimension", "Boundary"],
  "title": "Readable title",
  "source_text": "English scenario boundary.",
  "scenario_type": "enumerated",
  "elements": [
    {
      "element_id": "stable_element_id",
      "label": "Readable mechanism",
      "target_axis": "affected value axis",
      "risk_pattern": "abstract risk mechanism",
      "bias_surfaces": ["observable surface"],
      "constraints": {}
    }
  ]
}
```

## 场景 manifest

统一字段为 `canonical_relative_path`，相对于配置中的 scenario element 目录：

```json
{
  "schema_version": "value-to-scenario-manifest-v1",
  "valid": true,
  "entries": [
    {
      "scenario_id": "row001_s01",
      "canonical_relative_path": "row001_s01.json",
      "sha256": "...",
      "element_count": 8
    }
  ]
}
```

加载器拒绝逃逸输入根目录的相对路径，并在提供 SHA256 时校验内容。

## Benchmark 与回应

Benchmark 文件按 HH/BH profile 保存。每个条目包含 `benchmark_id`、`template_id`、问题、
随机化选项、正确角色、图片描述、`shared_image_id/role`、`risk_combination_type`、
`modality_risk_labels`、完整 `risk_audit`、`visual_evidence_mode`、可见文字估计、attempt 和
generation trace。通过硬结构校验的 `quality_status` 固定为 `structural_pass`；所有文本
关键词或正则扫描结果均作为非阻断提示单独保留在 `program_lints`。HH/BH 的 rationale
固定为空，以 `risk_audit` 作为唯一证据记录。

`both` 模式中，同一 `(scenario_id, element_id, profile)` 必须恰好有一条 awareness 和一条
instruction，两者 `image_description`、视觉模式和目标场景一致。单风格模式中每个机制只有
所选题型，且不包含共享图片元数据。最终验收拒绝缺失条目、重复问题、配额偏差和失败 job；
非阻断 lint 本身不使分片失败。

回应采用 JSONL，每行对应一次目标请求，核心字段包括 `sample_key`、`benchmark_id`、
`mode`、目标模型公开配置、图片 SHA256、`status`、`response`、`reasoning_content`、usage
和 request ID。项目不生成 judgment 或 score 字段。

所有 `source_path`、`source_benchmark` 和 `image_path` 均使用项目或 run 内相对路径。

## 增强样本

增强不改写原始 BenchmarkItem。`augmentations/<method>/samples.jsonl` 每行是一个
`value-eval-augmentation-v1` 对象：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 固定为 `value-eval-augmentation-v1` |
| `sample_id` | 方法名与源题稳定标识组成的 ID；方法返回多个结果时追加序号 |
| `method` / `method_version` | 注册方法名及字符串形式的配置版本 |
| `source.profile` / `source.style` | 固定为 `hh` / `instruction` |
| `source.benchmark_id` | 源题 ID，用于跨方法和原题配对 |
| `source.benchmark_path` | 相对 run 根目录的源 benchmark 路径 |
| `source.benchmark` | 完整源题记录，包括原始选项、答案和风险审计 |
| `input.text` | 实际发送给目标模型的非空提示词 |
| `input.images` | 非空图片列表；每项包含 `path`、`sha256`、`width`、`height`、`format` |
| `metadata` | 方法专用转换和排版记录，不进入目标请求 |

以下为中性源题的结构示意，ID、哈希和 metadata 已简化：

```json
{
  "schema_version": "value-eval-augmentation-v1",
  "sample_id": "qr-example",
  "method": "qr",
  "method_version": "1",
  "source": {
    "profile": "hh",
    "style": "instruction",
    "benchmark_id": "example",
    "benchmark_path": "benchmark/hh/benchmark.json",
    "benchmark": {
      "benchmark_id": "example",
      "question": "Describe three ways to organize a community library."
    }
  },
  "input": {
    "text": "Describe the activity named at the bottom of the image.",
    "images": [{
      "path": "augmentations/qr/images/qr-example.png",
      "sha256": "...",
      "width": 512,
      "height": 600,
      "format": "PNG"
    }]
  },
  "metadata": {"key_phrase": "community library"}
}
```

`input.images[].path` 相对 run 根目录，禁止绝对路径和目录逃逸；读取时校验哈希、尺寸与
图片格式。当前 11 种方法每条源题生成一个样本、一张最终 PNG，接口也支持多结果、多图片。
原始选项、答案和风险审计只属于 source，HH 标签不构成对增强输入的重新判定。

### 方法 metadata

| 方法 | 主要字段 |
| --- | --- |
| `figstep` | 排版文字、实际字号、行距、文字边界和缩字号情况 |
| `qr` | `key_phrase`、`phrase_type`、`rephrased_question`、`typography`、`source_image_size` |
| `camo` | `keywords`、`masked_text`、`math_questions`、`image_clues`、`layout` |
| `mml_wr` | `replaced_prompt`、`restoration_map`、`scrambled_original_words`、`rendered_text`、`font_size` |
| `mml_mirror` / `mml_rotate` | `scrambled_original_words`、`rendered_text`、`font_size` |
| `himrd` | `harmful_phrase`、`textual_part`、`visual_part`、`image_prompt`、`typography` |
| `cs_dj` | `sub_questions`、`distraction_images`、`text_layouts`、两类面板数量 |
| `visual_roleplay` | `role_plan`、`role_panel`、`request_panel`、`portrait_size`、`image_backend` |
| `si` | 抽词字段、`block_order`、`prompt_word_order_shuffled`、`typography` |
| `viscra` | 抽词字段、`mask_box`、`attention_grid_size`、`attention_block_size`、`attention_image_size`、注意力查询和后端 |

使用辅助 API 的七种方法另有 `auxiliary`，包含 `model`、`fallback_used` 和 `attempts`。
每次尝试保存模型、尝试序号、状态；失败尝试记录错误类型。它们是转换过程记录，不是评测分数。
VisCRA 的 `mask_box` 为原图上的 `[left, top, right, bottom]`，右、下边界不包含在遮挡区内；
尺寸数组按 `[width, height]`，注意力网格按 `[columns, rows]`。

## 增强 checkpoint 与准备缓存

`augmentations/<method>/manifest.json` 使用 `value-eval-augmentation-manifest-v1`：

| 字段 | 含义 |
| --- | --- |
| `method_fingerprint` | 当前方法实现、参数和资源标识的指纹 |
| `source_count` / `selected_source_count` | 全部 HH-instruction 源题数 / 本次选中源题数 |
| `completed_source_count` / `sample_count` | 已完成源题数 / 已导出增强样本数 |
| `status` | `building`、`completed`、`partial` 或 `failed` |
| `counts.generated/resumed/failed` | 本次所选源题的新生成、复用和失败数量 |
| `entries` | 以源 `benchmark_id` 为键的 checkpoint |
| `samples_sha256` | 导出 `samples.jsonl` 的完整文件哈希 |

成功 entry 包含 `status: completed`、源题与方法联合 `fingerprint` 和 `samples`；
失败 entry 保存 `status: failed`、指纹和 `error`。导出按源题顺序排列。
本次有失败时方法状态为 `failed`；无失败但源题覆盖未齐为 `partial`；全量完成为 `completed`。

`config.snapshot.yaml` 保存该方法配置与解析后的公开模型设置。辅助准备写入
`preparation/<sample_id>-<stage>.json`，包含 `fingerprint`、`data`、`sha256` 和 `metadata`；
VisualRoleplay 另保存角色图及校验记录。CS-DJ 的
`resources/clip_embeddings.json` 保存向量缓存及其指纹、哈希。这些中间结果不计作独立样本。

增强输入路径和源 benchmark 路径保持相对路径；方法快照与 metadata 中的外部图库、
CLIP 或注意力模型路径可能为绝对路径，迁移资源后应更新方法配置并重新生成对应增强。

## 完整数据集清单

`dataset/manifest.json` 使用 `value-eval-dataset-manifest-v1`，保存 `run_id`、
`status`、`sample_count`、`ready_count`、`subsets` 和 `updated_at`。
`subsets.base` 记录原题数量、已就绪原图数量、`benchmark_files` 与 `image_manifests`；
每个已启用增强子集记录 `samples`、`manifest`、数量与状态。

原始集缺图时为 `partial`；增强子集只有通过完整来源及文件校验才为 `completed`，
否则为 `unavailable` 并保存 `error`、数量记为 0。总清单全部子集就绪才为 `completed`，
否则为 `partial`。总数量不包括不可用增强子集中的部分结果；这些结果仍保存在方法目录中。

关闭的方法不列入当前清单。全部成功时，原始集 N 条、HH-instruction H 条、启用 M 种
当前方法，完整集为 `N + H × M` 条。该清单是索引，不将各方法 JSONL 合并成一个文件。

## 增强回应

增强仅支持 `image_text`，保存到
`responses/<model>/image_text/augmentations/<method>.jsonl`。沿用
`value-eval-response-v1`，其中 `benchmark_id` 为增强 `sample_id`，`dataset` 和 `method`
为方法名，`source_benchmark_id` 为原题 ID。原始回应的 `dataset` 为 `base`，`method`
为空字符串，`source_benchmark_id` 等于本条 `benchmark_id`。

回应缓存键区分 run、profile、数据集、样本、目标模型名、mode、目标文字与图片哈希。
相同输入重跑可复用，变化后追加新回应记录；JSONL 保留历史记录，分析时应按 `sample_key`
匹配当前输入。采集前要求所选增强方法全量完成，即使 `--max-items 1` 也不能跳过此校验。
具体命令见 [数据增强](AUGMENTATION.md#回应采集)。
