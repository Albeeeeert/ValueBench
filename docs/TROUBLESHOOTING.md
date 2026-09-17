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
