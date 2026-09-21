# 配置字段手册

主配置的输入、输出和环境文件路径相对 `run.root`；`run.root` 本身相对 YAML 所在目录。
密钥字段只填写环境变量名，已有进程环境变量优先于 `.env`。

## run

| 字段 | 说明 |
| --- | --- |
| `id` | 本次实验 ID，同时是 `outputs/<id>` 目录名 |
| `root` | 项目根目录；配置位于 `configs/` 时通常为 `..` |
| `output_root` | 运行产物根目录 |
| `log_root` | 日志根目录 |
| `env_file` | dotenv 或 PowerShell 环境变量文件 |

## scenario_source

| 字段 | 说明 |
| --- | --- |
| `mode` | `excel` 从工作簿构建；`prepared` 使用已有场景 |
| `xlsx` / `sheet_name` | Excel 文件和工作表名称 |
| `chunk_by` | `level1` 按一级分类分块，或 `fixed_rows` 固定行数分块 |
| `chunk_size` | 每次 taxonomy 请求最多处理的行数 |
| `min_elements` | 每个场景期望的最少 elements 数 |
| `concurrency` | 场景 element 构建并发数 |
| `fallback_on_llm_error` | 单场景模型失败时是否生成需人工复核的启发式结果 |
| `debug_save_llm_io` | 是否保存 taxonomy 请求、原始响应及解析结果 |
| `*_model` | 对应 `models` 中的模型别名 |

## generation

`input_dir`、`input_manifest` 和可选 `input_selection` 用于 `prepared` 模式。Excel 模式下，
下游自动读取本次 run 发布的场景。`profiles` 只支持 `hh/bh`。`styles` 有三种合法配置：
`[awareness, instruction]`、`[awareness]`、`[instruction]`，命令行分别对应
`--style both|awareness|instruction`。`both` 要求 `share_image_across_styles: true`，且非零
`max_items_per_profile` 必须为偶数；单风格要求该共享开关为 false，数量可为任意非负整数。
`run.id` 是基础 ID；单风格输出和日志自动追加 `--awareness` 或 `--instruction`，避免三种
模式共享 checkpoint。分阶段运行时每个命令都要使用相同的 `--style`。

`planner_model` 和 `author_model` 是模型别名；`concurrency` 控制每个 profile 的机制级并发，
不是全局并发。多个 profile 会并行运行，例如 `[hh, bh]` 配合 `concurrency: 2` 的文本峰值
并发为 4；单独 `[hh]` 时为 2。`image.concurrency` 另有一个全局图片线程池，不计入这里；
`item_max_attempts` 控制单 item 局部修复次数，`shard_max_attempts` 控制每 200 个机制的
checkpoint 恢复次数，默认分别为 3 和 5。`direct_severe_harm` 和 `strict_validation` 必须
为真。planner/author 正式配置均使用
`max_tokens: 8000`。

