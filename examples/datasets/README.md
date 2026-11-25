# 数据集转换范例

本目录的脚本演示如何把公开数据整理为平台格式的数据集（描述文件 `datapackage.yaml` + 数据文件），
不属于平台核心。数据规范见《平台设计说明书》"数据规范"一章，操作步骤见《使用手册》"数据准备"一章。

- 缺省把原始文件下载到 `${CPC_DATA_DIR}/raw/<来源>/`，转换结果写到 `${CPC_DATA_DIR}/<数据集名>/`
  （`CPC_DATA_DIR` 未设置时为 `~/.cache/cpc/datasets`）；`--raw` 指定已有的原始文件，`--out` 指定输出目录。
- 转换中的取舍以命令行参数显式给出（如输出 token 数为 0 的请求是报错还是剔除），并记录在描述文件的 `cpc.conversion` 中。
- 写出与校验使用 `cpc.datasets.build.write_dataset`；结果可用 `cpc data check <目录>` 再次检查。

| 脚本 | 类别 | 原始数据 | 许可 |
|---|---|---|---|
| `azure_llm.py` | workload | [Azure LLM Inference Trace 2023 / 2024](https://github.com/Azure/AzurePublicDataset)（`AzureLLMInferenceTrace_{conv,code}.csv`、`..._1week.csv`） | CC-BY-4.0 |
| `burstgpt.py` | workload | [BurstGPT v2.0](https://github.com/HPMLL/BurstGPT)（`BurstGPT_1.csv`） | CC-BY-4.0 |
| `mooncake.py` | workload | [Mooncake FAST'25 traces](https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release)（`{conversation,toolagent,synthetic}_trace.jsonl`） | Apache-2.0 |
| `synthetic_workload.py` | workload | 合成（非齐次泊松到达），只用于测试与演示 | — |
| `cn_grid/convert.py` | grid_signal | 本目录 `cn_grid/`：各省分时电价规则（`tou_tariffs.yaml`，出处见各条目）与 2023 年省级电力平均二氧化碳排放因子（`carbon_factors_2023.csv`，生态环境部、国家统计局公告 2025 年第 47 号） | 政府公开信息 |
| `eia930.py` | grid_signal | [EIA-930 Hourly Electric Grid Monitor](https://www.eia.gov/electricity/gridmonitor/) 六个月文件（2024 年 1–6 月） | public domain |
| `constant_signal.py` | grid_signal | 由参数给定的恒定信号（非实测） | — |
| `mlenergy.py` | power_profile | [ML.ENERGY Benchmark v3](https://huggingface.co/datasets/ml-energy/benchmark-v3)（`runs/llm.parquet`，受控访问，需 `HF_TOKEN`） | Apache-2.0 |
