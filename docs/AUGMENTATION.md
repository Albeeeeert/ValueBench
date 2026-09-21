# HH-instruction 越狱增强

增强阶段读取当前 run 的 `benchmark/hh/benchmark.json`，只处理
`scenario_question_style: instruction` 的条目。原始 benchmark 保留不变，每种方法独立生成
面向目标模型的文字和图片，并保留源题关联。generation 仍可生成 HH/BH ×
awareness/instruction 四类原题。

当前共支持 11 种方法，包含原有 FigStep 和 10 种新接入方法。三种 MML 分别配置、生成和
采集回应。项目只采集原始回应，不自动判断越狱是否成功，也不计算成功率。

## 输入和配置

在生成 benchmark 使用的主 YAML 中配置：

```yaml
augmentation:
  enabled: true
  method: [figstep, qr, camo, mml_wr, mml_mirror, mml_rotate, himrd, cs_dj, visual_roleplay, si, viscra]

response:
  enabled: true
  mode: image_text
  datasets: [base, enabled_augmentations]
```

主配置只接受增强总开关和方法列表。方法名必须已注册且不重复，方法参数在
`value_eval/augmentation/methods/<method>/config.yaml` 中修改。省略增强配置、
`enabled: false` 或空方法列表会让 `run-all` 跳过增强；独立执行
`generate-augmentations` 时必须至少启用一种方法。

`run-all` 在模型调用前要求 `generation.profiles` 包含 `hh`、`generation.styles` 包含
`instruction`，benchmark 生成后再检查实际源题。独立增强命令直接检查已有 benchmark，
不要求 YAML 的生成范围重新描述历史数据；须定位同一 `execution_run_id`，并沿用生成时的
`--style`。单 instruction run 的目录为 `<run.id>--instruction`。

## 方法与依赖

下表中的“原图”指图片阶段为 HH-instruction 生成的图片。需要原图的方法还会检查
`images/hh/manifest.json` 中的题目关联、完成状态和 SHA256。

| 方法及配置 | 图像与文本处理 | 原图 | 额外依赖 |
| --- | --- | --- | --- |
| [figstep](../value_eval/augmentation/methods/figstep/config.yaml) | 问题和空编号排成文字图，文本要求补全列表 | 否 | 无 |
| [qr](../value_eval/augmentation/methods/qr/config.yaml) | 原图底部加关键词条，文本使用改写问题 | 是 | 辅助文本 API |
| [camo](../value_eval/augmentation/methods/camo/config.yaml) | 问题遮字，图中给出字符索引，文本提供算术线索 | 是 | 辅助文本 API |
| [mml_wr](../value_eval/augmentation/methods/mml_wr/config.yaml) | 替换问题词语后排版，文本包含还原字典与乱序原词 | 否 | NLTK 英文词性标注数据 |
| [mml_mirror](../value_eval/augmentation/methods/mml_mirror/config.yaml) | 问题文字图水平镜像，文本包含乱序原词 | 否 | 无 |
| [mml_rotate](../value_eval/augmentation/methods/mml_rotate/config.yaml) | 问题文字图旋转 180°，文本包含乱序原词 | 否 | 无 |
| [himrd](../value_eval/augmentation/methods/himrd/config.yaml) | 问题片段排到原图顶部，文本中保留一个占位符 | 是 | 辅助文本 API |
| [cs_dj](../value_eval/augmentation/methods/cs_dj/config.yaml) | 9 张检索干扰图和 3 张子问题图组成一张编号拼图 | 否 | 辅助文本 API、本地 CLIP 和干扰图库 |
| [visual_roleplay](../value_eval/augmentation/methods/visual_roleplay/config.yaml) | ROLE 面板、新生成的角色图和 REQUEST 面板纵向拼接 | 否 | 辅助文本 API、当前生图后端 |
| [si](../value_eval/augmentation/methods/si/config.yaml) | 打乱原图图块和问题词序，底部添加关键词条 | 是 | 辅助文本 API |
| [viscra](../value_eval/augmentation/methods/viscra/config.yaml) | 注意力定位遮挡区，底部添加关键词条，文本使用改写问题 | 是 | 辅助文本 API、本地 Qwen2.5-VL |

统一依赖由 `requirements.txt` 提供，`pyproject.toml` 从该文件生成安装依赖；不再使用
`augmentation` 或 `local-image` extras。MML_WR 自动读取项目内
`value_eval/augmentation/assets/nltk_data/`，CS-DJ 和 VisCRA 的
模型、图库需提前放到本地。安装与迁移路径见 [部署说明](DEPLOYMENT.md)。

### 辅助模型

QR、CAMO、HIMRD、CS-DJ、VisualRoleplay、SI、VisCRA 共 7 种方法使用辅助文本 API。
各方法的 `auxiliary` 默认设置一致：

