# 流水线运行说明

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

图片生成支持两种时序，由 `image.generate_during_benchmark` 控制：

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

## 图片与回应

图片阶段按共享图片 ID 去重，校验格式、尺寸和 SHA256 后保存。回应阶段只向目标模型发送
运行模式需要的图片或描述、问题和可选选项，不发送答案与生成 trace，随后原样保存模型的
`content`、`reasoning_content`、usage 和 request ID。

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
