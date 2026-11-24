#import "../template/cpc-doc.typ": *

= 数据准备 <g-data>

平台的输入数据集是包含描述文件（`datapackage.yaml`）与数据文件的目录，格式见《平台设计说明书》"数据规范"一章。
原始数据须先转换为平台格式；`examples/datasets/` 提供若干公开数据集的转换脚本。

== 数据目录

场景文件中的数据集路径通常写为 `${CPC_DATA_DIR}/<数据集名>`。环境变量 `CPC_DATA_DIR` 未设置时取 `~/.cache/cpc/datasets`。
转换脚本缺省把原始文件下载到 `${CPC_DATA_DIR}/raw/<来源>/`，把转换结果写到 `${CPC_DATA_DIR}/<数据集名>/`；
`--raw` 指定已有的原始文件，`--out` 指定输出目录。原始数据与转换结果均不提交到代码仓库。

== 转换公开数据集

默认场景（`configs/scenario.yaml`）需要以下两个数据集：

```bash
# 负载（原始文件约 1.1 GB）
python examples/datasets/azure_llm.py --variant 2024-conv --zero-output fail
# 电价与碳强度
python examples/datasets/cn_grid/convert.py --start 2026-01-01 --end 2027-01-01
```

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("命令", "输出数据集"),
    [`azure_llm.py --variant {2023,2024}-{conv,code} --zero-output fail`], [`azure-llm-<variant>`：Azure LLM Inference Trace（2023 版 1 小时，2024 版 1 周）],
    [`burstgpt.py --zero-output drop`], [`burstgpt-1`：BurstGPT 前 2 个月的请求，扩展字段 `model`、`log_type`；剔除响应 token 数为 0 的失败请求],
    [`mooncake.py --trace {conversation,toolagent,synthetic} --zero-output fail`], [`mooncake-fast25-<trace>`：Mooncake trace，扩展字段 `prefix_hashes`],
    [`cn_grid/convert.py --start 日期 --end 日期 [--provinces ...]`], [`cn-grid-<年>`：广东、贵州、内蒙古（蒙西）、上海的逐时段分时电价与省级年均碳强度],
    [`eia930.py --series {adjusted,reported} [--regions ...] [--factors JSON]`], [`eia930-carbon-2024h1`：美国各平衡区 2024 年 1–6 月逐时碳强度],
    [`constant_signal.py --name 名称 --signal 信号 --unit 单位 --values 区域=值 ... --start --end`], [由参数给定的恒定信号（非实测）],
    [`mlenergy.py`], [`mlenergy-benchmark-v3-llm`：ML.ENERGY Benchmark v3 的 LLM 测量（需 `HF_TOKEN`）],
    [`synthetic_workload.py ...`], [合成负载（只用于测试与演示）],
  ),
  caption: [转换范例（`examples/datasets/`）],
)

`--zero-output` 等参数表示转换中的取舍须显式给出：例如平台规范要求输出 token 数 ≥ 1，原始数据中输出为 0 的请求须选择报错或剔除，
剔除的数量记录在描述文件的 `cpc.conversion` 中。ML.ENERGY Benchmark 在 Hugging Face 上为受控访问，
须先在其页面申请访问，再以环境变量 `HF_TOKEN` 提供访问令牌。美国平衡区场景（`configs/scenario.us-eia.yaml`）的数据准备命令见该文件开头的注释。

== 校验与查看

```bash
# 各类数据集的核心字段与约定扩展字段
cpc data kinds
# 完整校验：描述文件、类别规范、字段类型与约束
cpc data validate ${CPC_DATA_DIR}/cn-grid-2026
# 描述文件摘要：出处、许可、资源与字段
cpc data info ${CPC_DATA_DIR}/azure-llm-2024-conv
# 负载：请求数、时长、平均到达率、token 均值
cpc data summary ${CPC_DATA_DIR}/azure-llm-2024-conv
# 电网信号：信号、单位、区域、覆盖范围
cpc data summary ${CPC_DATA_DIR}/cn-grid-2026
```

数据集不符合规范、文件缺失或时间窗超出数据覆盖范围时，`cpc sim`、`cpc grid`、`cpc loadgen` 等命令报错并给出原因，不改用其他数据。

描述文件中资源的 `path` 可以是 URL，`cpc data fetch <数据集目录>` 把这些资源下载到描述文件所在目录；资源声明
`cpc_auth: hf` 时以 `HF_TOKEN` 认证。

== 整理自己的数据

转换脚本的任务是把原始数据整理为符合类别规范的表，并写出描述文件。可以直接使用平台提供的写出函数：

```python
import pyarrow as pa
from cpc.datasets.build import write_dataset

table = pa.table({
    "arrival_s": [0.0, 0.8, 1.5],
    "input_tokens": [812, 95, 2048],
    "output_tokens": [210, 64, 512],
    "tenant": ["a", "b", "a"],                    # 扩展字段
})
fields = [
    {"name": "arrival_s", "type": "number", "unit": "s",
     "constraints": {"required": True, "minimum": 0}},
    {"name": "input_tokens", "type": "integer",
     "constraints": {"required": True, "minimum": 0}},
    {"name": "output_tokens", "type": "integer",
     "constraints": {"required": True, "minimum": 1}},
    {"name": "tenant", "type": "string", "description": "业务标识"},
]
write_dataset(
    "/data/my-trace", name="my-trace", version="2026.11", kind="workload",
    table=table, fields=fields,
    licenses=[{"name": "internal"}], sources=[{"title": "服务日志导出"}],
    cpc={"origin": "2026-11-01T00:00:00+08:00",
         "conversion": {"script": "convert_my_trace.py"}},
)
```

`write_dataset` 写出数据文件（缺省 Parquet）与 `datapackage.yaml`，并按类别规范完整校验一次。也可以手工编写描述文件，
数据文件为 CSV 或 Parquet 均可。

== 扩展字段与请求属性

负载数据集的扩展字段经场景选择后成为请求属性，策略通过 `req.attrs` 读取：

```yaml
workload:
  dataset: /data/my-trace
  attributes: [tenant]       # 须在描述文件中声明
```

仿真中属性直接随请求传给路由策略与站内派发策略；实验中负载生成器以请求头 `x-cpc-attr-tenant` 发送，网关转发给站点代理。
电网信号数据集中的其他信号（如可再生能源占比）通过 `grid.extras` 声明后出现在 `GridSignal.extra` 中。

== 标定服务器参数

```bash
cpc data calibrate ${CPC_DATA_DIR}/mlenergy-benchmark-v3-llm --slots 128 \
    --where device=H100 device_count=8 task=lm-arena-chat \
            model=meta-llama/Llama-3.1-405B-Instruct-FP8
```

`--where` 选择同一组测量（字段须在描述文件中声明）。输出可写入场景 `node_profiles` 的参数（`gpus`、`gpu_model`、`slots`、
`gpu_active_min_w`、`gpu_max_w`、`power_exponent`、`decode_s_per_token`、`decode_slowdown`）、拟合残差与测量的占用率范围；
加 `--curves` 另给出查表式曲线 `gpu_power_curve`、`tpot_curve`。空载功率、主机功率、预填充吞吐与开机时间不在测量中，须另行给定。
真实服务器的测量整理为 power_profile 数据集后可用同样的方式标定（@g-calibrate）。