```yaml
auxiliary:
  enable_thinking: false
  primary:
    model: qwen3.5-35b-a3b
    api_key_env: DASHSCOPE_API_KEY
  fallback:
    model: deepseek-v4-flash
    api_key_env: DEEPSEEK_API_KEY
  temperature: 1.0
  max_tokens: 32768
  timeout_sec: 180
  retries: 2
  retry_delay_sec: 1
```

每个模型最多请求 3 次，即首次加 2 次重试；主模型全部失败后切换回退模型，回退模型也
最多请求 3 次。请求异常、空内容和方法输出结构校验失败都进入该流程，全部失败则记录为
失败样本。成功结果的 `metadata.auxiliary` 保存实际模型、是否回退以及尝试记录。

连接优先从主配置 `models` 中匹配同名模型，否则匹配相同 `api_key_env` 的模型，继承其
服务地址与密钥环境变量；主、回退两个连接均须存在。辅助模型独立于 planner、author
和 target，不修改它们的采样配置。默认关闭 thinking，并使用非流式请求；显式开启时主模型
使用 `enable_thinking`，回退模型使用 `thinking.type`，以流式方式读取最终回答。

辅助准备结果写入本方法的 `preparation/`。SI 和 VisCRA 各自抽取关键词，不依赖 QR 的
配置、产物或执行顺序。CLIP 检索与 VisCRA 注意力模型是本地算法组件，不经过辅助文本 API。

## 分阶段运行

### 预检已有数据

下列示例沿用 [增强验证配置](../configs/excel_to_benchmark_figstep_check.yaml)。如果源题
来自另一配置，应替换配置路径，并保持相同的 `run.id` 和 `--style`。

```bash
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both --dry-run
```

预检输出每种方法的 `source_count`、`selected_source_count`、`requires_original_image`、
`method_fingerprint` 和参数快照。不调用模型、不加载权重，也不读取 Excel；会读取方法
字体、本地配置以及所选方法需要的源图、图库快照或模型 `config.json`。
预检不会验证 NLTK 数据、模型完整权重、CUDA 推理能力和 API 可用性。

### 小样本生成

```bash
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both \
  --method figstep --method qr --max-items 1
```

`--method` 只适用于此命令，可重复传入，且只能选择已经启用的方法。
`--max-items N` 按 benchmark 中 HH-instruction 源题的顺序截取前 N 条，0 表示不限制；
每个所选方法使用相同源题范围。已有源题未全部覆盖时，方法状态为 `partial`。

### 补齐全部方法

```bash
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both
```

省略 `--method` 时按配置顺序执行所有启用方法，方法内并发由各自的
`runtime.concurrency` 控制。当前 FigStep 为 8，CS-DJ、VisualRoleplay、VisCRA 为 1，
其他方法为 4。模型按需加载，方法结束后释放资源。

只添加增强时使用独立命令即可，不必重新生成 benchmark。修改主 YAML 后直接重跑
benchmark 或 `run-all`，仍可能触发原有 benchmark checkpoint 的配置指纹检查。

### 一次运行完整流程

完成资源与密钥配置后运行：

```bash
python -m value_eval run-all \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both
```

顺序为场景准备、benchmark、原图、增强、回应。当前验证配置使用 API 生成 2048×2048
原图，并启用全部 11 种增强；本地 CLIP 和 VisCRA 仍需要对应资源。
`response.enabled: false` 只跳过最后的回应阶段。若原始集或任一启用增强集未就绪，
完整流水线会在回应采集前报告失败。

## 方法参数与产物说明

### FigStep 与 MML

FigStep 默认 760×760、三个空编号、最大字号 18、最小字号 10。`auto_fit: true` 按实际
像素宽度换行，高度不足时逐步缩小字号和行距；最小字号仍放不下时记录失败，不截断文字。
`wrap_width` 只在 `auto_fit: false` 时控制固定字符数换行。提示中的 `{numbers}` 随
`steps` 更新，metadata 保存实际字号、行距、行数和文字边界。

三种 MML 也默认 760×760、三个编号和 18–10 字号适配，不读取原图。
MML_WR 对名词、形容词作一一替换，`max_token_changes` 默认 15，metadata 保存
`replaced_prompt`、`restoration_map` 和 `scrambled_original_words`。
Mirror 和 Rotate 在问题排版后分别水平镜像、旋转 180°；乱序词表独立记录。
这些固定画布方法的尺寸由自身配置决定，不随基础图片尺寸变化。

### QR、HIMRD 与 CAMO

