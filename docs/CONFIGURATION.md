# 配置字段手册

可选增强使用 `augmentation: {enabled: true, method: [figstep]}`，方法参数位于
`value_eval/augmentation/methods/<name>/config.yaml`。增强范围固定为实际生成的
HH-instruction，generation 可包含全部四类。`response.datasets` 选择回应子集。
详见 [增强配置与独立命令](AUGMENTATION.md)。

所有相对路径均以 `run.root` 指向的项目根目录解析。密钥字段只填写环境变量名。

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

当前模型和正常网络条件下，单 profile 单 style 使用 `concurrency: 2` 约为 130--145
图文对/小时。项目目标 200 对/小时时，最低建议改为 3（约 195--220 对/小时）；需要稳定余量
可改为 4（约 260--290 对/小时）。图片实测约 11 秒/张，`image.concurrency: 2` 足以覆盖
上述速度。完整实测口径、`both` 共享图片的计数方式和服务端延迟风险见 README 的
“并发和速度估算”。

通用 `prepared_scenarios.yaml` 和 `excel_to_benchmark.yaml` 显式配置为文本 2、图片 2；
`hh_bh_4000.yaml` 配置为文本 3、图片 3。这里不存在脱离配置文件的统一运行默认值，执行时
应以传给 `--config` 的 YAML 为准。

`variants_per_scenario` 控制每个场景、每个 profile 的独立生成任务数。默认值 0 表示旧的
`all_elements_once` 行为；正整数表示固定场景级扩增，并允许大于该场景的 element 数。扩增时
程序按稳定顺序轮换场景内的 element，并把 `variant_index` 写入 ID、trace、checkpoint 和
恢复指纹。最终 item 数计算为：

```text
场景数 × variants_per_scenario × profile 数 × style 数
```

`max_items_per_profile` 是所有场景合计后的每 profile 总 item 上限，主要用于小样本调试；它
不是每场景数量。正式使用 `variants_per_scenario` 时建议设为 0，避免截断最后一部分场景。
改变变体数量时应同时使用新的 `run.id`；已有 run 的 fingerprint 不匹配时程序会拒绝混用。

`scene_mix` 四类场景目标比例为 15% text artifact、50% people interaction、20% physical
scene、15% environment context。`visual_evidence` 为 85% non-text、10% minimal-text、
5% text-supported；`max_visible_text_words` 对应 0/8/20。沿用旧禁配规则，text artifact
不会分配给 non-text；当前两组比例恰好都为文字类场景/证据保留 15% 容量。

## image_backend、image 和 local_image

顶层 `image_backend` 选择图片后端：`api` 使用 `image`，`local` 使用 `local_image`。
两组配置可以同时保留，未选中的一组不参与推理、图片缓存指纹或生图时机判断。
省略 `image_backend` 时默认 `api`，兼容旧配置。

```yaml
image_backend: local
image:
  model: qwen-image-2.0
  api_key_env: DASHSCOPE_API_KEY
  size: "2048*2048"
local_image:
  model: Qwen-Image-2512
  model_path: /HDD0/hanzhouyu/Qwen-image-2512
  model_revision: "2512"
  size: "512*512"
  num_inference_steps: 50
  true_cfg_scale: 4.0
  negative_prompt: "低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲。"
  seed: 42
  dtype: bfloat16
  cpu_offload: none
  device_map: balanced
  concurrency: 1
  generate_during_benchmark: false
```

当前三份模板均启用 `local`，参数参考
`/HDD0/hanzhouyu/50-single/image_dataset_generation/config.json`。
其中 `width`/`height` 合并为 `size`，`offload` 对应 `cpu_offload`；不执行额外预热。
固定 seed 策略与参考脚本一致：每张图重建随机数生成器并使用 seed 42。

`local_image.model_path` 是完整 Diffusers 模型目录，相对 `run.root` 解析，也支持绝对路径。
程序始终以 `local_files_only=True` 加载本地权重，不下载模型。目录应包含
`model_index.json` 以及模型、文本编码器、VAE、tokenizer、scheduler 等组件。
原地更换权重时同步修改 `model_revision`；此字段用于记录和缓存失效，不是下载参数。

本地依赖将 PyTorch 固定为 `2.8.0`，避免无上限升级到本机驱动不兼容的 CUDA 13 构建。
`pyproject.toml` 的版本约束不指定 pip 下载源，CUDA 构建通过安装命令明确选择。
在项目根目录执行：

```bash
.venv/bin/python -m pip install 'torch==2.8.0' --index-url https://download.pytorch.org/whl/cu128
.venv/bin/python -m pip install -e '.[local-image]'
```