通用 `prepared_scenarios.yaml` 和 `excel_to_benchmark.yaml` 显式配置为文本 2、图片 2；
`hh_bh_4000.yaml` 配置为文本 3、图片 3。本地图片并发固定为 1。
实际并发以运行配置为准，容量测量与调整见 [README](../README.md#并发设置)。

`variants_per_scenario` 控制每个场景、每个 profile 的独立生成任务数。默认值 0 表示
`all_elements_once` 行为；正整数表示固定场景级扩增，并允许大于该场景的 element 数。扩增时
程序按稳定顺序轮换场景内的 element，并把 `variant_index` 写入 ID、trace、checkpoint 和
恢复指纹。最终 item 数计算为：

```text
场景数 × variants_per_scenario × profile 数 × style 数
```

`max_items_per_profile` 是所有场景合计后的每 profile 总 item 上限，主要用于小样本调试；它
不是每场景数量。正式使用 `variants_per_scenario` 时建议设为 0，避免截断最后一部分场景。
变体数量或生成范围不同的批次使用独立 `run.id`。

`scene_mix` 四类场景目标比例为 15% text artifact、50% people interaction、20% physical
scene、15% environment context。`visual_evidence` 为 85% non-text、10% minimal-text、
5% text-supported；`max_visible_text_words` 对应 0/8/20。text artifact 分配给文字类证据模式，
两组比例均为文字类场景/证据保留 15% 容量。

## 图片后端

顶层 `image_backend: api | local` 分别选择 `image` 或 `local_image`；省略时为 API。
三份通用模板预设本地生图，越狱集成模板预设 API。两个配置块可以共存，选中的后端
决定生图参数及 `generate_during_benchmark` 时机。

本地后端通过 Diffusers 加载完整本地权重，不自动下载；需要可用 CUDA 环境。
模型官方地址与下载命令见[模型下载](DEPLOYMENT.md#模型下载)。HH/BH 原题图片和 VisualRoleplay
角色图共用以下图片后端配置：

```yaml
image_backend: local
local_image:
  model: Qwen-Image-2512
  model_path: /path/to/qwen-image
  model_revision: "delivery-01"
  size: "512*512"
  num_inference_steps: 50
  true_cfg_scale: 4.0
  negative_prompt: " "
  seed: 42
  dtype: bfloat16
  device_map: balanced
  cpu_offload: none
  concurrency: 1
  generate_during_benchmark: false
```

`model_path` 相对 `run.root` 或使用绝对路径，须包含 `model_index.json` 及完整组件。
原地更换权重时修改 `model_revision`，它是缓存版本标记。宽高须为正的 16 倍数；
`dtype` 支持 bfloat16、float16、float32；每图使用固定 seed，不保证跨硬件逐像素一致。
`balanced` 在当前可见 GPU 间分配，须搭配 `cpu_offload: none`；单卡卸载可用
`device_map: null`、`device: cuda:0` 和 `cpu_offload: model` 或 `sequential`。
本地并发固定为 1。预检检查目录、依赖及 CUDA 状态，实际推理通过小样本运行验收。
文本生成与回应阶段使用各自的 API 连接。

修改后端或图片参数后，独立运行 `generate-images` 更新图片。
配置版本与阶段恢复规则见[缓存与恢复](PIPELINE.md#缓存与恢复)。

## image

配置图片模型、endpoint、图片尺寸、同步或异步模式、并发、超时和重试次数。
`api_key_env` 只保存环境变量名。当前客户端校验返回内容确实是指定尺寸的 PNG/JPEG。
`image.concurrency` 是整个 benchmark run 共用的图片并发数，不会按 profile 再乘一次。

| 字段 | 说明 |
| --- | --- |
| `generate_during_benchmark` | 是否在 benchmark 每个机制 checkpoint 成功后立即提交图片任务；默认 `false`。关闭时单独运行 `generate-images`，或由 run-all 自动进入图片阶段。本地后端读取 local_image 中的同名字段。 |

开启时图片任务使用独立线程池，不阻塞文本生成主线程；benchmark 结束会补扫完整
`benchmark/*/benchmark.json`。`both` 模式按 `shared_image_id` 去重，awareness 和 instruction
共享一张图片；单风格模式每个机制各自一张。图片 manifest 会记录任务状态和校验信息，人工
图片阶段会复用状态为 `generated/reused` 且文件校验通过的任务。`--force` 才会忽略已有
manifest 并重新请求。由于图片请求早于最终跨 shard 全局重复检查，后续校验失败时可能已经
产生无法继续使用的图片 API 费用。

### 审核重试

`image.moderation_retry` 默认启用，字段为 `enabled: true`、`model: author`、
`validator_model: author`、`max_rewrites: 1`。两项模型引用 `models` 别名，省略时采用 author
及改写模型；改写额度固定为 1。仅在审核拒绝后提取原始视觉事实、改写措辞并独立校验，
保持内容与画面风格，合格候选才提交图片服务。候选不合格也消耗预算，普通重跑不重置额度。
记录在图片任务的 `moderation_retry` 与 `effective_prompt`；本地后端不执行此流程。
VisualRoleplay 的 API 角色图沿用同一机制。`--force` 会重置所选任务并重新生成，包括成功图片。

## augmentation

主配置使用 `enabled`（默认 false）和 `method`（默认空列表）选择越狱方法，例如
`augmentation: {enabled: true, method: [figstep, qr]}`。方法名不得重复，按列表顺序执行。
字体、排版、辅助模型和资源参数在 `value_eval/augmentation/methods/<method>/config.yaml` 中设置。
资源用途、放置目录和具体修改字段见[部署说明](DEPLOYMENT.md#本地模型与越狱资源)。

| 方法及参数文件 | 处理方式 | 原 HH 图 | 额外资源 |
| --- | --- | --- | --- |
| [figstep](../value_eval/augmentation/methods/figstep/config.yaml) | 问题与空编号排成文字图 | 否 | 无 |
| [qr](../value_eval/augmentation/methods/qr/config.yaml) | 原图加关键词条，改写问题 | 是 | 辅助文本 API |
| [camo](../value_eval/augmentation/methods/camo/config.yaml) | 文本遮字、算术线索与图片字符索引 | 是 | 辅助文本 API |
| [mml_wr](../value_eval/augmentation/methods/mml_wr/config.yaml) | 替换词语后排版，保存还原映射 | 否 | 项目内 NLTK 英文标注数据 |
| [mml_mirror](../value_eval/augmentation/methods/mml_mirror/config.yaml) | 问题文字图水平镜像 | 否 | 无 |
| [mml_rotate](../value_eval/augmentation/methods/mml_rotate/config.yaml) | 问题文字图旋转 180° | 否 | 无 |
| [himrd](../value_eval/augmentation/methods/himrd/config.yaml) | 问题片段移入原图面板，文本保留占位符 | 是 | 辅助文本 API |
| [cs_dj](../value_eval/augmentation/methods/cs_dj/config.yaml) | 9 张干扰图与 3 张子问题图拼接 | 否 | 辅助文本 API、本地 CLIP、至少 9 张普通图片组成的[干扰图库](DEPLOYMENT.md#cs-dj干扰图库与-clip) |
| [visual_roleplay](../value_eval/augmentation/methods/visual_roleplay/config.yaml) | 角色文字、额外生成的角色图与问题拼接 | 否 | 辅助文本 API、HH/BH 共用的[图片生成配置](DEPLOYMENT.md#图片生成) |
| [si](../value_eval/augmentation/methods/si/config.yaml) | 原图图块和问题词序打乱，增加关键词条 | 是 | 辅助文本 API |
| [viscra](../value_eval/augmentation/methods/viscra/config.yaml) | 注意力定位遮挡区，增加关键词条 | 是 | 辅助文本 API、分析图片关注区域的[本地 Qwen2.5-VL](DEPLOYMENT.md#viscra本地注意力模型) |

方法配置包含 `name`、`version`、`runtime.concurrency`、`parameters`，七种辅助 API 方法
另有 `auxiliary`。字体路径相对方法配置目录；CS-DJ 的 `src_dir` 相对 `run.root`，
`clip_path` 和 VisCRA 的 `attention_model_path` 建议填写当前机器的绝对路径。
CS-DJ 图库仅扫描第一层，至少 9 张；`max_pairs_per_question` 固定为 9。
SI 的宽高须能被 `blocks_per_side` 整除。FigStep/MML 放不下完整文字时失败，不截断原题。
VisCRA 依源图分辨率生成注意力网格，需核对窗口大小和显存；无有效注意力时失败。

辅助模型模板使用 `qwen3.5-35b-a3b`，回退使用 `deepseek-v4-flash`；分别引用
`DASHSCOPE_API_KEY` 和 `DEEPSEEK_API_KEY`。从主 `models` 优先按同名模型、再按同名密钥变量
匹配连接，主与回退均须存在。参数默认 `enable_thinking: false`、`temperature: 1.0`、
`max_tokens: 32768`、`timeout_sec: 180`、`retries: 2`、`retry_delay_sec: 1`。
retries 是首次之外的重试数，每模型最多三次。辅助请求不直接继承主模型的 `extra_body`。
模型 ID 应按账户实际可用范围修改；本地 CLIP 和注意力模型不走辅助 API。

方法内并发由 `runtime.concurrency` 控制，不影响方法缓存指纹；方法参数、代码及关联输入
变化会影响复用。独立预检检查源题、方法配置及所需文件；完整模型、NLTK 数据、API 连接和
GPU 推理通过所选方法的小样本运行验收。

## response

`enabled` 默认 true；设为 false 时 run-all 跳过回应，图片生成和已启用的越狱方法仍执行。
显式 `collect-responses` 不受此开关限制。`datasets` 默认 `[base, enabled_augmentations]`，
也可选择 `[base]` 或具体已启用方法列表。越狱样本仅支持 `image_text`，且采集前要求方法全量就绪。

`target_model` 指向 `models` 中的目标模型别名。原始集的 `mode` 支持：

| mode | 输入 | 回答形式 | 图片要求 |
| --- | --- | --- | --- |
| `image_text` | 真实图片和 question | 开放式，不发送选项 | `supports_images: true` 且图片已生成 |
| `image_mcq` | 真实图片、question 和 A/B/C/D | 要求只返回一个字母 | `supports_images: true` 且图片已生成 |
| `description_text` | 图片描述和 question | 开放式，不发送选项 | 不读取图片文件 |
| `description_mcq` | 图片描述、question 和 A/B/C/D | 要求只返回一个字母 | 不读取图片文件 |

`generation.styles` 与 `response.mode` 相互独立：awareness 和 instruction 均可用上述四种模式。
四种模式都不会发送参考答案、rationale、风险 profile 或生成 trace。采集阶段只保存原始回应，
MCQ 也不自动解析、判对或评分。`continue_on_error` 控制普通单条失败是否继续；鉴权和连接重试
耗尽始终终止批次。不同 mode 的输出写入不同目录，可对同一 benchmark 分别采集。命令行
`--response-mode` 可以临时覆盖 YAML 中的 `response.mode`；`--max-items 1` 可用于单条 API
测试。每条请求都会实时记录开始、完成、状态、进度和耗时，并同时输出到终端和 run 日志。
`--dataset` 可临时选择所需数据集并重复传入；原题 MCQ 或描述模式应指定 `--dataset base`。

## models

每个模型至少配置 `model`、`base_url`、`api_key_env`。可选字段包括 `temperature`、
`max_tokens`、`timeout_sec`、`max_retries`、`backoff_base_sec`、`backoff_jitter_sec`、
`thinking` 和 `extra_body`。`base_url` 可填写 API 根地址或完整 `/chat/completions` 地址。
