# 流水线运行说明

完整流程为“场景准备 → benchmark → 图片 → 可选越狱 → 可选回应”。
`augmentation.enabled` 控制越狱，`response.enabled` 单独控制 run-all 的回应阶段。

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
自己返回的图片描述及 audit，再将持久化图片描述统一为 awareness 描述。
Planner 负责规划，author 负责编写题目。

`awareness` 和 `instruction` 模式只生成所选题型，均独立执行 planner -> author；它们没有
有效的共享图片关联。单 instruction 使用独立 plan；双题型中的 instruction 复用 awareness
的图片描述构题。

每个 job 最多尝试 `item_max_attempts` 次。若 draft 已通过结构审计但视觉检查失败，流程会
保留 plan 和 previous draft 做局部修复；若 planner/draft 的结构化审计直接抛错，则本轮没有
可复用的完整结果，下一次会重建 plan。外层每个 200 机制分片最多执行
`shard_max_attempts` 轮，从原子 checkpoint 只补缺失 job。

结构化契约使用硬校验；正则或关键词扫描文本得到的判断，包括可见文字
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
逐 element、逐 shard 分配和稳定 ID。

## 图片生成模式

图片生成支持两种时序，API 后端由 `image.generate_during_benchmark` 控制，
本地后端由 `local_image.generate_during_benchmark` 控制：

- `false`（默认）：benchmark 只生成并校验题目；随后人工或流水线的 images 阶段调用
  `generate-images`。
- `true`：每个机制成功写入 shard checkpoint 后立即提交图片任务。图片任务在独立线程池中
  并发执行，benchmark 完成前会再扫描完整 benchmark 根目录，补齐遗漏和可重试的失败任务。

`both` 模式对每个机制的 awareness/instruction 使用相同 `shared_image_id`，因此只请求一张
图片；`awareness` 或 `instruction` 单风格模式没有共享配对，各自请求一张。任务写入与普通
图片阶段相同的 `images/<profile>/manifest.json`，包括状态、请求 ID、尺寸、格式和 SHA256。
图片文件通过这些字段校验后可复用。使用生成题目时的运行配置补齐图片：

```bash
CONFIG=configs/my_run.yaml
python -m value_eval generate-images --config "$CONFIG" --style both
```

该命令不会重复请求已校验的图片；显式 `--force` 才会重新生成。`run-all --force` 在开启
流式图片时也不会让后续 images 阶段再次强制生成同一批图片。图片任务可能在最终跨 shard
重复检查之前已经提交，因此全局校验失败时已产生的图片请求费用无法自动撤销。

## HH-instruction 越狱

独立命令读取已有 HH-instruction 源题；run-all 还要求生成范围包含 `hh` 和 `instruction`。
方法按启用顺序执行，各自负责转换，执行器统一校验图片、保存源题 checkpoint 并发布
`samples.jsonl`；需要原图的方法同时校验其 manifest 关联和哈希。辅助文本、角色图和
CLIP 向量按需缓存，方法结束后释放资源。操作见 [README](../README.md#第四步可选hh-instruction-越狱)。

只选择部分源题时方法为 `partial`，无失败且全量覆盖后为 `completed`；数据集索引只汇总
原题及当前启用的方法。所选越狱样本集完整就绪后才能采集回应，回应的条数限制不跳过这项校验。
目标请求仅使用越狱样本的 `input.text` 与 `input.images`，源题选项、答案和审计不进入请求。

## 图片与回应

图片阶段按共享图片 ID 去重，校验格式、尺寸和 SHA256 后保存。回应阶段只向目标模型发送
运行模式需要的图片或描述、问题和可选选项，不发送答案与生成 trace，随后原样保存模型的
`content`、`reasoning_content`、usage 和 request ID。
`response.datasets` 选择原题、全部已启用方法的越狱样本或指定方法的样本，越狱样本只支持 `image_text`。
描述模式虽不读取图片，run-all 仍执行生图；需要省去生图时使用独立 benchmark 和回应命令。

回应采集会在每条 target 请求开始和结束时实时输出状态、累计进度及 API 耗时，同时追加到
`logs/<execution_run_id>/pipeline.log`。JSONL 每条写入后执行 `fsync`；中断后用同一配置、题型
和 response mode 重跑会跳过已完成样本，继续补齐剩余请求。

## 缓存与恢复

重复执行默认复用已完成 checkpoint。输入、模型公开参数、提示词、关键代码或生成参数
变化会改变指纹。恢复中断任务时执行相同命令；输入或生成范围改变时使用新的 `run.id`。
`--force` 用于重新执行所选阶段，具体范围如下：

| 操作 | 执行方式 |
| --- | --- |
| 为已有题目生成越狱样本 | 在原运行配置中选择方法，执行 `generate-augmentations` |
| 更新已有题目的图片 | 执行 `generate-images`，匹配的有效图片自动复用 |
| 强制生成图片 | `generate-images --force` 重新生成所选图片任务 |
| 强制生成越狱样本 | `generate-augmentations --force` 重建最终条目，准备缓存按各自指纹复用 |
| 重新采集回应 | `collect-responses --force` 重新请求并追加记录 |

主 YAML 的内容参与 benchmark 指纹，生成题目后保留该配置版本。
启用越狱方法或调整图片、回应设置后，使用对应的独立阶段命令处理已有题目。

三种题型使用隔离的 execution run ID：`both` 使用配置中的基础 `run.id`，awareness 和
instruction 自动追加 `--awareness`/`--instruction`。分阶段运行时需始终传入同一个
`--style`，以读取对应模式的 benchmark、图片和回应 checkpoint。

场景准备的完整中间结果在 `scenario_preparation/work/<fingerprint>/`，失败后可检查请求、
缓存和报告。正式场景目录仅在全量校验成功后替换，旧版本保留在本次 run 的 archive 中。
场景准备固定调用配置中的模型；API 失败且启用 `fallback_on_llm_error` 时，才会为对应场景
写入 `heuristic_fallback` 并标记人工复核。该容错结果不是独立的离线生成模式。

已发布场景的指纹、数量与文件哈希匹配时，普通重跑直接复用原 manifest，保持下游指纹稳定。
越狱阶段使用方法写锁和源题级恢复。回应缓存键包含模型名及实际输入；更换同名模型的温度或
服务地址并要求重新采集时，使用 `collect-responses --force` 并记录批次。
统计与验收见 [数据契约](DATA_SCHEMA.md#回应历史与验收)。
