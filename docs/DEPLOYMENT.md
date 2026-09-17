# 独立部署清单

## 复制后检查

1. 只复制整个 `Value_eval/` 目录，不需要上级仓库。
2. 确认服务器 Python 版本不低于 3.10。
3. 创建虚拟环境并安装 `requirements.txt`。
4. 从 `.env.example` 创建 `.env`，只填写实际使用的 API Key。
5. 复制并修改一份 YAML，设置唯一 `run.id`、模型名称和 endpoint。
6. 将正式表格放入 `inputs/excel/`，或配置本地 prepared 场景目录。
7. 先运行输入验证和 `--dry-run`，再运行小样本，最后放开条数限制。

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

## 隔离验收

在临时目录中复制项目，进入复制后的目录运行测试和输入检查。以下命令不应访问原仓库：

```bash
python -m unittest discover -s tests -v
python -m value_eval validate-input --config configs/prepared_scenarios.yaml
python -m value_eval validate-excel --config configs/excel_to_benchmark.yaml
```

运行前可搜索遗留绝对路径：

```bash
rg '/home/|/home1/|valuebench|vlm_value' . \
  --glob '!outputs/**' --glob '!logs/**' --glob '!*.pyc'
```

预期源码、配置和预置 manifest 中无匹配项。