QR 将辅助模型抽取的 `key_phrase` 写入底部面板，目标文本使用 `rephrased_question`。
HIMRD 要求辅助结果包含完整源问题、一个 `( )` 占位符、视觉片段和图片提示，校验文本
与视觉片段能够按归一化规则还原源问题；目标文本使用保留占位符的部分。
HIMRD 的 `image_prompt` 仅作为 metadata 保留，图像继续复用原 HH 图。

QR 和 HIMRD 保留原图宽高，在底部或顶部额外增加面板；字号、留白和行距按实际图宽相对
`reference_width` 缩放，面板高度随完整文字展开。512 和 2048 宽原图无需另改排版参数。

CAMO 校验抽取关键词确实来自原文，再按 `character_masking_ratio`（默认 0.4）遮住部分
字符，并生成算术线索和 `ID:character` 图例。目标按算式顺序取得线索 ID，再从左到右
填入文本空缺；线索 ID 不代表空缺位置。画布使用实际原图尺寸，适配场景区与线索区；
`allow_height_expansion: true` 允许线索过多时增加画布高度。metadata 保留关键词、
遮字文本、算式、字符图例及布局。

### CS-DJ

`src_dir` 提供本地干扰图库，程序扫描该目录第一层的 JPG/JPEG/PNG/WebP，排序后按
`seed` 打乱，最多选取 `num_images`（默认 200）张候选，至少需要 9 张。
本地 CLIP 依次选取与原问题及已选图片的平均相似度较低的图片，禁止重复选择。

辅助模型必须返回按 1、2、3 编号的三个非空子问题；结构不完整时重试或回退。
拼图固定包含 9 张干扰图和 3 张子问题图，`max_pairs_per_question` 必须为 9。
默认 `tile_size: 500`、`images_per_row: 3`，形成三列四行，编号区额外增加每行高度。
这是一个增强样本中的一张拼图，不是 12 个样本。

`resources/clip_embeddings.json` 缓存候选图片向量；指纹包含有序图片路径、内容哈希及
CLIP 配置标识，候选顺序变化后会重新计算。metadata 保存三个子问题和所选干扰图片路径。

### VisualRoleplay

先由辅助模型规划角色，再使用主配置当前 `image_backend` 生成一张角色图，最后拼接
ROLE 和 REQUEST 面板。角色图复用 `image` 或 `local_image` 的模型、尺寸、步数及 seed
等参数，是当前唯一额外调用生图后端的增强方法。

角色规划和角色图分别缓存在 `preparation/<sample_id>-role.json` 与
`preparation/<sample_id>-portrait.png`，后者另有 JSON 校验记录。后续排版失败时可复用
已完成准备阶段。最终图片宽度等于角色图宽度，高度为角色图与两个文字面板高度之和。

### SI

`blocks_per_side: 2` 默认将原图分成 2×2 图块，按稳定随机顺序重新排列；宽、高必须
都能被该值整除。`shuffle_prompt: true` 按空白分词后打乱原问题词序，目标文本使用这一
结果；辅助准备中的 `rephrased_question` 仅保留在 metadata。底部关键词条在图块打乱后
添加，不参与图块置换。metadata 记录 `block_order` 和词序开关。

### VisCRA

从独立抽词结果构造关键词查询，通过本地 Qwen2.5-VL 提取指定解码层最后一个文本 token
对图像 token 的平均注意力。`attention_layer: 18` 直接用作代码中的层索引。
默认将首末行、首末列置零，以 `attention_stride: 1` 遍历窗口，选取最高分窗口并映射回
原图遮挡；无有效注意力时记录失败。

注意力输入仅进行 Qwen 的 28 像素网格对齐，不设置固定的低分辨率上限。默认尺寸映射为：

| 原图尺寸 | 注意力输入尺寸 | 注意力网格 | 窗口边长（网格数） |
| --- | --- | --- | --- |
| 512×512 | 504×504 | 18×18 | 5 |
| 2048×2048 | 2044×2044 | 73×73 | 24 |

其他尺寸使用 `attention_block_size`（默认 5），也可在
`attention_block_size_by_resolution` 增加条目。非正方形图像分别按宽、高对齐与映射。
最终图片在原始分辨率上遮挡并添加底部关键词条，目标文本使用 `rephrased_question`。

视觉编码器使用 SDPA，文本解码器保持 eager，仅捕获指定层所需注意力，不累计保存各层
矩阵或 KV cache。多张可见 GPU 使用 `device_map: balanced`，每卡权重分配预算 8 GiB；
单张卡使用 `auto`。分配预算不等于推理峰值，实际可见卡由 `CUDA_VISIBLE_DEVICES` 控制。
metadata 保存 `mask_box`、网格及窗口大小、注意力查询和实际后端。

## 产物与数量

