# 流水线运行说明

完整时序为“场景准备 → Plan-Game benchmark → 原图 → 可选增强 → 可选回应”。
`augmentation.enabled` 和方法列表控制增强，`response.enabled` 单独控制回应；
关闭回应仍会生成已启用增强。命令示例见 [README](../README.md)，增强细节见
[HH-instruction 越狱增强](AUGMENTATION.md)。

## 场景准备

1. 校验工作簿、工作表、表头和必填值。
2. 分块翻译中文 taxonomy，并校验每个 `row_id` 完整返回。
3. 提取价值范式、风险域、评判标准和安全内容边界。
4. 按编号出现顺序拆成稳定场景。
5. 对每个场景依次执行分类、elements 扩展和最终规范化审查。
6. 校验数量、ID、文件名、elements 和重复项。
7. 全量通过后发布给同一 run 的 Plan-Game 阶段。

场景间可以并发，同一场景内三个阶段不可并发。模型失败且启用 fallback 时，结果会标记
`requires_manual_review: true`，报告会给出数量。

## Plan-Game 单生成器

`both` 模式中，每个 `(scenario_id, element_id, profile)` 先由 planner 为 awareness 生成
包含模态标签、证据账本、taxonomy fit、反事实和歧义检查的蓝图，再由单个 author 生成
awareness。随后 instruction 不接收该蓝图，只接收逐字固定的 awareness
`image_description`，并独立生成问题、四个选项和完整 `risk_audit`。程序先审计 instruction
自己返回的图片描述及 audit，再将持久化图片描述无条件覆盖为 awareness 描述，这是旧 4000
共享图实现的原有顺序。Planner 不是候选作者，也不存在 reviewer 或 arbiter。

`awareness` 和 `instruction` 模式只生成所选题型，均独立执行 planner -> author；它们没有
`shared_image_id/role`。因此 instruction-only 与 `both` 中的 paired instruction 不同：前者有
自己的 instruction plan，后者为复现旧共享图片流程而不使用 plan。

每个 job 最多尝试 `item_max_attempts` 次。若 draft 已通过结构审计但视觉检查失败，旧流程会
保留 plan 和 previous draft 做局部修复；若 planner/draft 的结构化审计直接抛错，则本轮没有
可复用的完整结果，下一次会重建 plan。外层每个 200 机制分片最多执行
`shard_max_attempts` 轮，从原子 checkpoint 只补缺失 job。

旧生产的结构化契约仍为硬校验；所有由正则或关键词扫描文本得到的判断，包括可见文字
预算、选项长度泄漏、通用拒绝、placeholder、题型措辞、虚构 framing 和语义提示，均只写入
`program_lints`，不阻断生成，也不改变条目的 `structural_pass` 状态。

job 按共享图片对预分配场景类型、视觉证据模式、文字预算和 instruction family。正式
1000 机制 `both` 运行每个 profile 产生 850/100/50 个视觉模式图片对，即
1700/200/100 道题；场景类型图片对为 150/500/200/150。

配置 `variants_per_scenario: N` 后，每个场景会建立 N 个生成 job，即使 N 大于场景的 element
数也会继续稳定轮换 element。每个 job 有独立的 `variant_index`、ID、种子、plan、问题和图片；
提示要求变更人物角色、可见动作、空间布局、物体、视觉锚点和问题 framing，而不是只做同义
改写。扩增模式先对完整 job 集合分配 15/50/20/15 场景类型和 85/10/5 视觉证据配额，再切成
每 200 个 job 的 shard，避免最后一个小 shard 单独取整。`variants_per_scenario: 0` 继续使用
旧 4000 的逐 element、逐 shard 分配和 ID，保证旧配置不变。

## 图片生成模式

图片生成支持两种时序，由当前图片后端的 `generate_during_benchmark` 控制：
API 后端读取 `image`，本地后端读取 `local_image`。

- `false`（默认）：benchmark 只生成并校验题目；随后人工或流水线的 images 阶段调用
  `generate-images`。
- `true`：每个机制成功写入 shard checkpoint 后立即提交图片任务。图片任务在独立线程池中
  并发执行，benchmark 完成前会再扫描完整 benchmark 根目录，补齐遗漏和可重试的失败任务。

`both` 模式对每个机制的 awareness/instruction 使用相同 `shared_image_id`，因此只请求一张
图片；`awareness` 或 `instruction` 单风格模式没有共享配对，各自请求一张。任务写入与普通
图片阶段相同的 `images/<profile>/manifest.json`，包括状态、请求 ID、尺寸、格式和 SHA256。
图片文件通过这些字段校验后可复用，所以流式生成后仍可安全执行：

```bash
python -m value_eval generate-images --config <config.yaml> --style both
```

该命令不会重复请求已校验的图片；显式 `--force` 才会重新生成。`run-all --force` 在开启
流式图片时也不会让后续 images 阶段再次强制生成同一批图片。图片任务可能在最终跨 shard
重复检查之前已经提交，因此全局校验失败时已产生的图片请求费用无法自动撤销。

