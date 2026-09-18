# 独立部署清单

## 复制后检查

1. 复制整个 `Value_eval/` 目录，源码不需要上级仓库；本地模型与外部图库按下文单独准备。
2. 确认服务器 Python 版本不低于 3.10。
3. 创建虚拟环境并安装 `requirements.txt`。
4. 从 `.env.example` 创建 `.env`，只填写实际使用的 API Key。
5. 复制并修改一份 YAML，设置唯一 `run.id`、模型名称和 endpoint。
6. 将正式表格放入 `inputs/excel/`，或配置本地 prepared 场景目录。
7. 启用增强时，检查各方法配置中的字体、辅助模型连接和本地资源路径。
8. 先运行输入验证和 `--dry-run`，再运行小样本，最后放开条数限制。

```bash
python -m value_eval validate-excel --config configs/excel_to_benchmark.yaml
python -m value_eval run-all --config configs/excel_to_benchmark.yaml --dry-run
python -m value_eval run-all --config configs/excel_to_benchmark.yaml --max-items 4
```

命令默认同时写终端和 `logs/<execution_run_id>/pipeline.log`。长任务可在另一个终端实时查看：

```bash
RUN_ID=my_run
tail -f "logs/${RUN_ID}/pipeline.log"
```

中断后用相同配置和命令重跑会复用已完成 checkpoint；不要添加 `--force`，除非明确要覆盖
本次 run 的已有结果。

也可以通过启动脚本指定配置和解释器：

```bash
PYTHON_BIN=/path/to/python3.11 \
VALUE_EVAL_CONFIG=configs/excel_to_benchmark.yaml \
bash scripts/run_pipeline.sh --max-items 4
```

## 增强依赖与本地资源

统一安装依赖，使用 CUDA 12.8 构建时先安装项目固定的 PyTorch 版本：

```bash
python -m pip install 'torch==2.8.0' --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements.txt
```

`requirements.txt` 包含流水线、增强和本地生图依赖；`pyproject.toml` 使用同一清单，
不再单独安装 `augmentation` 或 `local-image` extras。安装 Python 包不包含模型权重、
NLTK 数据或 CS-DJ 干扰图库。

| 使用范围 | 部署时准备 |
| --- | --- |
| 全部方法 | 保留仓库内字体；FigStep 使用本方法 `assets/fonts/`，其他方法使用 `augmentation/assets/fonts/` |
| QR、CAMO、HIMRD、CS-DJ、VisualRoleplay、SI、VisCRA | 主配置提供 Qwen、DeepSeek 的连接和密钥环境变量，供辅助主模型与回退模型继承 |
| MML_WR | NLTK 的 `averaged_perceptron_tagger_eng` 数据 |
| CS-DJ | 本地 CLIP 完整权重目录，以及第一层至少含 9 张可读取图片的图库 |
| VisCRA | 本地 Qwen2.5-VL 完整权重、tokenizer 和图像预处理配置，默认使用 CUDA |
| VisualRoleplay | 当前 `image_backend` 的 API 连接，或本地生图完整权重与运行环境 |

MML_WR 数据在部署阶段安装，生成过程不会自动下载：

```bash
python -m nltk.downloader averaged_perceptron_tagger_eng
```

离线机器需提前复制 NLTK 数据并设置 `NLTK_DATA`。CLIP、Qwen2.5-VL 和本地生图模型
均通过 `local_files_only=True` 读取，不会在运行中自动下载权重。

当前模板含原机器的资源路径，复制后必须按所选方法修改：

| 配置文件 | 字段 | 路径解析 |
| --- | --- | --- |
| 主 YAML | `local_image.model_path` | 相对 `run.root` 或绝对路径，仅本地生图使用 |
| `methods/cs_dj/config.yaml` | `parameters.src_dir` | 相对 `run.root` 或绝对路径 |
| `methods/cs_dj/config.yaml` | `parameters.clip_path` | 建议绝对路径；相对值由加载器按工作目录解释 |
| `methods/viscra/config.yaml` | `parameters.attention_model_path` | 建议绝对路径；相对值由加载器按工作目录解释 |
| 各方法 `config.yaml` | `parameters.font_path` | 相对方法配置所在目录，保留仓库布局时无需修改 |

表中 `methods/` 位于 `value_eval/augmentation/`。辅助模型优先从主 `models` 匹配同名
模型，否则按密钥环境变量名匹配连接；主、回退都需要可用连接。默认关闭 thinking、
温度 1.0、输出预算 32768，每个模型最多尝试 3 次。完整字段见
[配置手册](CONFIGURATION.md#auxiliary)。

GPU 编号遵循 `CUDA_VISIBLE_DEVICES`。FigStep、QR、CAMO、MML、HIMRD、SI 的本地
转换不执行 GPU 推理；CS-DJ、VisCRA 使用各自 `device`，VisualRoleplay 取决于生图后端。
API 生图配置也可能同时启用需要本地 GPU 的增强方法。

## 增强小样本验收

使用 [增强验证配置](../configs/excel_to_benchmark_figstep_check.yaml)，设置新 `run.id`
和本机资源路径，再执行：

```bash
python -m value_eval run-all \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both --dry-run

python -m value_eval run-all \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both --max-items 4
```

模板默认 API 原图为 2048×2048，启用 11 种增强和回应。满额成功时应有 8 条原题、
22 条增强，`dataset/manifest.json` 为 `completed` 且 `sample_count: 30`。
如先只检查生成产物，在开始新 run 前将 `response.enabled` 设为 `false`，增强仍会执行。
`--dry-run` 不加载增强模型权重，也不验证辅助 API 调用和 NLTK 数据，需用实际小样本验收。

已有 benchmark 与原图时，可独立检查并补做方法：

```bash
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both --dry-run

python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both \
  --method figstep --method qr --max-items 1
```

检查所选方法的图片、`samples.jsonl`、来源关联和 manifest；去掉 `--max-items` 补齐方法
后再采集回应。使用全量方法时同时确认辅助准备、角色图、CLIP 检索及注意力遮挡结果。
恢复和选择部分方法的规则见 [增强文档](AUGMENTATION.md)。

## 隔离验收

在临时目录中复制项目，进入复制后的目录运行测试和输入检查。以下命令不应访问原仓库：

```bash
python -m unittest discover -s tests -v
python -m value_eval validate-input --config configs/prepared_scenarios.yaml
python -m value_eval validate-excel --config configs/excel_to_benchmark.yaml
```

如只检查增强集成，可单独运行：

```bash
python -m unittest tests.test_augmentation tests.test_migrated_augmentations -v
```

其中增强离线测试使用替身模拟辅助 API、检索、注意力模型和角色生图，验证结构、转换、
来源及恢复，不代表已经通过真实模型的效果或显存验收。

迁移前可检查本地模型和图库路径：

```bash
rg -n 'model_path:|src_dir:|clip_path:|attention_model_path:|font_path:' \
  configs value_eval/augmentation/methods
```

确认启用方法引用的资源均存在于新机器。样本输入使用 run 内相对路径，但增强配置快照
和部分 metadata 可记录外部资源的绝对路径；迁移后修改配置会使对应增强缓存失效。
