# Value_eval

`Value_eval` 是可独立复制和部署的多模态价值观基准流水线。它支持从中文 Excel
开始构建场景，也支持直接使用已准备好的场景数据集，随后执行严格 HH/BH Plan-Game 单生成器、
图片生成和目标模型原始回应采集。项目不包含 judge、自动评分或答案判定。

支持 11 种可选的越狱增强方法：FigStep、QR、CAMO、MML_WR、MML_Mirror、MML_Rotate、
HIMRD、CS-DJ、VisualRoleplay、SI 和 VisCRA。增强只处理 HH-instruction，原始四类数据
仍按 generation 配置生成。已有 benchmark 可通过 `generate-augmentations` 独立补做。
方法说明见 [数据增强](docs/AUGMENTATION.md)，完整验证入口沿用
[excel_to_benchmark_figstep_check.yaml](configs/excel_to_benchmark_figstep_check.yaml)。

完整链路如下：

```text
价值观 Excel -> taxonomy 翻译/提取 -> 场景拆分 -> elements
             -> Plan-Game planner + single author
             -> 图片生成 -> 可选数据增强 -> 目标模型原始回应 JSONL
```

代码边界见 [ARCHITECTURE.md](ARCHITECTURE.md)，配置字段见
[docs/CONFIGURATION.md](docs/CONFIGURATION.md)，Excel 规范见
[docs/EXCEL_INPUT.md](docs/EXCEL_INPUT.md)。阶段时序与恢复规则见
[docs/PIPELINE.md](docs/PIPELINE.md)，产物字段见 [docs/DATA_SCHEMA.md](docs/DATA_SCHEMA.md)，
部署和排错分别见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) 与
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

## 环境准备

要求 Python 3.10 或更高版本。

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

程序只读取环境变量，不会把密钥写入 YAML 或运行 manifest。也可以安装命令行入口：

```bash
python -m pip install -e .
value-eval --help
```

## 选择配置

`configs/` 提供三份通用配置和一份增强验证配置，文件内均有中文字段说明：

| 配置 | 用途 |
| --- | --- |
| `hh_bh_4000.yaml` | 使用固定 1000 个机制严格复现 HH/BH 旧 4000 基准；也可作为扩增实验起点。 |
| `prepared_scenarios.yaml` | 已有 `scenario_elements` 和 manifest 时使用；默认是两个场景的小样本模板。 |
| `excel_to_benchmark.yaml` | 从中文 Excel 构建场景，再生成 benchmark、图片和原始回应。 |
| `excel_to_benchmark_figstep_check.yaml` | 四类原题、11 种增强和回应采集的小样本模板；当前使用 API 生成 2048×2048 图片。 |

运行时始终通过 `--config` 明确选择；省略时默认使用 `prepared_scenarios.yaml`。

三份配置均同时提供 `image`（API）和 `local_image`（本地 Qwen-Image）。当前模板设置
`image_backend: local`，使用 `/HDD0/hanzhouyu/Qwen-image-2512`，512×512、50 步、固定 seed 42，
通过 `device_map: balanced` 交给 Diffusers 在当前可见 GPU 中自动分配。
无需指定 GPU 数量；如需限定两张卡，可设置 `CUDA_VISIBLE_DEVICES=0,1`。
切换为 `image_backend: api` 即可使用原 API 配置；旧配置省略该字段仍默认 API。
可使用参考项目已有环境运行：

```bash
CUDA_VISIBLE_DEVICES=0,1 \
PYTHON_BIN=/SSD3/hanzhouyu/anaconda3/envs/sd/bin/python \
bash scripts/run_pipeline.sh
```

使用项目 `.venv` 时，按以下顺序安装。PyTorch 固定为 2.8.0，第一条命令选择与本机
已验证环境一致的 CUDA 12.8 构建，第二条安装统一的流水线、增强和本地生图依赖：

```bash
.venv/bin/python -m pip install 'torch==2.8.0' --index-url https://download.pytorch.org/whl/cu128
.venv/bin/python -m pip install -r requirements.txt
```

