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

API 后端默认尝试一次保持视觉事实与风格的措辞改写，经独立校验后才提交；失败时仍标记
`moderated`，不会伪造占位图片。检查 manifest 的 `moderation_retry` 和 `effective_prompt`。
普通重跑不刷新已耗尽预算，`generate-images --force` 会重置所选任务，也会重做成功图片。
参数见[审核重试](CONFIGURATION.md#审核重试)。

## 本地生图或 GPU 失败

确认 `image_backend`、`local_image.model_path`、完整权重及可见 GPU；预检不加载模型，
不能证明显存充足。balanced 需搭配 `cpu_offload: none`；单卡卸载使用 `device_map: null`。
原生 Windows 的 fcntl 导入错误应改在 WSL/Linux 运行。`scripts/run_pipeline.sh` 内部指定 GPU 7；
需要选择其他 GPU 时，使用 `CUDA_VISIBLE_DEVICES` 配合 `python -m value_eval` 入口。

## 越狱配置、资源或状态异常

| 现象 | 处理 |
| --- | --- |
| 方法未启用或名称错误 | 核对 `augmentation.enabled` 和方法列表；`--method` 不能启用列表外方法，参数放方法自己的 config.yaml |
| 找不到源题或原图 | 确认同一 run.id 和 style、有 HH-instruction，以及所需原图的 manifest 关联和哈希；先补图 |
| 辅助连接缺失或请求耗尽 | 主与回退连接均需配置；查方法 manifest 的错误，修复后续跑 |
| MML_WR 的 NLTK LookupError | 补齐项目 assets/nltk_data 下的 averaged_perceptron_tagger_eng 数据 |
| CS-DJ / VisCRA 失败 | 核对本机权重与图库路径、完整模型文件、窗口参数和设备资源 |
| 方法 partial、回应 incomplete/stale | 去掉越狱的 max-items 限制补齐全量源题；回应只采一条也要求方法完整 |
| 越狱不支持当前回应模式 | 越狱使用 image_text；原题 MCQ 或描述模式明确选择 `--dataset base` |
| augmentation is already running | 等待持锁进程结束；正常退出后锁文件可能仍存在，不要删锁绕过互斥 |

## 配置改动后指纹不匹配，或回应数量异常

主 YAML 内容参与 benchmark 指纹，连注释改动也会影响恢复；恢复原配置或使用新 run.id。
为已有 run 生成越狱样本时直接执行 `generate-augmentations`；方法 force 只重建最终条目，
匹配的辅助准备缓存仍会复用。回应 JSONL 包含历史重试和强制重采，统计时按 sample_key
筛选当前输入与批次。总 run 显示 completed 时仍需核对图片及回应失败数，见
[验收规则](DATA_SCHEMA.md#回应历史与验收)。
