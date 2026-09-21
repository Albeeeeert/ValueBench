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

回应中的 `source_benchmark`、`image_path` 相对 run 根目录；图片任务的 `image_path`
相对其图片 manifest 所在目录。合并 benchmark 的 ID 带 profile 前缀，与原题回应关联时使用
合并文件的 `(profile, source_benchmark_id)` 对应回应的 `(profile, benchmark_id)`。

## 图片任务

图片 manifest 记录 `backend`、`generation_settings`、`generation_fingerprint` 和任务计数。
任务包含 `seed`（本地实际种子）、`effective_prompt`（实际提交描述）及 `moderation_retry`。
审核恢复包含原始请求 `original`、视觉事实 `anchors`、候选及校验 `rewrites`、预算耗尽标记
`exhausted`；原始 `prompt` 保留。API 后端保存审核恢复记录，本地后端记录本地生成参数。

## 越狱样本与数据集

`augmentations/<method>/samples.jsonl` 每行一个 `value-eval-augmentation-v1` 对象：

| 字段 | 含义 |
| --- | --- |
| `sample_id` / `method` / `method_version` | 稳定样本 ID、方法与版本 |
| `source.profile` / `source.style` | 固定为 hh / instruction |
| `source.benchmark_id` / `source.benchmark_path` | 原题 ID / 相对 run 根目录的来源路径 |
| `source.benchmark` | 完整原题，包括选项、答案和审计 |
| `input.text` / `input.images` | 实际目标输入；图片项含 path、sha256、width、height、format |
| `metadata` | 转换、排版、辅助请求及本地算法记录，不进入目标请求 |

越狱图片路径相对 run 根目录，读取时校验哈希、尺寸和格式。当前每方法每源题输出一个样本。
方法 manifest 包含 `status`、`method_fingerprint`、`source_count`、`completed_source_count`、
`sample_count`、`entries` 与 `samples_sha256`；状态有 building、completed、partial、failed。
`config.snapshot.yaml` 保存方法配置，`preparation/`、`resources/` 保存准备缓存。

`dataset/manifest.json` 索引原始集及已启用方法，不复制合并样本文件。全量就绪为 completed，
否则为 partial；不可用越狱子集为 unavailable，其部分结果不计入总数。
全部成功时总数为“原题数 + HH-instruction 数 × 方法数”，未启用越狱方法时可没有该索引。

越狱样本的回应保存到 `responses/<model>/image_text/augmentations/<method>.jsonl`，
其 `benchmark_id` 为越狱样本的 `sample_id`，`dataset/method` 为方法名，`source_benchmark_id` 指向原题。
原始回应 `dataset` 为 base、method 为空，source_benchmark_id 等于原始 benchmark_id。

## 回应历史与验收

JSONL 追加保存失败、重试和强制重采历史，行数不等于有效样本数。按当前输入 `sample_key`
筛选批次；同一键需要最新成功结果时，取最后一条 completed 记录。
`responses/manifest.json` 只记录最近一次采集，应满足 failed、skipped 均为 0，且
`completed + resumed == sample_count`。图片应满足 `completed_count == task_count`、
`failed_count == 0`；越狱方法与总数据集应完整。总 run 的 completed 只表示编排正常结束，
不能替代这些逐阶段检查。
