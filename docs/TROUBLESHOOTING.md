# 常见问题

## Python 语法错误

项目要求 Python 3.10+。部分服务器的 `python` 或 `python3` 仍指向旧版本，先执行
`python --version`，必要时显式设置 `PYTHON_BIN`。

## 找不到工作表或表头

确认 YAML 中的 `sheet_name` 与 Excel 标签完全一致，并核对五个中文表头。不要用视觉上
相似但编码不同的括号替换 `场景描述（评判标准）`。

## 找不到 API Key

错误会显示缺失的环境变量名。确认 `.env` 路径由 `run.env_file` 指向，变量名与
`models.*.api_key_env` 或 `image.api_key_env` 一致。不要在 YAML 中直接写密钥。

## prepared 场景不存在

Excel 模式需要先成功执行 `prepare-scenarios`，或直接使用 `run-all`。使用
`--no-publish` 时只生成暂存结果，不能紧接着运行下游完整链路。

## manifest hash 不一致

场景文件在 manifest 生成后被修改。重新构建 manifest，或恢复对应版本的场景文件；
不要手动删除单条 SHA256 来绕过来源检查。

## 模型返回无法解析的 JSON

检查 `scenario_preparation/work/<fingerprint>/taxonomy/` 中保存的 prompt、raw 和 error
文件。确认服务支持 OpenAI-compatible JSON mode；必要时通过 `models.*.extra_body` 传入
供应商参数。

## 图片被审核拒绝

图片 manifest 会把任务标记为 `moderated`。这属于单条图片失败，不会伪造占位图片。
应检查对应 `image_description`，调整生成内容后使用新 `run.id` 重跑。

## 增强方法未启用或方法名错误

`unknown augmentation method(s)` 表示名称未注册；名称区分大小写，使用
`figstep`、`qr`、`camo`、`mml_wr`、`mml_mirror`、`mml_rotate`、`himrd`、`cs_dj`、
`visual_roleplay`、`si`、`viscra`。`method` 必须为列表，不接受单个字符串或重复名称。

`augmentation method(s) not enabled` 表示 `--method` 或回应数据集选择了未启用方法。
检查主 YAML 的 `augmentation.enabled: true` 及 `augmentation.method`。
方法参数应写入各方法目录的 `config.yaml`，不能写入主 `augmentation` 块。

## 增强找不到 HH-instruction

`run-all` 要求 generation 同时包含 `hh` 和 `instruction`，可以继续生成其他三类。
独立增强则检查已有 `benchmark/hh/benchmark.json`，其中必须有 instruction 条目。
确认配置指向源题所在 run，并沿用生成时的 `--style`；单 instruction 的目录有
`--instruction` 后缀，不能用 `--style both` 读取。

## 增强找不到原 HH 图片

QR、CAMO、HIMRD、SI、VisCRA 需要有效原图。检查 `images/hh/manifest.json` 中是否有
对应 `benchmark_id`、有效完成状态、图片路径及匹配 SHA256。使用生成该 benchmark 的
同一配置和题型执行 `generate-images` 补图，再运行增强。单独复制 PNG 到目录中不会
自动建立 manifest 关联；也不要删除哈希字段来绕过校验。

## 辅助模型连接缺失或重试耗尽

`requires a configured connection using ...` 表示方法配置中的主模型或回退模型无法在
主 YAML 的 `models` 找到同名模型或相同 `api_key_env` 连接；即使主模型可用，回退连接
也需配置。默认使用 `DASHSCOPE_API_KEY` 与 `DEEPSEEK_API_KEY`，密钥值写入 `.env`。

`augmentation auxiliary exhausted primary and fallback` 表示请求或结果校验在两组模型
均已耗尽尝试。默认每个模型为首次加 2 次重试，共最多 6 次请求。检查终端、日志和
`augmentations/<method>/manifest.json` 的失败 entry，区分连接、鉴权、空回复和结构错误。
CS-DJ 必须返回三条编号子问题；HIMRD 必须能由文本与视觉片段还原源题。修复原因后重跑
所选方法即可，其他成功条目会复用。

## NLTK 词性标注数据缺失

