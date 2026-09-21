# Value_eval

`Value_eval` 是可独立复制和部署的多模态价值观基准流水线。它支持从中文 Excel
开始构建场景，也支持直接使用已准备好的场景数据集，随后执行严格 HH/BH Plan-Game 单生成器、
图片生成和目标模型原始回应采集。项目不包含 judge、自动评分或答案判定。

图片生成支持 API 和本地 Qwen-Image，并提供 11 种面向 HH-instruction 的越狱方法。
越狱操作见[第四步](#第四步可选hh-instruction-越狱)，参数见
[配置说明](docs/CONFIGURATION.md#augmentation)。

完整链路如下：

```text
价值观 Excel -> taxonomy 翻译/提取 -> 场景拆分 -> elements
             -> Plan-Game planner + single author
             -> 图片生成 -> 可选 HH-instruction 越狱 -> 目标模型原始回应 JSONL
```

代码边界见 [ARCHITECTURE.md](ARCHITECTURE.md)，配置字段见
[docs/CONFIGURATION.md](docs/CONFIGURATION.md)，Excel 规范见
[docs/EXCEL_INPUT.md](docs/EXCEL_INPUT.md)。

## 环境准备

运行环境为 Linux、Python 3.10 或更高版本。Windows 用户在 WSL/Linux 环境中执行以下命令。

```bash
cd Value_eval
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 中填写配置实际引用的密钥：

```dotenv
DEEPSEEK_API_KEY=...
DASHSCOPE_API_KEY=...
```

程序只读取环境变量，不会把密钥写入 YAML 或运行 manifest。
以下命令均在项目根目录 `Value_eval/` 中执行，使用 `python -m value_eval` 启动程序。
查看可用命令和参数：

```bash
python -m value_eval --help
```

## 选择配置

`configs/` 提供三份通用配置和一份越狱集成配置，文件内均有中文字段说明：

| 配置 | 用途 |
| --- | --- |
| `hh_bh_4000.yaml` | 使用固定 1000 个机制构建 HH/BH × 双题型的 4000 条基准。 |
| `prepared_scenarios.yaml` | 已有 `scenario_elements` 和 manifest 时使用；默认是两个场景的小样本模板。 |
| `excel_to_benchmark.yaml` | 从中文 Excel 构建场景，再生成 benchmark、图片和原始回应。 |
| `excel_to_benchmark_figstep_check.yaml` | 单行 Excel、四类原题和全部 11 种越狱方法；需额外准备本地模型及图库。 |

运行时始终通过 `--config` 明确选择；省略时默认使用 `prepared_scenarios.yaml`。

选择模板后复制为本次运行的配置。以下以两个已准备场景为例：

```bash
cp configs/prepared_scenarios.yaml configs/my_run.yaml
CONFIG=configs/my_run.yaml
```

使用 Excel、4000 条基准或全部越狱方法时，将复制命令的源文件换成表中对应模板。
编辑 `configs/my_run.yaml`，在开始生成前完成以下设置：

| 设置 | 填写要求 |
| --- | --- |
| `run.id` | 本次运行的唯一名称，例如 `my_run` |
| 场景输入 | Excel 路线填写工作簿与工作表；prepared 路线填写场景目录和配套清单 |
| `models` | 填写账户可调用的模型名称、接口地址和密钥变量名 |
| 图片后端 | API 路线设 `image_backend: api`；本地路线设 `local` 并填写完整权重路径 |
| `augmentation` | 启用越狱时设置 `enabled: true` 和方法列表，见第四步 |
| `response` | 采集回应时设置目标模型与数据集；仅生成数据时设 `enabled: false` |

三份通用模板预设本地生图，越狱集成模板预设 API 生图。后端参数见
[配置说明](docs/CONFIGURATION.md#图片后端)，方法资源见[越狱配置表](docs/CONFIGURATION.md#augmentation)。
下文命令均读取 `$CONFIG`，示例使用双题型 `--style both`；单题型运行时，各阶段统一使用
`--style awareness` 或 `--style instruction`。新终端中重新激活环境并设置
`CONFIG=configs/my_run.yaml`。YAML 示例为局部字段，编辑时合并到已有配置块。

## 第一步：Excel 生成场景文件

本节适用于 `scenario_source.mode: excel`。使用已准备场景时从第二步开始。

### 输入和配置

以 [configs/excel_to_benchmark.yaml](configs/excel_to_benchmark.yaml) 创建运行配置，并设置：

```yaml
run:
  # 后续阶段沿用此运行 ID。
  id: my_run

scenario_source:
  mode: excel
  xlsx: inputs/excel/value.xlsx
  sheet_name: "价值观目录及评判标准"
  # 每个场景至少生成多少个候选 element。
  min_elements: 8
  # 不同场景的构建并发数。
  concurrency: 4

  # 这四项是 models 下的模型别名，一般无需改别名，只需修改对应模型参数。
  translation_model: translator
  taxonomy_model: taxonomy
  stage1_model: scenario_classifier
  element_model: element_author
```

同时检查 `models.translator`、`models.taxonomy`、`models.scenario_classifier` 和
`models.element_author` 的 `model`、`base_url`、`api_key_env`。Excel 必需表头和编号规则见
[docs/EXCEL_INPUT.md](docs/EXCEL_INPUT.md)。

建议首次测试改用两行样例：

```yaml
scenario_source:
  xlsx: inputs/excel/examples/value_first_2_rows.xlsx
```

### 预检 Excel

该命令只读 Excel，不调用 API，也不写场景产物：

```bash
python -m value_eval validate-excel \
  --config "$CONFIG"
```

### 正式生成场景

```bash
python -m value_eval prepare-scenarios \
  --config "$CONFIG"
```

中断后执行同一命令会按 fingerprint 和 checkpoint 恢复。只有确认要忽略已有缓存时才加
`--force`。场景准备固定调用配置中的翻译、taxonomy、Stage 1 和 element 模型。

### 校验场景输出

```bash
python -m value_eval validate-scenarios \
  --config "$CONFIG"
```

输出位于：

```text
outputs/<run.id>/scenario_preparation/
├── scenario_elements/   # 第二步直接消费的场景文件
├── manifest.json
├── report.md
└── work/<fingerprint>/  # 中间请求、解析结果和 checkpoint
```

Excel 模式下，第二步自动读取本次运行发布的场景。

## 第二步：场景文件生成 Benchmark

### 选择入口配置

本节沿用 `$CONFIG`，按所选模板确定场景来源：

| 场景来源 | 来源模板 | 说明 |
| --- | --- | --- |
| 刚完成第一步的 Excel run | `excel_to_benchmark.yaml` | 保持相同 `run.id`，自动读取本次发布的场景。 |
| 自己已有场景目录和 manifest | `prepared_scenarios.yaml` | 修改 `generation.input_dir` 和 `input_manifest`。 |
| 固定 4000 条基准 | `hh_bh_4000.yaml` | 固定 canonical 场景和 1000 机制 selection。 |

运行内置样例时保留模板的输入和数量设置。接入自己的 prepared 场景时，修改相应字段：

```yaml
run:
  id: my_run

scenario_source:
  mode: prepared

generation:
  input_dir: inputs/scenarios/my_data/scenario_elements
  # 可选；已有 manifest 时填写，没有时直接扫描 input_dir 中的 JSON。
  input_manifest: inputs/scenarios/my_data/manifest.json
  # 可选：只生成 selection 中指定的 scenario/element。
  # input_selection: inputs/selections/my_selection.json

  # 可选 [hh]、[bh] 或 [hh, bh]。
  profiles: [hh, bh]
  # both 模式：每个机制生成 awareness+instruction，共享一张图片。
  styles: [awareness, instruction]
  share_image_across_styles: true

  # 0：每个 element 生成一次；N>0：每个场景固定生成 N 个独立变体。
  variants_per_scenario: 5
  # 0 表示不限制。正式运行应为 0；非零值只用于小样本截断。
  max_items_per_profile: 0

  planner_model: planner
  author_model: author
```

如果只需要场景子集，最简单的方式是把目标场景 JSON 放进独立目录，并且只配置
`input_dir`；此时不需要生成 `input_selection`：

```yaml
generation:
  input_dir: inputs/scenarios/my_subset/scenario_elements
  input_manifest: null
  input_selection: null
```

从共享目录精确选择机制时，使用 `input_selection` 指定 `(scenario_id, element_id)` 集合。

如果只生成一种题型，配置必须成对修改：

```yaml
generation:
  styles: [awareness]
  share_image_across_styles: false
```

或只生成 instruction：

```yaml
generation:
  styles: [instruction]
  share_image_across_styles: false
```

还需检查：

- `models.planner` 和 `models.author`：模型、接口、密钥环境变量、超时和重试。
- `scene_mix`：四类场景比例，当前为 15/50/20/15。
- `visual_evidence`：当前为 non-text/minimal-text/text-supported = 85/10/5。
- `max_visible_text_words`：三种视觉模式的可见文字预算。
- `concurrency`、`item_max_attempts`、`shard_max_attempts`：并发和恢复参数。
- `image.generate_during_benchmark`：是否在本阶段同步启动图片任务，详见第三步。

### 并发设置

`generation.concurrency` 控制每个 profile 的机制并发数。例如 HH/BH 同时运行且值为 2 时，
文本生成最多并行处理 4 个机制。双题型每个机制生成两条题，单题型生成一条。

| 配置项 | 控制范围 |
| --- | --- |
| `scenario_source.concurrency` | Excel 场景构建并发 |
| `generation.concurrency` | 每个 profile 的构题并发 |
| `image.concurrency` | API 生图的全局并发 |
| `local_image.concurrency` | 本地生图，固定为 1 |
| 方法配置的 `runtime.concurrency` | 单个越狱方法的源题处理并发 |
| `response.concurrency` | 目标模型回应采集并发 |

先用小批次记录各阶段耗时、失败率和服务限流情况，再调整并发与批次规模。
吞吐按实际完成题数统计；双题型的共享图片单独计数。

### 预检输入和预计数量

场景准备完成后，检查本次运行的输入与计划题数：

```bash
python -m value_eval validate-input --config "$CONFIG"
```

| 输出字段 | 含义与检查要求 |
| --- | --- |
| `scenario_count` / `scenario_element_count` | 场景数与选中的 element 数，与输入范围一致 |
| `generation_job_count` | 每个 profile 的机制任务数 |
| `planned_questions_per_profile` | 每个 profile 的计划题数 |
| `variants_per_scenario` | 每场景变体数，0 为逐 element 模式 |
| `prepared_scenarios_ready` | 场景输入已就绪 |
| `missing_api_key_envs` | 应为空列表 |
| `local_image_status` | 本地生图路线的目录、依赖与 CUDA 检查结果 |

预检完成本地输入与配置检查。服务连接和实际推理通过下一步的小样本运行验证。

### 小样本测试

`--max-items` 临时覆盖每个 profile 的题数上限；both 模式必须是偶数：

```bash
python -m value_eval generate-benchmark \
  --config "$CONFIG" \
  --style both \
  --max-items 4
```

需要测试场景变体时，在本次首次生成命令中增加 `--variants-per-scenario N`。
同一批次续跑时保留相同参数。

### 正式生成 Benchmark

正式批次使用独立的 `run.id`，按所需规模设置变体数及 `max_items_per_profile`，然后执行：

```bash
python -m value_eval generate-benchmark --config "$CONFIG" --style both
```

`both` 模式下，awareness 与 instruction 共享图片描述；单题型分别执行独立的规划与编写流程。
生成机制及校验规则见 [流水线说明](docs/PIPELINE.md#plan-game-单生成器)。

数量计算：

```text
最终题数 = 场景数 × variants_per_scenario × profile 数 × style 数
```

例如 473 个场景、每场景 5 个变体、HH/BH、both：

```text
473 × 5 × 2 × 2 = 9460 条题
```

当 `variants_per_scenario: 0` 时按选中的 element 数计算。
4000 配置为 1000 elements × 2 profiles × 2 styles = 4000 条题。

benchmark 输出位于：

```text
outputs/<execution_run_id>/benchmark/
├── shards/shard_NN/<profile>/benchmark.json # 每 200 个机制的恢复 checkpoint
├── <profile>/benchmark.json                 # profile 合并结果
└── benchmark_<count>[_<style>].json         # 全部 profile 合并结果
```

`both` 的 execution run ID 就是 `run.id`；单题型自动使用 `<run.id>--awareness` 或
`<run.id>--instruction`。后续生成图片时必须使用相同 `--style`。

## 第三步：Benchmark 生成图片

### 配置图片模型

图片阶段读取本次运行配置中的生图参数。以下为 API 配置；本地路线使用 `local_image`
及同名 `generate_during_benchmark` 开关，见[配置说明](docs/CONFIGURATION.md#图片后端)。

```yaml
image_backend: api
image:
  # false：benchmark 完成后人工执行 generate-images，推荐用于正式批量构建。
  # true：每个机制 checkpoint 落盘后立即提交图片任务。
  generate_during_benchmark: false
  api_key_env: DASHSCOPE_API_KEY
  model: qwen-image-2.0
  size: 2048*2048
  endpoint: https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
  task_endpoint: https://dashscope.aliyuncs.com/api/v1/tasks
  prompt_extend: false
  async_call: false
  concurrency: 3
  timeout_sec: 120
  max_retries: 3
  rate_limit_retries: 6
```

`api_key_env` 填环境变量名称，真实密钥写在 `.env`。`size` 必须和服务实际返回尺寸一致。
API 审核拒绝后默认进行一次保持视觉事实与风格的措辞改写，通过独立校验才提交；原描述不变，
历史保存到图片 manifest。普通重跑不重置已耗尽的额度，见[审核重试](docs/CONFIGURATION.md#审核重试)。

### 方式 A：Benchmark 完成后人工生成

推荐先把 `generate_during_benchmark` 保持为 `false`，确认 benchmark 全局校验通过后运行：

```bash
python -m value_eval generate-images \
  --config "$CONFIG" \
  --style both
```

小样本图片测试可加 `--max-items 2`；该限制按每个 profile 的图片任务计算。

### 方式 B：按机制立即生成

先修改：

```yaml
image:
  generate_during_benchmark: true
```

然后正常执行第二步的 `generate-benchmark`。每个机制成功写入 benchmark shard checkpoint 后，
图片任务会在独立线程池中提交。`both` 模式的 awareness/instruction 只生成一张共享图片；
单题型每个变体各生成一张。

benchmark 完成后仍可人工再执行 `generate-images`。程序会校验 manifest、文件尺寸、格式和
SHA256，已完成图片直接复用，不会重复调用 API。只有显式添加 `--force` 才会覆盖已有图片。

图片输出位于：

```text
outputs/<execution_run_id>/images/<profile>/
├── files/
└── manifest.json
```

注意：即时图片模式会在最终跨分片重复校验前产生图片请求。如果 benchmark 最终失败，已经
发生的图片 API 费用不会回滚；因此正式大批量运行通常建议采用方式 A。

## 第四步（可选）：HH-instruction 越狱

越狱阶段读取本次运行的 HH-instruction 题目，为每条源题生成对应的越狱文本和图片。
本节使用 FigStep，运行配置沿用 `$CONFIG`。

### 选择越狱方法

在运行配置中设置：

```yaml
augmentation:
  enabled: true
  method: [figstep]
```

输入为 `outputs/<execution_run_id>/benchmark/hh/benchmark.json` 中的 instruction 条目，
由第二步生成。FigStep 将问题排成文字图，无需原始图片或辅助模型。
其他方法的资源要求和参数入口见[越狱配置表](docs/CONFIGURATION.md#augmentation)。

### 生成与采集

```bash
# 检查源题、方法配置与字体。
python -m value_eval generate-augmentations --config "$CONFIG" --style both --method figstep --dry-run

# 为全部 HH-instruction 源题生成 FigStep 样本。
python -m value_eval generate-augmentations --config "$CONFIG" --style both --method figstep

# 采集 FigStep 样本的目标模型回应。
python -m value_eval collect-responses --config "$CONFIG" --style both --response-mode image_text --dataset figstep
```

`--method` 指定本次执行的越狱方法，方法名须已列入运行配置的 `augmentation.method`，
且 `augmentation.enabled` 为 `true`。选择多种方法时，为每种方法分别写一个参数，
例如 `--method figstep --method qr`；省略 `--method` 时执行配置中全部已启用的方法。
小批次可用 `--max-items N` 限制源题数，正式采集前移除限制并完成该方法的全部源题。

### 检查结果

| 产物 | 检查内容 |
| --- | --- |
| `augmentations/figstep/samples.jsonl` | 每条源题对应一个越狱样本，保留来源 ID |
| `augmentations/figstep/images/` | 文字完整、图片可读取 |
| `augmentations/figstep/manifest.json` | `status: completed`，完成源题数等于总源题数 |
| `responses/<target>/image_text/augmentations/figstep.jsonl` | 目标模型的原始回应 |

全部启用方法完成后，`dataset/manifest.json` 汇总原题与越狱子集。
每种方法每条源题生成一个样本，总数为“原题数 + HH-instruction 数 × 方法数”。
续跑与配置变更规则见[缓存与恢复](docs/PIPELINE.md#缓存与恢复)。

## 可选：采集目标模型回应

原始集支持下表四种模式，越狱样本集只支持 `image_text`。`response.datasets` 默认
`[base, enabled_augmentations]`，也可选择 `[base]` 或具体已启用方法。命令行可通过多个
`--dataset` 选择多个数据集，例如 `--dataset base --dataset figstep`。

回应采集同时支持开放式和 MCQ。`generation.styles` 决定 benchmark 是 awareness、instruction
还是 both；`response.mode` 独立决定发送真实图片还是图片描述，以及要求开放回答还是选项字母。

| `response.mode` | 发送给目标模型 | 回答要求 | 需要先生成图片 |
| --- | --- | --- | --- |
| `image_text` | 真实图片 + question | 开放式回答，不发送选项 | 是 |
| `image_mcq` | 真实图片 + question + A/B/C/D | 严格返回一个选项字母 | 是 |
| `description_text` | `image_description` + question | 开放式回答，不发送选项 | 否 |
| `description_mcq` | `image_description` + question + A/B/C/D | 严格返回一个选项字母 | 否 |

回应阶段读取本次运行配置中的目标模型和模式：

```yaml
response:
  # 对应 models.target。
  target_model: target
  # 四选一：image_text、image_mcq、description_text、description_mcq。
  mode: image_text
  datasets: [base] # 此示例只采集原题；包含越狱时使用 image_text。
  concurrency: 3
  continue_on_error: true

models:
  target:
    model: qwen3-vl-8b-thinking
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: DASHSCOPE_API_KEY
    # image_text/image_mcq 必须为 true；description 模式不要求图片能力。
    supports_images: true
```

然后运行：

```bash
python -m value_eval collect-responses \
  --config "$CONFIG" \
  --style both
```

以下命令分别对同一条原题采集开放式和 MCQ 回应，结果写入各自的 mode 目录：

```bash
# 开放式：发送图片和 question，不发送选项。
python -m value_eval collect-responses \
  --config "$CONFIG" \
  --style both \
  --response-mode image_text \
  --dataset base \
  --max-items 1

# MCQ：发送图片、question 和 A/B/C/D，要求返回一个选项字母。
python -m value_eval collect-responses \
  --config "$CONFIG" \
  --style both \
  --response-mode image_mcq \
  --dataset base \
  --max-items 1
```

终端会实时显示每条 target 请求的开始、完成状态、进度和 API 耗时，同时默认追加到
`logs/<execution_run_id>/pipeline.log`。另开终端可持续查看相同日志：

```bash
RUN_ID=my_run
tail -f "logs/${RUN_ID}/pipeline.log"
```

已完成的 sample 会按 mode 独立断点复用；重跑相同命令不会再次调用 API。明确需要重新采集时
才添加 `--force`；新回应追加到 JSONL，旧记录不会删除。

所有 benchmark item 内部都保留四个选项和参考答案，但开放式模式不会发送选项；四种模式均
不会发送参考答案、rationale、风险 profile 或生成 trace。MCQ 模式当前也只原样保存模型输出，
不会校验它是否确实只返回一个字母，更不会判断答案正确性。

输出为 `outputs/<execution_run_id>/responses/<target>/<mode>/<profile>.jsonl`，保存原始
`response`、`reasoning_content`、usage、耗时和 request ID，不运行 judge 或自动评分。不同
mode 使用不同子目录，因此可对同一 benchmark 分别采集开放式和 MCQ 回应。

越狱样本的回应位于 `responses/<target>/image_text/augmentations/<method>.jsonl`，通过
`source_benchmark_id` 关联原题。该方法须全量完成，即使回应只采一条也会检查完整性。

## 一次运行完整流程

输入、模型、图片后端、越狱方法和回应数据集配置完成后，可按顺序执行所有启用阶段：

```bash
python -m value_eval run-all --config "$CONFIG" --style both
```

Excel 路线先准备场景，prepared 路线从 benchmark 开始，随后依次生成图片和越狱样本，再采集回应。
启用越狱时，生成范围应包含 `hh` 和 `instruction`；`response.enabled: false` 时流程在数据生成后结束。
验收使用下文的阶段计数，并抽查题目、图片及回应。

## 常用控制参数

| 参数 | 作用 |
| --- | --- |
| `--dry-run` | 只检查输入和配置，不发请求 |
| `--style MODE` | benchmark 题型：`both`、`awareness` 或 `instruction` |
| `--response-mode MODE` | 临时覆盖回应模式：`image_text`、`image_mcq`、`description_text` 或 `description_mcq` |
| `--method NAME` | 仅用于 `generate-augmentations`，从配置已启用的方法中选择本次执行范围；多选示例：`--method figstep --method qr`；省略时执行全部已启用方法 |
| `--dataset NAME` | 指定 `collect-responses` 或 `run-all` 采集回应的数据集：`base`、`enabled_augmentations` 或已启用的方法名；多选示例：`--dataset base --dataset figstep` |
| `--max-items N` | benchmark / run-all 按每个 profile 题数，图片按每个 profile 任务数，越狱按源题数，回应按所选集总样本数；both benchmark 要求偶数，不限制 Excel 场景准备范围 |
| `--variants-per-scenario N` | 覆盖每个场景的独立生成变体数；0 保持 element-once 模式 |
| `--force` | 忽略对应阶段 checkpoint 并重新生成 |
| `--retry-fallbacks` | 重试此前因模型错误而降级的场景 |
| `--no-publish` | 只构建和校验场景，不发布给下游阶段 |

相同配置会基于指纹恢复执行。鉴权、欠费和连接重试耗尽会终止当前阶段；已成功写入的
checkpoint 保留。普通单条失败会记录在阶段报告中。

## 输出

```text
outputs/<execution_run_id>/
├── run_manifest.json
├── scenario_preparation/
│   ├── scenario_elements/
│   ├── manifest.json
│   ├── report.md
│   └── work/<fingerprint>/
├── benchmark/shards/shard_NN/<profile>/benchmark.json
├── benchmark/<profile>/benchmark.json
├── benchmark/benchmark_<count>[_<style>].json
├── images/<profile>/
│   ├── files/
│   └── manifest.json
├── augmentations/<method>/
│   ├── images/
│   ├── samples.jsonl
│   ├── manifest.json
│   ├── config.snapshot.yaml
│   └── preparation/、resources/  # 按需产生的中间缓存
├── dataset/manifest.json        # 启用越狱时产生的数据集索引
└── responses/
    ├── manifest.json           # 最近一次回应采集计数
    └── <target>/<mode>/
        ├── <profile>.jsonl
        └── augmentations/<method>.jsonl
```

只有完整 HH/BH、双风格的 4000 条正式产物使用兼容文件名 `benchmark_4000.json`；单风格和
小样本文件名包含实际条数与模式。

回应文件只保存发给目标模型的输入摘要和原始输出，不进行评判。
验收还应确认图片无失败、越狱样本完整，以及回应的 `completed + resumed == sample_count`
且 `failed/skipped` 为 0；总 run 的 `completed` 不代表每条成功。分阶段运行不生成总 run 清单。

部署步骤与验收要求见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。
