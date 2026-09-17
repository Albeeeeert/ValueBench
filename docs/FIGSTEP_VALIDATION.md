# FigStep 接入验证

## 首次接入验证（760×760，历史记录）

2026-09-18 使用 `configs/excel_to_benchmark_figstep_check.yaml` 完成真实全流程验证。
该配置从 Excel 配置的修改版复制，保留 HH/BH × awareness/instruction，使用一行 Excel，
每个 profile 最多 4 条题，开启 `augmentation: {enabled: true, method: [figstep]}`。

| 验证项 | 结果 |
| --- | --- |
| 离线测试 | 65 项全部通过 |
| 四类原题 | HH-awareness、HH-instruction、BH-awareness、BH-instruction 各 2 条 |
| 原始图片 | 本地 Qwen-Image 生成 4 张 512×512 共享图片 |
| FigStep | 仅增强 2 条 HH-instruction，输出 2 张 760×760 PNG |
| 完整数据集 | 10 条，全部就绪 |
| 真实回应 | qwen3-vl-8b-thinking 完成 10 条，失败/跳过均为 0 |
| 独立补做 | 2 条全部复用，新增生成 0 条 |
| 回应恢复 | 10 条全部复用，新增请求 0 条 |
| 原始产物保护 | 复跑前后 13 个原题、图片及相关文件的 SHA256 和修改时间一致 |
| 输出层级 | figstep 下仅 images 子目录，没有指纹目录 |

独立预检、补做与回应恢复均在移除两项 API Key 并指定不存在的 env 文件后通过。
回应逐条核对当前输入的 sample_key；增强提示词只包含方法提示，不额外发送原题选项或答案。
离线测试还覆盖缺少目标类别、关闭方法、字体缺失、溢出、缓存损坏、参数/源题变化、部分构建、
单 instruction 模式、原图依赖和错误恢复。当时的 FigStep 默认渲染与既有实现通过逐像素比对。

真实运行发现并修复了公共客户端对 HTTP 200 正常正文的错误分类：正常回答中出现
unauthorized、account balance 等词不再被当作 API 鉴权/计费错误。新增回归测试覆盖此行为。

运行目录：`outputs/augmentation_excel_to_benchmark_figstep_verified/`。

- `run_manifest.json`：完整 run-all 成功记录。
- `dataset/manifest.json`：原始集与增强集的统一入口。
- `augmentations/figstep/samples.jsonl`：增强图文及原题来源。
- `responses/qwen3-vl-8b-thinking/image_text/augmentations/figstep.jsonl`：FigStep 原始回应。
- `validation_report.json`：数量、当前输入对应关系和恢复检查的机器可读结果。

本次验证证明工程链路和恢复逻辑可用；未进行自动评分或难度提升判定。

## 512×512 与自动排版验证（历史记录）

同日将默认画布调整为 512×512，按字体实际像素宽度换行，并在 10–18 号之间选择
能容纳完整问题和全部编号的最大字号。行间距随字号缩小；最小字号仍放不下时明确报错。

- 67 项离线测试全部通过，覆盖长文本缩小字号、长词拆行、段落保留、实际像素边界、
  完整文字与编号、最小字号溢出，以及关闭自动排版后的旧版逐像素兼容。
- 使用独立 `generate-augmentations` 命令重生成现有 2 张 FigStep 图片，均为 512×512，
  均以 18 号字体完整显示；中性长文本示例自动缩小到 13 号。
- 重生成前后，核对的 11 个原始 benchmark、图片和图片清单文件的 SHA256 及修改时间一致。
- 离线核对回应缓存：8 条原始数据的回应可复用；2 条 FigStep 数据因图片变化需重新采集。
  本次未请求模型；上节的 10 条真实回应及 `validation_report.json` 属于原 760×760 版本。

该次排版验证记录见运行目录下的 `figstep_layout_validation.json`。

## 恢复 760×760，保留自动排版（当前配置）

按要求将默认尺寸恢复为 760×760，保留自动换行和 10–18 的字号适配。
原实验 `value_figstep_config.yaml` 使用固定 `font_size: 18`、`wrap_width: 70`，
未做自动缩小；当前优先使用 18，只有完整内容放不下时才减小字号。

13 项增强模块测试全部通过。验证 run 中的 2 张 FigStep 图片已重新生成并检查像素边界，
均为 760×760、实际字号 18。本次未重新采集模型回应。
当前排版验证记录为 `figstep_layout_760_validation.json`。