修改 `requirements.txt` 不会自动替换现有环境中的 PyTorch，需要实际执行上述安装命令。
一键入口 `bash scripts/run_pipeline.sh`
和各阶段命令均读取此选项，详细字段和旧 run 迁移方式见
[配置说明](docs/CONFIGURATION.md#image_backendimage-和-local_image)。

只需要数据产物时，将配置中的 `response.enabled` 改为 `false`，一键脚本会在
图片及已启用的增强生成完成后结束，跳过目标模型回答采集。该开关默认 `true`。

## 第一步：Excel 生成场景文件

如果已经有 `scenario_elements/*.json` 和 manifest，可跳过本步，直接进入第二步。

### 输入和配置

使用 [configs/excel_to_benchmark.yaml](configs/excel_to_benchmark.yaml)，至少修改：

```yaml
run:
  # 每次实验使用新 ID；后续 benchmark 和图片阶段继续使用同一份配置。
  id: my_excel_run

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
  --config configs/excel_to_benchmark.yaml
```

### 正式生成场景

```bash
python -m value_eval prepare-scenarios \
  --config configs/excel_to_benchmark.yaml
```

中断后执行同一命令会按 fingerprint 和 checkpoint 恢复。只有确认要忽略已有缓存时才加
`--force`。场景准备固定调用配置中的翻译、taxonomy、Stage 1 和 element 模型。

### 校验场景输出

```bash
python -m value_eval validate-scenarios \
  --config configs/excel_to_benchmark.yaml
```

输出位于：

```text
outputs/<run.id>/scenario_preparation/
├── scenario_elements/   # 第二步直接消费的场景文件
├── manifest.json
├── report.md
└── work/<fingerprint>/  # 中间请求、解析结果和 checkpoint
```

Excel 模式下不需要把这些路径再填写到 `generation.input_dir`；使用同一份
`excel_to_benchmark.yaml` 运行第二步时，程序会自动读取本次 `run.id` 发布的场景。

## 第二步：场景文件生成 Benchmark

### 选择入口配置

有三种常见入口：

| 场景来源 | 使用配置 | 说明 |
| --- | --- | --- |
| 刚完成第一步的 Excel run | `excel_to_benchmark.yaml` | 保持相同 `run.id`，自动读取本次发布的场景。 |
| 自己已有场景目录和 manifest | `prepared_scenarios.yaml` | 修改 `generation.input_dir` 和 `input_manifest`。 |
| 严格复现旧 4000 | `hh_bh_4000.yaml` | 固定 canonical 场景和旧 1000 机制 selection。 |

使用 prepared 场景时，至少配置：

```yaml
run:
  id: my_prepared_run

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
```

`input_selection` 仅用于不能整理输入目录、且必须从共享大目录中精确选择
`(scenario_id, element_id)` 时使用。

如果只生成一种题型，配置必须成对修改：

```yaml
# 只生成 awareness
styles: [awareness]
share_image_across_styles: false

# 或只生成 instruction
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

### 并发和速度估算

`generation.concurrency` 是**每个 profile 的机制级并发数**，不是整个进程的全局并发数。
HH 和 BH 会并行运行，因此 `[hh, bh]` 且 `concurrency: 2` 时，文本生成峰值为 4 个机制；
只运行 `[hh]` 时峰值为 2 个机制。每个机制在 `both` 模式生成两条图文对，在单 style
模式生成一条图文对。`image.concurrency` 是图片线程池的全局并发数，和文本线程池独立。

仓库当前配置值如下：

| 配置 | `generation.concurrency` | HH/BH 同跑文本峰值 | `image.concurrency` |
| --- | ---: | ---: | ---: |
| `prepared_scenarios.yaml` / `excel_to_benchmark.yaml` | 2 | 4 | 2 |
| `hh_bh_4000.yaml` | 3 | 6 | 3 |

这里所说的“默认”是 YAML 中显式填写的运行值；实际运行始终以所选 `--config` 为准。

2026-09-16 使用 `deepseek-v4-flash` planner、`qwen3-max` author 和
`qwen-image-2.0`，在网络正常且 planner 单次约 7--9 秒时，测得/推算吞吐如下。这里的
“图文对”指一条 benchmark item 及其引用图片；`both` 的两条 item 共用一张唯一图片。

| 模式 | `generation.concurrency` | 文本峰值并发 | 预计图文对/小时 | 唯一图片/小时 |
| --- | ---: | ---: | ---: | ---: |
| HH+BH × both | 2 | 4 | 长时间约 300--320；28 条短任务约 280 | 约 150--160 |
| 单 profile × 单 style，如 HH × instruction | 2 | 2 | 约 130--145 | 约 130--145 |
| 单 profile × 单 style | 3 | 3 | 约 195--220 | 约 195--220 |
| 单 profile × 单 style | 4 | 4 | 约 260--290 | 约 260--290 |

项目目标为 200 图文对/小时。只生成 `HH × instruction` 时，建议先把
`generation.concurrency` 从 2 改为 3；这是达到目标的最低建议值。若实测持续低于 200，
再改为 4 留出余量。对应配置为：

```yaml
generation:
  profiles: [hh]
  styles: [instruction]
  share_image_across_styles: false
  concurrency: 3 # 约 195--220 对/小时；需要更大余量时改为 4。

image:
  generate_during_benchmark: true
  concurrency: 2 # 实测约 11 秒/张，当前不是吞吐瓶颈。
```

本次 28 条实验在 planner 出现 136--140 秒服务延迟时实际耗时 13 分 7 秒，仅约 128
图文对/小时；单 profile 单 style 在同类延迟下可能只有约 40--55 对/小时。提高并发不能保证
绕过服务端排队或限流，调整后应先用小批次观察日志中的 planner 延迟、429 和超时，再决定是否
继续提高。以上数字是当前模型、提示长度和 API 条件下的容量参考，不是固定性能承诺。

### 预检输入和预计数量

prepared 输入运行：

```bash
python -m value_eval validate-input \
  --config configs/prepared_scenarios.yaml
```

刚由 Excel 发布场景时运行：

```bash
python -m value_eval validate-input \
  --config configs/excel_to_benchmark.yaml
```

旧 4000 配置运行：

```bash
python -m value_eval validate-input \
  --config configs/hh_bh_4000.yaml
```

输出中的关键字段：

- `scenario_count`：场景数。
- `scenario_element_count`：输入 element 数。
- `generation_job_count`：扩增后每个 profile 的机制任务数。
- `planned_questions_per_profile`：每个 profile 最终题数。
- `variants_per_scenario`：每场景变体数，0 表示 element-once。
- `missing_api_key_envs`：尚未设置的密钥环境变量。

### 小样本测试

`--max-items` 临时覆盖每个 profile 的题数上限；both 模式必须是偶数：

```bash
python -m value_eval generate-benchmark \
  --config configs/prepared_scenarios.yaml \
  --style both \
  --max-items 4
```

也可以临时覆盖每场景变体数：

```bash
python -m value_eval generate-benchmark \
  --config configs/hh_bh_4000.yaml \
  --style both \
  --variants-per-scenario 5 \
  --max-items 4
```

### 正式生成 Benchmark

```bash
python -m value_eval generate-benchmark \
  --config configs/prepared_scenarios.yaml \
  --style both
```

Excel 场景改用：

```bash
python -m value_eval generate-benchmark \
  --config configs/excel_to_benchmark.yaml \
  --style both
```

旧 4000 复现改用：

```bash
python -m value_eval generate-benchmark \
  --config configs/hh_bh_4000.yaml \
  --style both
```

`both` 严格使用旧共享图片流程：awareness 使用 planner 和 author；paired instruction 不接收
plan，只逐字复用 awareness 的图片描述。单独 awareness/instruction 都执行自己的
planner → author 流程，并且不产生共享图片元数据。

数量计算：

```text
最终题数 = 场景数 × variants_per_scenario × profile 数 × style 数
```

例如 473 个场景、每场景 5 个变体、HH/BH、both：

```text
473 × 5 × 2 × 2 = 9460 条题
```

当 `variants_per_scenario: 0` 时不使用该公式中的变体项，而是按选中的 element 数计算。旧
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

在生成 benchmark 使用的同一份 YAML 中配置：

```yaml
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

### 方式 A：Benchmark 完成后人工生成

推荐先把 `generate_during_benchmark` 保持为 `false`，确认 benchmark 全局校验通过后运行：

```bash
python -m value_eval generate-images \
  --config configs/prepared_scenarios.yaml \
  --style both
```

Excel 和旧 4000 分别替换配置路径：

```bash
python -m value_eval generate-images --config configs/excel_to_benchmark.yaml --style both
python -m value_eval generate-images --config configs/hh_bh_4000.yaml --style both
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

## 第四步（可选）：HH-instruction 越狱增强

### 选择方法和准备依赖

在生成 benchmark 使用的同一份 YAML 中添加：

```yaml
augmentation:
  enabled: true
  method: [figstep, qr, camo, mml_wr, mml_mirror, mml_rotate, himrd, cs_dj, visual_roleplay, si, viscra]

response:
  enabled: true
  mode: image_text
  datasets: [base, enabled_augmentations]
```

`method` 是方法名列表，删除某一项即可关闭该方法。主配置的 `augmentation` 块只接受 `enabled` 和 `method`，
字号、辅助模型和本地资源路径在 `value_eval/augmentation/methods/<method>/config.yaml`
中修改。`run-all` 要求 `generation.profiles` 包含 `hh`、`generation.styles` 包含
`instruction`；可以继续生成 HH/BH × awareness/instruction 四类原题。

| 方法名 | 处理方式 | 需要原 HH 图 | 额外准备 |
| --- | --- | --- | --- |
| `figstep` | 问题和空编号排成文字图 | 否 | 无 |
| `qr` | 原图底部添加关键词，改写文本问题 | 是 | 辅助文本 API |
| `camo` | 文本遮字、算术线索与图片字符索引 | 是 | 辅助文本 API |
| `mml_wr` | 词语替换后排版，保留还原字典 | 否 | NLTK 英文词性标注数据 |
| `mml_mirror` | 问题文字图水平镜像 | 否 | 无 |
| `mml_rotate` | 问题文字图旋转 180° | 否 | 无 |
| `himrd` | 将问题片段移到原图顶部，文本保留占位符 | 是 | 辅助文本 API |
| `cs_dj` | 9 张干扰图与 3 张子问题图拼成一张图 | 否 | 辅助文本 API、本地 CLIP 和干扰图库 |
| `visual_roleplay` | 角色文字、额外生成的角色图与原问题拼接 | 否 | 辅助文本 API、当前生图后端 |
| `si` | 打乱原图图块及问题词序，底部添加关键词 | 是 | 辅助文本 API |
| `viscra` | 注意力定位遮挡区域，底部添加关键词 | 是 | 辅助文本 API、本地 Qwen2.5-VL |

统一依赖使用 `requirements.txt`；MML_WR 另需安装
`averaged_perceptron_tagger_eng`，CS-DJ 和 VisCRA 需在各自方法配置中填写当前机器的
资源路径。辅助 API 默认使用 `qwen3.5-35b-a3b`，失败后回退 `deepseek-v4-flash`，
继承主配置中的服务连接。详细参数与资源准备见 [增强文档](docs/AUGMENTATION.md)
和 [部署说明](docs/DEPLOYMENT.md)。

### 预检和小样本生成

以下命令使用已有验证 run；如果 benchmark 来自其他配置，将路径替换为该配置。
分阶段命令必须保持相同 `run.id` 和 `--style`，单 instruction run 使用 `--style instruction`。

```bash
# 检查所有启用方法的源题、原图及本地配置，不调用模型。
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both --dry-run

# 先为一条 HH-instruction 生成 FigStep 和 QR。
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both \
  --method figstep --method qr --max-items 1

# 为全部 HH-instruction 补齐所有已启用方法。
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both
```

`--method` 只能选择主配置中已启用的方法，可以重复传入。`--max-items` 按源题数限制；
未覆盖全部源题时方法状态为 `partial`，需要去掉限制补齐后才能采集该方法的回应。
独立预检不加载模型权重，也不验证 API 可用性；通过后仍需做小样本运行。

### 数量与输出

当前每种方法为每条 HH-instruction 生成一条增强样本。原始集 N 条、HH-instruction H 条、
启用 M 种方法时，全部成功后的完整集为 `N + H × M` 条。验证配置每个 profile 最多
4 条，生成满额时为 8 条原题、2 条 HH-instruction，全部 11 种增强后为 `8 + 2 × 11 = 30` 条。

增强写入 `augmentations/<method>/`，完整集索引写入 `dataset/manifest.json`；原 benchmark
不被改写。中断后重跑相同命令会复用已完成条目。对已有 run 仅添加增强时使用独立命令，
因为重新执行 benchmark 或 `run-all` 仍会检查主 YAML 的 checkpoint 指纹。

## 可选：采集目标模型回应

原始集支持下面四种回应模式；11 种增强集均只支持 `image_text`。默认
`response.datasets: [base, enabled_augmentations]` 采集原题和全部已启用增强；只采集原题
可设置 `[base]`，只采集指定增强可设置 `[qr, si]`。

回应采集同时支持开放式和 MCQ。`generation.styles` 决定 benchmark 是 awareness、instruction
还是 both；`response.mode` 独立决定发送真实图片还是图片描述，以及要求开放回答还是选项字母。

| `response.mode` | 发送给目标模型 | 回答要求 | 需要先生成图片 |
| --- | --- | --- | --- |
| `image_text` | 真实图片 + question | 开放式回答，不发送选项 | 是 |
| `image_mcq` | 真实图片 + question + A/B/C/D | 严格返回一个选项字母 | 是 |
| `description_text` | `image_description` + question | 开放式回答，不发送选项 | 否 |
| `description_mcq` | `image_description` + question + A/B/C/D | 严格返回一个选项字母 | 否 |

在同一份 YAML 中配置目标模型和模式：

```yaml
response:
  # 对应 models.target。
  target_model: target
  # 四选一：image_text、image_mcq、description_text、description_mcq。
  mode: image_text
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
  --config configs/prepared_scenarios.yaml \
  --style both
```

无需修改 YAML，即可让被评测模型分别跑一条真实图片开放式和一条真实图片 MCQ。把下面的
`CONFIG` 设置为生成该 benchmark 时使用的同一份配置；两条命令会选择相同的首条 benchmark，
并写入互不覆盖的 mode 目录：

```bash
CONFIG=configs/prepared_scenarios.yaml

# 开放式：发送图片和 question，不发送选项。
python -m value_eval collect-responses \
  --config "$CONFIG" \
  --style both \
  --response-mode image_text \
  --dataset base \
  --max-items 1

# MCQ：只选择原始集，发送图片、question 和 A/B/C/D。
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

已完成的 sample 会按 mode 独立断点复用；重跑相同命令不会再次调用 API。只有确认要覆盖已有
回应时才添加 `--force`。

所有 benchmark item 内部都保留四个选项和参考答案，但开放式模式不会发送选项；四种模式均
不会发送参考答案、rationale、风险 profile 或生成 trace。MCQ 模式当前也只原样保存模型输出，
不会校验它是否确实只返回一个字母，更不会判断答案正确性。

输出为 `outputs/<execution_run_id>/responses/<target>/<mode>/<profile>.jsonl`，保存原始
`response`、`reasoning_content`、usage、耗时和 request ID，不运行 judge 或自动评分。不同
mode 使用不同子目录，因此可对同一 benchmark 分别采集开放式和 MCQ 回应。

增强完成后可单独采集回应：

```bash
python -m value_eval collect-responses \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both \
  --response-mode image_text --dataset enabled_augmentations
```

重复 `--dataset` 可选择多个方法，例如 `--dataset qr --dataset si`。目标请求使用增强样本
的 `input.text` 和 `input.images`；原题答案、选项与风险审计留在来源记录中。
增强回应单独写入 `responses/<target>/image_text/augmentations/<method>.jsonl`，可通过
`source_benchmark_id` 与原题回应配对。

## 一次运行完整流程

配置全部确认后可以运行：

```bash
# 从 Excel 开始
python -m value_eval run-all --config configs/excel_to_benchmark.yaml --style both

# 从 prepared 场景开始
python -m value_eval run-all --config configs/prepared_scenarios.yaml --style both

# 从 Excel 开始，生成四类原题、全部 11 种增强并采集原始回应。
python -m value_eval run-all --config configs/excel_to_benchmark_figstep_check.yaml --style both
```

首次运行建议按上述步骤分阶段执行，先检查 benchmark，再确认图片与增强产物。
启用增强时，`run-all` 按“benchmark → 图片 → 增强 → 回应”执行；
`response.enabled: false` 只跳过最后的回应阶段。

## 常用控制参数

| 参数 | 作用 |
| --- | --- |
| `--dry-run` | 只检查输入和配置，不发请求 |
| `--style MODE` | benchmark 题型：`both`、`awareness` 或 `instruction` |
| `--response-mode MODE` | 临时覆盖回应模式：`image_text`、`image_mcq`、`description_text` 或 `description_mcq` |
| `--method NAME` | 仅用于 `generate-augmentations`；选择已启用方法，可重复传入 |
| `--dataset NAME` | 用于 `collect-responses` / `run-all`；选择 `base`、`enabled_augmentations` 或已启用方法，可重复传入 |
| `--max-items N` | 限制当前阶段数量；独立增强按 HH-instruction 源题数，回应按所选集的总样本数；`both` benchmark 要求偶数 |
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
│   ├── images/<sample_id>.png
│   ├── preparation/                 # 辅助文本、角色图等中间结果，按需生成
│   ├── resources/                   # CS-DJ 的 CLIP 向量缓存
│   ├── samples.jsonl
│   ├── manifest.json
│   └── config.snapshot.yaml
├── dataset/manifest.json            # 原始集与当前启用增强集的索引
└── responses/<target>/<mode>/
    ├── <profile>.jsonl
    └── augmentations/<method>.jsonl
```

只有完整 HH/BH、双风格的 4000 条正式产物使用兼容文件名 `benchmark_4000.json`；单风格和
小样本文件名包含实际条数与模式。

回应文件只保存发给目标模型的输入摘要和原始输出，不进行评判。

## 测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q value_eval tests
```

新服务器交付步骤和检查项见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。
