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

增强不改写原始 BenchmarkItem。`augmentations/<method>/samples.jsonl` 每行独立保存：

- `sample_id`、`method`、`method_version`：方法和增强样本标识。
- `source`：源 profile/style、benchmark 相对路径及源题记录。
- `input.text`：实际目标提示词。
- `input.images`：图片的 run-relative 路径、SHA256、尺寸、格式。
- `metadata`：方法专用信息，例如 FigStep 的排版文字及文字边界。

原始选项、答案和风险审计只属于 source，不直接赋予增强输入。完整数据集清单位于
`dataset/manifest.json`，分别记录原始集和当前启用方法的路径、数量与就绪状态。
增强回应增加 `dataset`、`method`、`source_benchmark_id`，单独保存到
`responses/<model>/<mode>/augmentations/<method>.jsonl`。具体规则见 [数据增强](AUGMENTATION.md)。