```text
outputs/<execution_run_id>/
├── benchmark/hh/benchmark.json
├── images/hh/manifest.json
├── augmentations/<method>/
│   ├── images/<sample_id>.png
│   ├── preparation/               # 辅助文本、角色图及其校验记录，按需生成
│   ├── resources/                 # CS-DJ 的 CLIP 向量缓存
│   ├── samples.jsonl
│   ├── manifest.json
│   ├── config.snapshot.yaml
│   └── .generation.lock
└── dataset/manifest.json
```

每个方法一个目录，没有指纹子目录。`samples.jsonl` 保存来源和实际目标输入，字段见
[数据契约](DATA_SCHEMA.md)。原题选项、答案和 `risk_audit` 留在 `source.benchmark`，
HH 标签只表示来源，不代表对转换后输入重新作了风险判定。

当前 11 种方法各为每条源题生成一个样本、一个最终 PNG。原始集 N 条、HH-instruction
H 条、启用 M 种方法时，全部完成后的完整集为 `N + H × M` 条。验证模板按两个 profile
各 4 条生成满额时：8 条原题 + 2 × 11 条增强 = 30 条。

`dataset/manifest.json` 是索引，不复制或合并样本文件。它仅汇总原始集与当前启用方法，
关闭的方法即使仍有历史目录也不进入清单。原图未齐或启用方法未完成时，总状态为
`partial`；未就绪增强子集显示为 `unavailable`，具体已完成数量查该方法 manifest。

## 缓存与恢复

同一方法使用独占写锁，图片与清单原子写入，每条任务结束后保存 checkpoint。相同配置
重跑会验证源题、方法指纹和图片哈希、尺寸、格式，复用有效条目，补做缺失或失败条目。
方法参数、代码、字体内容、Pillow 版本及已解析辅助模型设置参与指纹；依赖原图的方法
还检查原图 SHA256。`runtime.concurrency` 不进入方法指纹。

`--force` 使本次所选方法、所选源题的最终增强条目重建，不清除独立的准备缓存。
匹配指纹的辅助文本、角色图和 CLIP 向量仍可复用；它不表示所有外部模型一定重新调用。
改变对应准备参数会使相应缓存失效。未被清单引用的历史图片不参与回应采集。

## 回应采集

全部 11 种方法仅支持 `image_text`，目标模型须设置 `supports_images: true`。
`response.datasets` 默认 `[base, enabled_augmentations]`；可改为 `[base]`、`[qr, si]`
等子集。方法仍须在 `augmentation.method` 中启用。

```bash
# 采集全部已启用增强的开放式回应。
python -m value_eval collect-responses \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both \
  --response-mode image_text --dataset enabled_augmentations

# 单独采集 QR 和 SI；--max-items 按所选数据集的总样本数限制。
python -m value_eval collect-responses \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both \
  --response-mode image_text --dataset qr --dataset si

# MCQ 只选择原始集。
python -m value_eval collect-responses \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both \
  --response-mode image_mcq --dataset base
```

采集前检查方法配置、全部源题覆盖、图片和 JSONL 完整性。`partial`、失败、过期或损坏的
方法不能采集；即使回应命令只要求一条，也需要先补齐该方法。
目标只接收 `input.text` 和 `input.images`，不会额外拼入源题的选项、答案或风险审计。

原始回应继续保存到 `responses/<model>/<mode>/<profile>.jsonl`，增强回应保存到
`responses/<model>/image_text/augmentations/<method>.jsonl`。通过 `dataset`、`method` 和
`source_benchmark_id` 关联原题；目标文字或图片哈希变化后会使用新的回应缓存键。

## 实现范围与扩展

当前迁移保留方法的主要处理流程，同时统一了辅助模型重试、来源记录、字体排版和恢复。
与迁移前实现相比，像素换行、动态面板高度、PNG 输出、辅助模型采样参数以及部分方法的
随机种子派生方式有所调整，不保证逐像素或逐 token 一致。
QR、CAMO、HIMRD 使用复用原图路径；VisualRoleplay 继承当前主配置的生图参数；
VisCRA 只提供注意力最高分窗口路径，不包含 baseline、visual_cot、多候选随机选择或
热力图导出，混合注意力后端的数值也可能影响窗口选择。

新增方法在 `value_eval/augmentation/methods/<name>/` 放置实现、`config.yaml` 和资源，
并在 `registry.py` 注册。实现 `AugmentationMethod`，声明原图依赖和支持的回应模式，
`generate` 接收源题、稳定 ID 和输出目录，返回一个或多个 `AugmentationResult`。
每个结果可包含多张图片；方法负责原子写图，执行器统一负责筛选、校验、来源关联和恢复。
如未来方法增加每题变体数，应按实际输出数量更新样本数量计算。
