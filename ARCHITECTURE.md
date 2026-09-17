# 代码框架与数据流

## 分层结构

```text
inputs/excel                人工维护的原始输入
inputs/scenarios            可直接运行的场景数据快照
inputs/selections           固定实验抽样清单
configs                     可复制修改的运行配置
value_eval                  全部可执行源码
outputs/<run_id>            单次运行产物、报告和 checkpoint
tests                       离线单元测试与端到端测试
```

`inputs/` 不由运行过程改写。Excel 模式生成的数据发布到本次 `run_id` 下，随后由同一
流水线直接消费，不需要复制或转换文件。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `config.py` | 解析 YAML、模型别名、相对路径和环境变量名称 |
| `schemas.py` | Plan-Game、图片和回应阶段的稳定数据结构 |
| `io_utils.py` | 原子写入、JSONL、SHA256、稳定 ID |
| `clients/openai_compat.py` | 所有文本及多模态 Chat Completions 请求 |
| `value_to_scenario/excel_reader.py` | Excel 工作表、表头和必填字段校验 |
| `value_to_scenario/taxonomy.py` | 分块翻译、taxonomy 元数据抽取及合并 |
| `value_to_scenario/scenario_splitter.py` | 编号识别、稳定场景 ID 和维度索引 |
| `value_to_scenario/element_builder.py` | Stage 1 分类、Stage 2 扩展、Stage 3 审查 |
| `value_to_scenario/validator.py` | 场景文件、element、数量和 manifest 校验 |
| `generation/` | Plan-Game planner 到 single author 的题目生成 |
| `image_generation/` | 图片任务去重、请求、文件校验和恢复 |
| `response_collection/` | 构造目标请求并保存模型原始回应 |
| `augmentation/` | 筛选 HH-instruction，执行独立方法模块，保存增强样本和完整数据集清单 |
| `pipeline.py` | 跨阶段编排和总运行 manifest |
| `cli.py` | 唯一用户入口 |

## 完整时序

```text
Excel
  | 校验五个中文表头、空值和工作表
  v
翻译模型 -> 英文 taxonomy 行
  v
taxonomy 模型 -> 价值范式、风险域、评判边界
  v
按源文本编号拆分 -> rowNNN_sNN 稳定场景 ID
  v
Stage 1 分类 -> Stage 2 elements 扩展 -> Stage 3 规范化审查
  v
scenario_elements + manifest + report
  v
planner 生成题型蓝图 -> single author 生成题目
                    -> both 模式再固定图片、无蓝图生成 instruction
  v
image_description -> (可选：每机制 checkpoint 后) 图片 API / 本地 Qwen-Image -> 图片校验/manifest
  v
可选增强：HH-instruction -> FigStep 文字图片/提示词 -> dataset manifest
  v
原始及所选增强图文 -> target model -> raw response JSONL
```

benchmark 每 200 个机制形成一个恢复 shard；同 shard 的 HH/BH 并行，每个 profile 内按配置
并发。`both` 同一图片对严格按 planner、awareness author、instruction author 的顺序执行；
`awareness` 和 `instruction` 单风格模式均为各自独立的 planner -> author 流程。

`variants_per_scenario` 为正数时，输入 element 会先扩展成固定数量的场景变体 job。场景内
element 可以循环复用，但 `(scenario_id, element_id, variant_index)` 构成唯一机制键，变体
索引同时进入 benchmark ID、共享图片 ID、随机种子和 checkpoint 指纹。配额在完整 job 集合
上分配后再切 shard；值为 0 时保留旧的 element-once 数据流。

## 稳定性与恢复

场景指纹包含 Excel 内容、schema 版本、模型公开参数、prompt、关键代码和构建参数，
不包含密钥。Benchmark、图片和回应也使用内容键恢复。输入或配置变化时不会
错误复用旧结果。

`image_backend: api | local` 分别选择 `image` 和 `local_image`，两组配置共存。
本地后端通过 Diffusers 延迟加载模型，任务共享模型和推理锁，阶段结束释放资源；API 模式
不依赖 PyTorch。图片 manifest 记录所选后端的生成参数指纹，本地任务另记录实际 seed。

图片生成可通过所选后端的 `generate_during_benchmark` 流式开启。流式任务与文本生成使用独立
线程池，按 `shared_image_id` 去重，并沿用人工 images 阶段的 manifest、文件校验和恢复逻辑。
benchmark 完成后会补扫整个 benchmark 根目录，故中途失败或进程重启后仍可通过
`generate-images` 继续；参数指纹匹配且已通过尺寸、格式和 SHA256 校验的文件会直接复用。图片请求发生在
最终跨 shard 重复校验之前，后者若失败不会回滚已发生的外部图片费用。

所有 JSON checkpoint 使用同目录临时文件和原子替换。回应逐条追加 JSONL 并执行 `fsync`。
场景必须完成全量结构校验后才发布到 `scenario_preparation/scenario_elements/`。

## 数据边界

目标模型只收到图片或图片描述、问题以及 MCQ 模式下的选项。正确答案、rationale、风险
profile、taxonomy 和生成 trace 不会进入目标请求。回应阶段不解析选项，也不生成分数。

所有持久化路径使用相对路径；配置加载时可以解析绝对路径，但不会把项目所在服务器的
绝对目录写入预置数据清单。
