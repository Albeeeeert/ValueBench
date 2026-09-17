# HH-instruction 数据增强

完整运行结果见 [FigStep 接入验证](FIGSTEP_VALIDATION.md)。

所有方法只处理当前 run 的 `benchmark/hh/benchmark.json` 中
`scenario_question_style: instruction` 的题目。generation 仍可同时生成四类数据。

## 配置

```yaml
augmentation:
  enabled: true
  method: [figstep]
response:
  enabled: true
  mode: image_text
  datasets: [base, enabled_augmentations]
```

主配置只控制总开关和方法列表。省略配置、总开关关闭或空列表使 run-all 跳过增强。
方法名必须已注册且不重复；关闭的方法不自动加载历史产物。
run-all 在模型调用前检查生成范围包含 hh 和 instruction，生成后再校验实际源题。

方法参数在
[`value_eval/augmentation/methods/figstep/config.yaml`](../value_eval/augmentation/methods/figstep/config.yaml)。
默认图片 760×760、最大字号 18、最小字号 10、三个空编号。字体路径相对该配置目录。
`auto_fit: true` 按字体实际像素宽度自动换行，优先保持单词完整；超长单词按字符拆行。
若问题和全部编号的总高度超出可用区域，逐步减小字号并同比缩小行间距，使用能完整放下的
最大字号。缩至 min_font_size 仍不够时记录失败，不截断文字。metadata 记录实际字号、
行距、行数、文字边界及是否缩小字号。原始问题和默认提示词保持原有含义，`{numbers}` 随 steps 更新。

`wrap_width` 仅在 `auto_fit: false` 时控制固定字符数换行，可配合旧图片尺寸复现旧排版。
修改图片参数后运行 generate-augmentations 即可在原方法目录重建；图片变化会使对应旧回应
缓存失效，下次 collect-responses 会重新采集这些样本。

## 命令

完整 Excel 验证配置保留四类生成，每个 profile 最多 4 条，完整成功输出 8 条原题和
2 条 FigStep 样本。`response.enabled: false` 只跳过回应，不跳过增强。

```bash
CUDA_VISIBLE_DEVICES=0,1 .venv/bin/python -m value_eval run-all \
  --config configs/excel_to_benchmark_figstep_check.yaml

.venv/bin/python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --dry-run

.venv/bin/python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --method figstep

.venv/bin/python -m value_eval collect-responses \
  --config configs/excel_to_benchmark_figstep_check.yaml --dataset figstep
```

独立补做只检查实际 benchmark，不要求当前 YAML 的生成范围重现历史范围。配置须定位同一
execution_run_id；生成时使用的 `--style` 覆盖也需用于补做。FigStep 不需要原图、GPU 或
API Key，独立预检不读取 Excel 或探测模型。未启用任何方法却执行独立命令会报错。

省略 `--method` 执行所有开启的方法，重复该参数可选择多个方法，未来方法共用此入口。
`--max-items N` 按 HH-instruction 源题顺序限制本次任务；未覆盖全部源题时标记 partial，
去掉限制重跑可补齐。`--force` 只强制处理本次选中的方法和源题。

只修改增强开关时，推荐对已有 run 使用独立命令。原有 benchmark checkpoint 包含主 YAML
哈希，重新执行 benchmark/run-all 仍遵循原有配置一致性检查。

## 产物和恢复

```text
outputs/<execution_run_id>/
├── benchmark/
├── images/
├── augmentations/
│   └── figstep/
│       ├── images/<sample_id>.png
│       ├── samples.jsonl
│       ├── manifest.json
│       ├── config.snapshot.yaml
│       └── .generation.lock
└── dataset/manifest.json
```

没有指纹子目录。指纹仅存于 manifest，包含源题、方法实现/版本、渲染参数、字体内容和
Pillow 版本；并发数不影响图片缓存。方法独占写锁，图片和清单原子写入，逐条保存 checkpoint。
配置/源题变化、图片缺失或损坏时重建相关条目。旧的未引用图片不进入导出的样本清单。

增强样本包含 sample_id、method、method_version、source、input、metadata。source 保存原题
记录和来源，input.text 和 input.images 是实际模型输入，图片路径相对 run 根目录。
HH 风险标签仅作为来源信息，不作为增强输入的重新判定。

FigStep 每条源题输出一条样本。原始集 N 条、HH-instruction H 条时，完整集为 N+H 条。
dataset/manifest.json 汇总当前启用子集的路径、数量及就绪状态。FigStep 完成而原图未齐时
总清单仍为 partial；完整 run-all 若数据集未就绪会失败。

## 回应和新方法

response.datasets 默认包含 base 和 enabled_augmentations，可设为 [base] 或 [figstep]。
FigStep 只支持 image_text，不兼容模式在请求前报错。目标只收到文字图片和固定提示词，
原始 question 不重复进入文本消息，答案/选项/risk_audit 留在来源记录。
采集前检查配置、源题、图片和 JSONL，拒绝过期、损坏或未完成的增强集。

原始回应继续保存到 responses/<model>/<mode>/<profile>.jsonl，增强回应保存到
responses/<model>/<mode>/augmentations/<method>.jsonl，包含 dataset、method 和
source_benchmark_id，便于配对分析。项目只采集原始回应，不自动判分。

后续方法在 augmentation/methods/<name>/ 放置代码、config.yaml 和资源，并在 registry.py
注册。实现 AugmentationMethod，声明是否需要原图和支持的回应模式；generate 接收源样本、
稳定 ID 和输出目录，返回一个或多个 AugmentationResult，每个结果可包含多张图片。
方法原子保存图片，执行器统一负责筛选、校验、来源关联、恢复与汇总。