## HH-instruction 增强

1. `run-all` 在生成前检查 generation 包含 `hh` 和 `instruction`，加载各方法配置；
   独立增强命令直接检查已有源题。
2. 从 `benchmark/hh/benchmark.json` 筛选 instruction，检查 ID、问题非空及来源标签。
3. QR、CAMO、HIMRD、SI、VisCRA 从原图 manifest 解析题目对应图片并校验哈希。
   FigStep、三种 MML、CS-DJ、VisualRoleplay 不要求原 HH 图。
4. 按启用列表顺序执行方法，方法内按自身 `runtime.concurrency` 并发处理源题。
5. 需要辅助模型的方法先读取或生成准备缓存；CS-DJ 按需执行本地 CLIP 检索，
   VisualRoleplay 生成额外角色图，VisCRA 执行本地注意力定位。
6. 原子保存增强图片，每条任务结束后更新 checkpoint，按源题顺序发布 `samples.jsonl`。
7. 汇总原始集和当前启用增强集到 `dataset/manifest.json`，完整流水线只在全部就绪后
   进入回应阶段。

当前 11 种方法各为每条 HH-instruction 生成一个样本。原始集 N 条、源题 H 条、启用
M 种方法时，全部完成后共 `N + H × M` 条；原 benchmark 不因增强而改写。
方法之间不共享辅助准备，SI 和 VisCRA 可以不启用 QR 而单独执行。
每个方法结束都会调用资源释放逻辑，再开始下一个方法。

独立入口可选择方法和源题数量：

```bash
python -m value_eval generate-augmentations \
  --config configs/excel_to_benchmark_figstep_check.yaml --style both \
  --method qr --method si --max-items 1
```

未覆盖的源题使方法保持 `partial`，去掉 `--max-items` 重跑即可补齐。方法有失败条目时
会保存错误和成功结果，独立命令最终报告失败；重跑默认复用成功条目。
只补做少数方法时，其余已启用方法尚未完成可使总数据集为 `partial`，不代表所选方法失败。

## 图片与回应

图片阶段按共享图片 ID 去重，校验格式、尺寸和 SHA256 后保存。回应阶段只向目标模型发送
运行模式需要的图片或描述、问题和可选选项，不发送答案与生成 trace，随后原样保存模型的
`content`、`reasoning_content`、usage 和 request ID。

`response.datasets` 默认选择 `base` 和 `enabled_augmentations`。原始集支持四种回应
模式，增强集只支持 `image_text`；采集原题 MCQ 时须选择 `--dataset base`。
增强请求直接使用已发布样本的 `input.text` 与 `input.images`，来源答案、选项和 metadata
不进入目标消息。增强回应写到独立的 `augmentations/<method>.jsonl`，通过
`source_benchmark_id` 关联源题。选择方法只改变回应范围，不会为缺失方法自动生成增强。

采集前校验所选增强方法的全部源题覆盖、方法指纹、图片和样本文件哈希。
即使回应阶段使用 `--max-items 1`，对应方法仍必须完整就绪。

回应采集会在每条 target 请求开始和结束时实时输出状态、累计进度及 API 耗时，同时追加到
`logs/<execution_run_id>/pipeline.log`。JSONL 每条写入后执行 `fsync`；中断后用同一配置、题型
和 response mode 重跑会跳过已完成样本，继续补齐剩余请求。

## 缓存与恢复

重复执行默认复用已完成 checkpoint。输入、模型公开参数、提示词、关键代码或生成参数
变化会改变指纹。`--force` 只应在明确需要覆盖当前 `run.id` 的结果时使用；更推荐为不同
实验使用新的 `run.id`。

三种题型使用隔离的 execution run ID：`both` 使用配置中的基础 `run.id`，awareness 和
instruction 自动追加 `--awareness`/`--instruction`。分阶段运行时需始终传入同一个
`--style`，以读取对应模式的 benchmark、图片和回应 checkpoint。

场景准备的完整中间结果在 `scenario_preparation/work/<fingerprint>/`，失败后可检查请求、
缓存和报告。正式场景目录仅在全量校验成功后替换，旧版本保留在本次 run 的 archive 中。
场景准备固定调用配置中的模型；API 失败且启用 `fallback_on_llm_error` 时，才会为对应场景
写入 `heuristic_fallback` 并标记人工复核。该容错结果不是独立的离线生成模式。

增强单独按方法和源题恢复，方法目录使用独占写锁。`--force` 只重建本次选中的最终增强
条目，匹配指纹的辅助准备、角色图和 CLIP 向量缓存仍可复用。变化的目标文字或图片哈希
会使旧回应缓存键不再匹配；新回应追加写入 JSONL。

benchmark checkpoint 包含主 YAML 哈希，给已有 run 添加方法后应直接运行
`generate-augmentations`，不必重新执行 benchmark。修改方法内配置会使相关增强失效，
但不修改原 benchmark；重新生成对应增强后再采集回应。