CUDA 12.8 安装源见 [PyTorch 官方安装说明](https://pytorch.org/get-started/previous-versions/#v280)。
修改依赖文件不会自动更新已经安装的包；上述命令也用于替换现有 `.venv` 中的错误版本。

可选依赖要求 Diffusers >= 0.35，该版本加入 Qwen-Image pipeline，见
[官方发布说明](https://github.com/huggingface/diffusers/releases/tag/v0.35.0)。

本地默认 512×512、50 步、BF16、CFG 4.0；`size` 的两维必须为正的 16 倍数。
`device_map: balanced` 由 Diffusers 在当前可见 GPU 中自动分配模型，并设 `cpu_offload: none`。
不设置 `CUDA_VISIBLE_DEVICES` 时使用进程默认可见的 GPU；如需限定两张物理卡，设置
`CUDA_VISIBLE_DEVICES=0,1`。不再要求恰好可见两张卡，至少需要一张可用 CUDA GPU。
自动分配行为见 [Diffusers 设备分配说明](https://huggingface.co/docs/diffusers/main/en/tutorials/inference_with_big_models#device-placement)。
此模式不手动分配层，也不调用整个 pipeline 的 `.to()`。
如需单卡 CPU 卸载，设 `device_map: null`，然后选择 `cpu_offload: model` 或 `sequential`；
这时 `device` 指定单张 CUDA 卡，默认 `cuda:0`。CPU 卸载与自动设备分配不同时启用。
本地 `concurrency` 固定为 1，各图片任务共享模型，模型在首个待生成任务上加载，
阶段结束后释放；全部图片可复用时不加载模型。

每张图直接使用配置的 `seed`，默认 42，实际 seed 写入任务 manifest。CPU 随机数生成器
创建噪声后由 Diffusers 送到对应设备；相同 seed 不保证跨硬件或依赖版本的逐像素一致性。
本地 manifest 记录模型路径/版本标记、尺寸、步数、CFG、负提示词、dtype 和 seed；
修改生成参数或切换后端会使图片缓存失效。API 历史 manifest 仍可按原规则复用。
`device_map` 和随机数生成器设备也进入图片缓存指纹。
`validate-input` / `--dry-run` 在本地模式下报告目录、依赖和 CUDA 就绪情况，不加载权重。
图片阶段不要求图片 API Key，其他文本和回应阶段仍要求各自模型的密钥。

已有 benchmark 可单独运行 `generate-images` 补图。benchmark checkpoint 当前包含整个
YAML 文件哈希，修改配置后对旧 run 执行 `run-all` 可能触发指纹不匹配；新实验使用新
`run.id`。切换图片后重新执行 `collect-responses`，图片哈希变化会使旧图片回应缓存失效。

以下 `image` 字段仅用于 API 后端：

配置图片模型、endpoint、图片尺寸、同步或异步模式、并发、超时和重试次数。
`api_key_env` 只保存环境变量名。当前客户端校验返回内容确实是指定尺寸的 PNG/JPEG。
`image.concurrency` 是整个 benchmark run 共用的图片并发数，不会按 profile 再乘一次。

| 字段 | 说明 |
| --- | --- |
| `generate_during_benchmark` | 是否在 benchmark 每个机制 checkpoint 成功后立即提交图片任务；默认 `false`。`run-all` 关闭此项时也会在 benchmark 完成后自动执行图片阶段；单独运行 `generate-benchmark` 时需另行运行 `generate-images`。本地后端读取 `local_image` 中的同名字段。 |

开启时图片任务使用独立线程池，不阻塞文本生成主线程；benchmark 结束会补扫完整
`benchmark/*/benchmark.json`。`both` 模式按 `shared_image_id` 去重，awareness 和 instruction
共享一张图片；单风格模式每个机制各自一张。图片 manifest 会记录任务状态和校验信息，人工
图片阶段会复用生成参数匹配、状态为 `generated/reused` 且文件校验通过的任务。`--force` 会忽略已有
manifest 并重新生成。由于图片请求早于最终跨 shard 全局重复检查，后续校验失败时可能已经
产生无法继续使用的图片 API 费用。

## response

`enabled` 默认 `true`。如果只需要 benchmark 和图片，设置 `response.enabled: false`，
一键脚本 / `run-all` 会在图片阶段完成后结束，manifest 将回答阶段标记为 `skipped`。
此时全流程预检不要求目标回答模型及其 API Key，也不检查其图片能力。
显式运行 `collect-responses` 仍会采集回答，需配置有效的 `target_model` 和对应密钥。

`target_model` 指向 `models` 中的目标模型别名。`mode` 支持：

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

## models

每个模型至少配置 `model`、`base_url`、`api_key_env`。可选字段包括 `temperature`、
`max_tokens`、`timeout_sec`、`max_retries`、`backoff_base_sec`、`backoff_jitter_sec`、
`thinking` 和 `extra_body`。`base_url` 可填写 API 根地址或完整 `/chat/completions` 地址。