MML_WR 的 `LookupError` 若提及 `averaged_perceptron_tagger_eng`，说明已安装 Python 包
但缺少词性标注数据。在运行项目的同一环境执行：

```bash
python -m nltk.downloader averaged_perceptron_tagger_eng
```

离线服务器提前复制数据并配置 `NLTK_DATA`。预检和增强生成不会自动下载这些资源。

## CS-DJ 图库或 CLIP 加载失败

检查 `methods/cs_dj/config.yaml` 的 `src_dir`、`clip_path` 和 `device`。图库只扫描第一层
JPG/JPEG/PNG/WebP，不递归子目录；选中候选数至少为 9，`num_images` 也不能限制到 9 以下。
CLIP 路径必须包含完整本地模型和预处理资源，仅有 `config.json` 可能通过预检但无法推理。
固定拼图要求 `max_pairs_per_question: 9`。

## VisCRA 本地模型或显存错误

检查 `attention_model_path` 指向完整 Qwen2.5-VL 目录，GPU 对当前进程可见，依赖与
`requirements.txt` 一致。预检只读取配置，不加载权重，不能证明显存充足。
默认模型按源图分辨率作 patch 对齐；2048 原图比 512 原图需要更多显存。
多卡权重分配的每卡 8 GiB 预算不代表推理峰值，也不会拆分单个注意力算子。

使用空闲且资源充足的可见卡，避免与其他大模型任务竞争。检查窗口参数是否能放入网格：
默认 512×512 使用 18×18 网格和 5×5 窗口，2048×2048 使用 73×73 网格和 24×24 窗口。
若无有效注意力或网格不匹配，方法会失败，不生成随机遮挡作为替代。

## 文字溢出或 SI 分块失败

FigStep 和 MML 在最小字号下仍放不下完整内容时会报错。调整方法配置中的画布尺寸、
最大/最小字号等排版参数后重跑；不应截断原题。SI 的原图宽、高必须都能被
`blocks_per_side` 整除，默认值为 2。

QR、HIMRD、SI、VisCRA 的文字面板在原图之外增加高度，因此最终图片高于原图是正常
产物。VisualRoleplay 的角色图须符合当前生图后端尺寸，最终图再增加上下文字面板。

## 数据集是 partial，或回应拒绝 incomplete/stale

先检查 `dataset/manifest.json` 的 `subsets`。原始集缺图会使总状态为 `partial`；
任一已启用方法未全量完成、配置过期或文件损坏时，该子集为 `unavailable`。
已完成源题数量和具体错误在对应方法的 manifest 中。

增强 `--max-items 1` 只生成首条源题，不代表该方法已经完整。去掉限制补齐所需方法：

```bash
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both --method qr
```

方法只有全量完成才能采集回应，`collect-responses --max-items 1` 也不会跳过此检查。
若只需要 QR 回应，可用 `--dataset qr`；完整 `run-all` 则要求所有已启用子集都就绪。

## 增强不支持当前回应模式

全部 11 种增强只支持 `image_text`。采集增强时将模式改为 `image_text`，并确认 target
设置 `supports_images: true`。采集原题 MCQ 或描述模式时选择 `--dataset base`，避免
默认的 `enabled_augmentations` 一同进入不兼容模式。

## 增强写锁、配置变化与 force

`augmentation is already running` 表示另一个进程正持有该方法的写锁，等待其完成后重试。
`.generation.lock` 文件在正常结束后仍可存在，锁随文件句柄释放；不要删除锁文件来启动
第二个并发写进程。

给已有 run 添加增强后若 benchmark 报配置 fingerprint 不匹配，直接使用
`generate-augmentations` 补做即可，无需重建 benchmark。方法参数、字体或代码变化会
使对应增强重新生成，目标文字或图片变化也会使旧回应缓存键失效。

`--force` 重建本次所选方法与源题的最终条目，但辅助准备、角色图和 CLIP 向量仍按各自
指纹复用。因此强制重建时没有新增辅助 API 或生图请求，不一定是故障。
详细恢复规则见 [增强文档](AUGMENTATION.md#缓存与恢复)。
