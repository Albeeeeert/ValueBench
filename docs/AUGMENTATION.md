# HH-instruction 数据增强

迁移与验证记录见 [增强方法接入验证](FIGSTEP_VALIDATION.md)，沿用已有文档和
`configs/excel_to_benchmark_figstep_check.yaml`，不为每种方法创建额外运行配置。

所有方法只处理当前 run 的 `benchmark/hh/benchmark.json` 中
`scenario_question_style: instruction` 的题目。generation 仍可同时生成四类数据。

## 配置

```yaml
augmentation:
  enabled: true
  method: [figstep, qr, camo, mml_wr, mml_mirror, mml_rotate, himrd, cs_dj, visual_roleplay, si, viscra]
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

## 已接入的方法

每种方法有独立的 `methods/<method>/config.yaml`、实现和提示词；公用字体放在
`augmentation/assets/fonts/`。三种 MML 分开启用、输出和采集回应。

| 方法 | 图片处理 | 额外依赖 |
| --- | --- | --- |
| figstep | 760×760 问题与编号排版 | 无 |
| qr | 原 HH 图底部添加关键词条 | 辅助 API 抽词和改写 |
| camo | 原 HH 图适配场景区，添加字符索引线索区 | 辅助 API 抽取原文关键词 |
| mml_wr | 名词/形容词一一替换，保存可逆字典，排版图 | NLTK 英文词性标注 |
| mml_mirror | 排版图水平镜像 | 无 |
| mml_rotate | 排版图旋转 180° | 无 |
| himrd | 原 HH 图顶部添加抽取文字，文本用单个占位符 | 辅助 API 抽取连续片段 |
| cs_dj | 9 张 CLIP 检索干扰图＋3 张分解问题图 | 辅助 API、CLIP、本地干扰图库 |
| visual_roleplay | ROLE 文字＋新角色图＋REQUEST 文字 | 辅助 API、当前配置的生图后端 |
| si | 打乱原 HH 图块和问题词序，添加底部关键词条 | 独立辅助 API 抽词 |
| viscra | 用注意力定位原 HH 图遮挡区，添加底部关键词条 | 独立辅助 API 抽词、本地 Qwen2.5-VL |

QR、HIMRD、SI、VisCRA 保留原图宽高，在上下增加文字条；字体、间距和留白按实际图宽
相对 `reference_width` 缩放，文字按像素换行，面板高度按完整文字展开。CAMO 沿用原方法
的场景区与线索区布局，以实际原图尺寸替代固定 512，并同比缩放线索字号；线索过多可增高。
因此 512 和 2048 输入不需要修改方法配置。SI 要求宽高可被 `blocks_per_side` 整除。

SI 和 VisCRA 在自己的 `preparation/` 中完成抽词，不要求启用或先运行 QR。
VisCRA 对关键词计算本地注意力，再将遮挡框映射回原图分辨率；`max_pixels` 只限制注意力
模型输入，不降低导出的原图分辨率。没有注意力结果会记录失败，不用随机框代替。
CLIP 与注意力模型是原方法的本地算法组件，不属于辅助文本生成 API。

VisualRoleplay 是唯一额外调用生图模型的方法，直接复用 `image_backend` 以及 `image` /
`local_image` 的客户端、尺寸和生成参数。角色图按实际输出宽度添加上下文字面板；中间角色图
和角色规划分别缓存，渲染或生图失败重跑可复用已完成阶段。

辅助模型配置位于各自方法目录的 `auxiliary` 块：默认 Qwen3.5-35B-A3B，温度 1.0，
每个模型总共最多尝试 3 次（首次＋2 次重试，`auxiliary.retries: 2`）。Qwen 失败 3 次后
切到 DeepSeek-V4-Flash，回退模型也总共最多尝试 3 次。
7 种使用辅助模型的方法均将 `max_tokens` 设为 32768（原为 8192），主模型和回退模型共用该值，
以降低长度截断的概率；本次仅扩大输出预算，未增加 `finish_reason` 截断拦截。
请求失败、空回复、编号列表/分块/JSON 等内容结构校验失败均进入该策略。连接优先匹配主配置中的同名模型，
否则匹配相同 `api_key_env` 的现有模型，继承 URL 与密钥环境变量；不复制旧项目中的密钥。
基础数据集模型的低温度参数保持原值。各样本 metadata 记录实际使用的辅助模型和尝试次数。
7 种使用辅助模型的方法均设置 `auxiliary.enable_thinking: false`，省略该配置时也默认关闭：
Qwen 使用 `enable_thinking: false`，DeepSeek 回退使用 `thinking: {type: disabled}`，
均通过非流式接口获取结果。思考设置变化会使旧增强缓存失效，需重新执行增强生成。

安装统一依赖：`pip install -r requirements.txt`。MML_WR 还需要已有的
`averaged_perceptron_tagger_eng` NLTK 数据；如未安装，运行
`python -m nltk.downloader averaged_perceptron_tagger_eng`。生成和预检不会自动下载模型或词典。
CS-DJ 的 `src_dir`、`clip_path` 和 VisCRA 的 `attention_model_path` 均在各方法配置中设置。
本地模型按需加载，方法完成后释放；GPU 编号遵循 `CUDA_VISIBLE_DEVICES`。

`requirements.txt` 是唯一依赖清单，`pyproject.toml` 从它生成安装依赖。
原 `augmentation`、`local-image` extras 已移除。

## 与迁移前实现的差异

这里比较的是 `/SSD3/hanzhouyu/VLMEvaluation/jailbreak` 的代码及对应 `value_*_config.yaml`，
不是各论文官方仓库。迁移保留主要方法流程，尚未证明与旧实现输出逐像素或模型输入逐元素等价。

所有使用辅助文本模型的方法都接入了统一重试/回退，并采用温度 1.0。当前 Qwen 主模型和
DeepSeek 回退均关闭 thinking。旧实现通常显式设置 top_p，当前没有
显式传入该参数。这些属于生成设置变化，不仅是接口整理。
随机流程也需注意：CAMO、SI、MML 镜像/旋转的随机种子派生方式与旧实现
不同，具体线索顺序、图块排列或乱序词表不保证与旧样本相同。

辅助请求的初始 system 提示和 user 格式已按原版核对：CS-DJ 恢复完整示例与
`[Question]` / `[Sub-questions]`，按原版编号文本解析，并严格要求完整三条；
HIMRD 恢复原版四段输出及 `[Request]` 输入，保留拆分还原校验，并额外核对与源题一致；
CAMO 恢复 `[Request]` / `[/Request]` 包装，VisualRoleplay 恢复 `{"request": ...}`。
QR、SI、VisCRA 的抽词 system 提示以及其他方法的目标提示模板已与原源码比对。
CAMO 的目标提示现已修正原版编号歧义：按算式顺序取得线索 ID，查图中的 `ID:character`，
再按空缺从左到右填入字符；线索 ID 不表示空缺位置，随机编码算法保持不变。
失败后的反馈仍使用统一辅助客户端的重试机制。提示模板一致不代表渲染、随机策略、
注意力输入或采样结果与旧实现完全相同。

| 方法 | 保留与变化 |
| --- | --- |
| FigStep | 保留问题与空编号；旧实验固定字号 18、70 字符换行，当前按像素换行并在 10–18 中适配字号。 |
| QR | 保留原图＋抽词/改写＋底部文字条；文字条从固定 512 宽改为匹配源图，输出从 JPEG 改为 PNG，JSON 额外严格要求 phrase_type。只迁移复用原图路径。 |
| CAMO | 原关键词跨度校验、前缀遮字、算术线索算法保留；画布与字号跟随原图，随机排列与旧结果不同。只迁移复用原图路径。 |
| MML 三种 | 始终按各自配置生成 760×760，不跟随原图；WR 一一替换核心函数保持原样，镜像和 180° 旋转保留。共同改为像素换行、10–18 字号适配及统一边距，旧实验为 18 号、70 字符换行。 |
| HIMRD | 已恢复原版完整拆分提示、四段输出和目标提示；保留模型选择的占位符位置，不再本地固定替换首次出现。按原版归一化规则校验还原，并额外核对源题；图像提示仅存入 metadata，继续复用原图。顶部文字条的字号、边距和对齐方式仍有调整。 |
| CS-DJ | 保留 CLIP 干扰图检索和 9＋3 拼图；已恢复原版分解提示、示例、编号输出格式和目标提示，严格要求三条子问题，失败时继续重试/回退而不使用单条原题。检索候选先排序并禁止重复选择；文字图由原先 900 宽、50 号生成后缩放，改为 500 宽、32–12 号适配，标签/留白布局及保存格式也不同。 |
| visual_roleplay | 保留角色规划提示、校验和 ROLE/角色图/REQUEST 结构；原角色图为 1024×768，当前继承基础生图尺寸、步数、负向提示和 seed，不再使用原角色专用生图参数及按样本计算的 seed。文字面板改为随宽度缩放并展开高度，原标题字号和九行缩字号策略未沿用。 |
| SI | 保留原图分块与问题词序打乱；原读取/补做 QR 产物，当前独立抽词并重画文字条，随机排列不同。 |
| VisCRA | 保留本地 Qwen2.5-VL、第 18 层、最后文本 token 对图像 token 的平均注意力和最高分窗口遮挡；独立抽词，关键词查询注意力，最终请求使用 rephrased_question。按源图尺寸计算注意力，只做模型 patch 对齐，首末行/列各置零。仅迁移 attention/top1 路径，未迁移 baseline、visual_cot、多候选随机选择、热力图与叠加图导出。 |

CS-DJ 的图库快照使用有序的路径与内容哈希列表，CLIP 向量缓存指纹包含该顺序。
同一组图片因 seed 变化而重新排列时会重新计算向量，避免旧向量与当前图片路径错配；
旧字典格式对应的向量缓存也会失效。

VisCRA 的“静态图片接口适配”指：旧路径调用 `AutoProcessor` 与 `qwen_vl_utils`；当前环境
的 AutoProcessor 会额外初始化视频处理器并因缺少 torchvision 失败。现在分别加载
`Qwen2VLImageProcessor`、`AutoTokenizer`，按实际图像网格计算 image token 数后送入原模型。
这条路径已通过实际推理，但尚未与旧处理路径做输入张量和注意力数值的逐元素对比。

VisCRA 已移除固定 `max_pixels: 262144` 限制。每张图的预处理上限根据其实际尺寸计算，
仅允许 Qwen 的 28 像素 patch 对齐，并检查实际网格；不因显存开销降低分辨率。
512×512 对齐到 504×504，形成 18×18 网格；2048×2048 对齐到 2044×2044，
形成 73×73 网格。`attention_block_size_by_resolution` 按源图尺寸配置窗口：
512×512 使用 5×5 格，映射回原图约为 142×142；2048×2048 使用 24×24 格，
映射回原图约为 673×673（坐标取整后边长为 673 或 674）。其他尺寸使用 `attention_block_size`（默认 5），
也可在尺寸映射中显式增加条目。非正方形图像按宽、高分别对齐和映射。

`zero_side_columns: true`、`zero_top_bottom_rows: 1` 在所有尺寸下都将首末列和首末行
置零后选择窗口，沿用原实现的过滤方式；不裁剪网格，避免改变坐标映射。
视觉编码器使用 SDPA，文本解码器保持 eager。所有文本层正常执行前向；仅在第 18 层
通过 hook 读取最后一个文本 token 对图像 token 的平均注意力并复制到 CPU，
不累计保存各层完整注意力矩阵，也不保留本次前向的 KV cache。
同一混合模型的 512 实测中，按需提取与完整返回后读取所需注意力逐元素一致。
视觉 SDPA 与全 eager 的浮点计算路径不同，实测注意力数值和遮挡框均可能改变，
因此这条路径不保证与全 eager 生成完全相同的增强图片。metadata 记录
`vision_attention_backend: sdpa`、`text_attention_backend: eager`、`attention_capture: selected_layer`。
加载方式也沿用原版：多张可见 GPU 使用 `device_map="balanced"`，每卡权重分配预算 8 GiB；
单张可见 GPU 使用 `device_map="auto"`。这里的 8 GiB 是模型分配预算，不是运行峰值。
通过 `CUDA_VISIBLE_DEVICES` 限定实际参与的卡，不要求固定五张卡。
此前混合后端与按需提取在双 A100 40GB 上通过 512 和 2048 实测，2048 的两卡峰值
已分配显存为 14.51 / 15.54 GiB，保留 73×73 网格和 12×12 窗口。
只切换视觉 SDPA、仍返回全部文本层注意力的方案，在双卡 2048 下仍会 OOM；
全 eager 的双卡 2048 则在视觉编码器中 OOM。按层分配不会拆分单个注意力算子的分配。
补测全 eager＋按需提取：512 通过且所需注意力与完整返回一致；2048 仍在视觉编码器
分配 27.08 GiB 临时矩阵时 OOM。按需提取减少累计保存，无法消除 eager 单层计算峰值。
当前 SDPA 接口不返回视觉编码器的注意力权重；用于 VisCRA 遮挡的二维热图来自文本
第 18 层对图像 token 的注意力，这部分保持 eager 并按需提取。
这些结果来自单张合成中性图片，不代表全数据集的显存上限或效果评测。
详见原验证文档中的双卡实测及后端数值对照记录。

原 VisCRA 支持 `input_prompt_source` 和 `attention_prompt_source`：可用原题或 QR 改写题
构造最终请求，可用输入题或 QR 底部关键词查询注意力。当前迁移使用独立准备得到的
`rephrased_question` 构造最终请求，以 `Which regions in the image show {key_phrase}? Answer:` 查询注意力。
四边过滤沿用旧 `generate_plan_game_dataset.py` 专用入口，不依赖 QR 产物。
当前旧默认配置本身就是 `attention_stride: 1` 和 `top1_attention`，迁移保留这一默认路径；
未迁移的多候选随机、baseline、visual_cot 属于其他可选路径。

各方法的长提示词已整理为直接换行的多行字符串，去掉源码中密集的 `\n` 写法并清理
行尾空白，保留原版文字和段落。运行时原本就没有多余的字面量反斜杠加 n。

## 命令

完整 Excel 验证配置保留四类生成，每个 profile 最多 4 条；8 条原题中的 2 条 HH-instruction
分别经过 11 种增强后，完整集为 30 条。`response.enabled: false` 只跳过回应，不跳过增强。

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
