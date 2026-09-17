# 输入目录

该目录只存放用户输入和可复用数据快照，运行过程不会在这里写 checkpoint。

| 目录 | 内容 |
| --- | --- |
| `excel/` | 原始价值观工作簿、空模板和小样例 |
| `scenarios/examples/` | 两个场景的快速验证数据 |
| `scenarios/canonical_473/` | 473 个场景、3458 个 mechanism elements 的快照 |
| `selections/` | 固定实验使用的 `(scenario_id, element_id)` 集合 |

场景 JSON 和 manifest 结构见 [../docs/DATA_SCHEMA.md](../docs/DATA_SCHEMA.md)。
