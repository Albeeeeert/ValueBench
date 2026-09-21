# 部署与验收

## 部署步骤

1. 将 `Value_eval/` 放置在目标服务器的工作目录中，后续命令均在该项目根目录执行。
2. 使用 Linux 和 Python 3.10+；Windows 使用 WSL/Linux。
3. 创建虚拟环境并安装 `requirements.txt`。
4. 从 `.env.example` 创建 `.env`，只填写实际使用的 API Key。
5. 复制模板为运行配置，填写唯一 `run.id`、模型名称、endpoint 和所需功能开关。
6. 将正式表格放入 `inputs/excel/`，或配置本地 prepared 场景目录。
   本地生图需修改 `local_image.model_path`；无需本地权重时改用 `image_backend: api`。
   启用越狱时，按下文[本地模型与越狱资源](#本地模型与越狱资源)准备所选方法需要的文件和连接。
7. 先运行输入验证和 `--dry-run`，再运行小样本，最后放开条数限制。

以下以 Excel 路线为例创建运行配置：

```bash
cp configs/excel_to_benchmark.yaml configs/my_run.yaml
CONFIG=configs/my_run.yaml
```

编辑运行配置，完成输入、模型、图片后端和越狱资源设置后，依次执行：

```bash
python -m value_eval validate-excel --config "$CONFIG"
python -m value_eval run-all --config "$CONFIG" --dry-run
python -m value_eval run-all --config "$CONFIG" --max-items 4
```

命令默认同时写终端和 `logs/<execution_run_id>/pipeline.log`。长任务可在另一个终端实时查看：

```bash
RUN_ID=my_run
tail -f "logs/${RUN_ID}/pipeline.log"
```

中断后用相同配置和命令重跑，程序复用已完成 checkpoint，继续处理未完成任务。
正式批次使用独立的 `run.id` 和所需题数设置。

本地推理使用 `CUDA_VISIBLE_DEVICES` 指定可见 GPU，例如：

```bash
CUDA_VISIBLE_DEVICES=0,1 python -m value_eval run-all --config "$CONFIG" --max-items 4
```

## 本地模型与越狱资源

`requirements.txt` 统一管理流水线、本地生图及越狱依赖；PyTorch 固定为 2.8.0。
已确认支持 CUDA 12.8 构建的环境可先安装该构建，再装统一依赖：

```bash
python -m pip install 'torch==2.8.0' --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements.txt
```

### 配置文件与资源位置

运行配置为命令 `--config` 指定的文件，例如 `configs/my_run.yaml`。
方法配置固定从 `value_eval/augmentation/methods/<方法名>/config.yaml` 读取，例如
CS-DJ 对应 `value_eval/augmentation/methods/cs_dj/config.yaml`。

| 文件 | 修改内容 |
| --- | --- |
| `configs/my_run.yaml` | `augmentation.enabled`、`augmentation.method`；`models` 中的 API 连接；顶层 `image_backend` 及 `image`、`local_image` 图片生成设置 |
| `value_eval/augmentation/methods/<方法名>/config.yaml` | 该方法的 `parameters` 资源路径、排版参数及 `auxiliary` 辅助模型 |
| `.env` | 上述配置通过 `api_key_env` 引用的 API 密钥 |

以下 YAML 均为需要修改的局部字段，合并到对应文件的已有配置块中。
图库示例放在项目内的 `inputs/images/cs_dj/`；大型模型示例放在服务器的 `/data/models/`。
模型也可放在其他磁盘，将配置中的绝对路径改为实际位置即可。

### 模型下载

按启用功能下载对应的官方模型仓库。下载目录示例为 `/data/models/`，可替换为服务器上有写入权限的模型目录。

| 用途 | 官方原始仓库 | 本地目录 | 配置位置 |
| --- | --- | --- | --- |
| 本地图片生成 | [Qwen/Qwen-Image-2512](https://huggingface.co/Qwen/Qwen-Image-2512) | `/data/models/Qwen-Image-2512` | `configs/my_run.yaml` → `local_image.model_path` |
| CS-DJ 图片检索 | [openai/clip-vit-large-patch14-336](https://huggingface.co/openai/clip-vit-large-patch14-336) | `/data/models/clip-vit-large-patch14-336` | `methods/cs_dj/config.yaml` → `parameters.clip_path` |
| VisCRA 图片关注区域分析 | [Qwen/Qwen2.5-VL-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct) | `/data/models/Qwen2.5-VL-7B-Instruct` | `methods/viscra/config.yaml` → `parameters.attention_model_path` |

表中的 `methods/` 位于 `value_eval/augmentation/` 下。使用
[Hugging Face 下载工具](https://huggingface.co/docs/huggingface_hub/guides/cli)取得完整仓库：

```bash
python -m pip install huggingface_hub

# 本地图片生成
hf download Qwen/Qwen-Image-2512 --local-dir /data/models/Qwen-Image-2512

# CS-DJ
hf download openai/clip-vit-large-patch14-336 --local-dir /data/models/clip-vit-large-patch14-336

# VisCRA
hf download Qwen/Qwen2.5-VL-7B-Instruct --local-dir /data/models/Qwen2.5-VL-7B-Instruct
```

每条下载命令对应表中的一个功能。下载完成后，将完整目录路径填入对应配置字段。
程序运行时从本地加载模型，保留仓库中的配置、全部权重、分片索引、预处理及 tokenizer 文件。

### 图片生成

HH/BH 原题图片与 VisualRoleplay 角色图共用图片生成模块，统一读取
`configs/my_run.yaml` 的 `image_backend`、`image` 和 `local_image`。选择一种生图方式：

| 方式 | 所需资源 | 运行配置 |
| --- | --- | --- |
| API | 千问图片生成服务及 API 密钥 | `image_backend: api`，填写 `image` |
| 本地 | Qwen-Image-2512 完整模型目录及可用 CUDA GPU | `image_backend: local`，填写 `local_image` |

API 方式使用[千问图像生成接口](https://help.aliyun.com/zh/model-studio/qwen-image-api)，
在 `.env` 中填写 `DASHSCOPE_API_KEY`，在运行配置中设置：

```yaml
image_backend: api
image:
  model: qwen-image-2.0
  api_key_env: DASHSCOPE_API_KEY
  endpoint: https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
  task_endpoint: https://dashscope.aliyuncs.com/api/v1/tasks
  size: "2048*2048"
  async_call: false
```

本地方式使用[模型下载](#模型下载)中的 Qwen-Image-2512，运行配置为：

```yaml
image_backend: local
local_image:
  model_path: /data/models/Qwen-Image-2512
  size: "512*512"
  dtype: bfloat16
  device_map: balanced
  cpu_offload: none
  concurrency: 1
```

模型目录应包含 `model_index.json` 及 `scheduler/`、`text_encoder/`、`tokenizer/`、
`transformer/`、`vae/` 等完整组件。`balanced` 在当前可见 GPU 间分配模型，
使用 `CUDA_VISIBLE_DEVICES` 指定设备；显存与卸载参数见[图片后端配置](CONFIGURATION.md#图片后端)。
VisualRoleplay 通过这组配置自动生成角色图，方法文件只设置辅助文本模型和排版参数。

### 按方法准备资源

| 方法 | 需要准备的资源 | 是否需要原 HH 图片 |
| --- | --- | --- |
| `figstep`、`mml_mirror`、`mml_rotate` | 项目自带字体 | 否 |
| `mml_wr` | 项目自带字体与 NLTK 英文词性标注数据 | 否 |
| `qr`、`camo`、`himrd`、`si` | 项目自带字体、[辅助文本 API](#文本与多模态-api) | 是 |
| `cs_dj` | 项目自带字体、辅助文本 API、[干扰图库与本地 CLIP](#cs-dj干扰图库与-clip) | 否 |
| `visual_roleplay` | 项目自带字体、辅助文本 API、HH/BH 共用的[图片生成配置](#图片生成) | 否 |
| `viscra` | 项目自带字体、辅助文本 API、[本地 Qwen2.5-VL](#viscra本地注意力模型) | 是 |

原 HH 图片由 `generate-images` 生成，保存在本次运行的 `outputs/<execution_run_id>/images/hh/`，
程序按 manifest 自动关联源题。按 README 完成题目和原图生成后，再执行越狱阶段。

字体位于 `value_eval/augmentation/assets/fonts/ARIAL.TTF`，各方法的
`parameters.font_path: ../../assets/fonts/ARIAL.TTF` 相对该方法的 `config.yaml` 所在目录解析。
MML_WR 自动读取 `value_eval/augmentation/assets/nltk_data/`，部署时保持该目录及内部结构完整。

### CS-DJ：干扰图库与 CLIP

干扰图库是一组用于拼图的普通图片，例如风景、动物、建筑和日常物品照片。
CS-DJ 使用 CLIP 计算图片与问题的相关性，选出 9 张干扰图，再与 3 张子问题文字图拼成一张图。
图库仅需图片文件，无需标签、答案或 JSON 清单。

在项目根目录创建图库目录，并将准备好的图片直接放入其中：

```bash
mkdir -p inputs/images/cs_dj
```

```text
Value_eval/inputs/images/cs_dj/
├── landscape.jpg
├── animal.png
├── building.webp
└── ...                 # 至少 9 张可读取的图片
```

支持 `.jpg`、`.jpeg`、`.png`、`.webp`，只读取该目录第一层的图片。
`num_images` 是候选图库的读取上限，默认 200；图库及读取上限均需至少为 9。
最终每条源题使用 9 张干扰图，`max_pairs_per_question` 保持为 9。

将完整的 CLIP ViT-L/14@336 模型目录放到 `/data/models/clip-vit-large-patch14-336/`。
目录应包含 `config.json`、模型权重、`preprocessor_config.json`、tokenizer 配置及词表文件。
CLIP 用于筛选图库图片，在生成越狱样本时从本地加载。

编辑 [value_eval/augmentation/methods/cs_dj/config.yaml](../value_eval/augmentation/methods/cs_dj/config.yaml)：

```yaml
parameters:
  src_dir: inputs/images/cs_dj
  clip_path: /data/models/clip-vit-large-patch14-336
  device: cuda
  num_images: 200
  max_pairs_per_question: 9
```

`src_dir` 相对运行配置中的 `run.root` 解析。配置文件放在 `configs/` 且 `run.root: ..` 时，
上述路径就是项目内的 `inputs/images/cs_dj/`。`clip_path` 填模型目录的绝对路径。

### VisCRA：本地注意力模型

VisCRA 使用本地 Qwen2.5-VL 分析原 HH 图片中的关注区域，确定遮挡位置，再生成越狱样本。
该模型在目标服务器执行图片分析；原题使用 API 生图时，VisCRA 仍需单独准备它。

将完整的 Qwen2.5-VL-7B-Instruct 模型目录放到 `/data/models/Qwen2.5-VL-7B-Instruct/`，
包含 `config.json`、全部权重分片及索引、图像预处理配置、tokenizer 文件和对话模板。
编辑 [value_eval/augmentation/methods/viscra/config.yaml](../value_eval/augmentation/methods/viscra/config.yaml)：

```yaml
parameters:
  attention_model_path: /data/models/Qwen2.5-VL-7B-Instruct
  device: cuda
```

`attention_model_path` 填模型目录的绝对路径；其余注意力窗口与排版参数保留方法模板设置。
CLIP、Qwen2.5-VL 和本地 Qwen-Image 均从上述目录读取完整权重，运行时不会自动下载模型。

### 文本与多模态 API

场景构建、题目生成和目标模型回应的连接在 `configs/my_run.yaml` 的 `models` 中配置。
各条目的 `model` 填模型名称，`base_url` 填服务地址，`api_key_env` 引用项目根目录 `.env` 中的密钥变量。

| 服务 | 模板使用的模型 | 官方地址 | 密钥变量 |
| --- | --- | --- | --- |
| 阿里云百炼 | `qwen3.5-35b-a3b`、`qwen3-max`、`qwen3-vl-8b-thinking` | [模型目录](https://help.aliyun.com/zh/model-studio/models)、[API Key 申请](https://help.aliyun.com/zh/model-studio/get-api-key) | `DASHSCOPE_API_KEY` |
| DeepSeek | `deepseek-flash`、`deepseek-v4-flash` | [API 文档与密钥入口](https://api-docs.deepseek.com/zh-cn/) | `DEEPSEEK_API_KEY` |

API 服务使用账户已开通的模型，接口地址与密钥按服务地域配套填写。

`qr`、`camo`、`himrd`、`cs_dj`、`visual_roleplay`、`si`、`viscra` 需要文本模型完成关键词提取、
问题改写、子问题拆分或角色描述。按以下三个位置配置所选方法的主模型及备用模型：

| 修改位置 | 填写内容 |
| --- | --- |
| `value_eval/augmentation/methods/<方法名>/config.yaml` 的 `auxiliary.primary`、`auxiliary.fallback` | `model` 填实际调用的模型名称，`api_key_env` 填密钥变量名 |
| `configs/my_run.yaml` 的 `models` | 相应服务的 `model`、`base_url` 和 `api_key_env`；程序优先匹配同名模型，其次匹配相同密钥变量名，读取该条目的连接 |
| 项目根目录 `.env` | 对应变量的真实密钥 |

方法模板默认使用以下主、备用模型：

```yaml
auxiliary:
  primary:
    model: qwen3.5-35b-a3b
    api_key_env: DASHSCOPE_API_KEY
  fallback:
    model: deepseek-v4-flash
    api_key_env: DEEPSEEK_API_KEY
```

使用 Excel 模板时，主模型连接来自 `models.taxonomy`，备用模型连接来自 `models.planner`。
使用 prepared 或 4000 条模板时，主模型通过 `DASHSCOPE_API_KEY` 匹配 `models.author` 的连接，
备用模型仍匹配 `models.planner`。在运行配置中修改对应条目的 `base_url`，并在 `.env` 中填写
`DASHSCOPE_API_KEY` 和 `DEEPSEEK_API_KEY`。
更换辅助模型时，同步修改所选方法文件中的模型名称和密钥变量名，并在运行配置的 `models`
中配置对应服务连接。主、备用连接均需配置。

## 部署验收

在目标服务器安装依赖后，运行示例输入检查：

```bash
python -m value_eval validate-input --config configs/prepared_scenarios.yaml
python -m value_eval validate-excel --config configs/excel_to_benchmark.yaml
```

完成运行配置后，按所选方法核对以下项目：

| 项目 | 验收要求 |
| --- | --- |
| CS-DJ 图库 | `parameters.src_dir` 第一层至少有 9 张可读取的受支持格式图片 |
| 本地模型 | 模型目录位于配置指定位置，配置文件、全部权重及预处理文件完整 |
| API 连接 | 运行配置中的接口地址正确，主、备用辅助模型和生图服务的密钥已填写 |
| 项目资源 | 方法配置中的字体可读取，MML_WR 的 NLTK 数据目录完整 |

按 README 完成 benchmark 与原图生成后，检查本次启用的越狱方法，并各生成一条源题的小样本：

```bash
python -m value_eval generate-augmentations --config "$CONFIG" --style both --dry-run
python -m value_eval generate-augmentations --config "$CONFIG" --style both --max-items 1
```

预检检查源题、方法配置和资源文件；小样本运行验证 API 请求、完整权重加载与实际推理。
确认样本正常后，去掉 `--max-items 1` 生成全部源题，再采集目标模型回应。
内置两个 prepared 场景按双 profile、双题型生成时，应有 8 条题、4 张共享图片，
回应成功或复用共 8 条；启用 FigStep 后共 10 条样本。
验收计数见[数据契约](DATA_SCHEMA.md#回应历史与验收)。
