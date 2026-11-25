#import "../template/cpc-doc.typ": *

= 数据规范 <ch-data>

平台的输入数据分为三类：推理请求负载（workload）、电网信号（grid_signal）与功率特性（power_profile）。
每个数据集是一个目录，包含描述文件与数据文件。平台规定每类数据完成基本调度所必需的核心字段，
其余信息以扩展字段承载；平台按描述文件与类别规范校验数据后加载，不包含任何特定原始数据集的解析代码。
原始数据到平台格式的转换由使用者完成，`examples/datasets/` 提供若干公开数据集的转换范例（@fig-data）。

#figure(image("../figures/data-workflow.pdf", width: 100%), caption: [数据集的组织、转换与使用]) <fig-data>

== 描述文件

描述文件采用 Frictionless Data Package v2 @frictionless2024datapackage 的子集：数据包描述（名称、版本、许可、来源）、
数据资源（路径、格式、字节数、哈希）与表结构（Table Schema：字段名、类型、约束、缺失值）。
该标准允许在描述文件、资源与字段中加入自定义属性；平台的扩展属性为顶层的 `cpc` 与字段的 `unit`。

```yaml
name: azure-llm-2024-conv            # 数据集名称
version: dataset-llm-2024            # 版本（如原始数据的发布标签或提交）
title: Azure LLM Inference Trace 2024（对话服务，1 周）
licenses: [{name: CC-BY-4.0}]
sources:
  - {title: Azure Public Dataset, path: https://github.com/Azure/AzurePublicDataset}
cpc:
  kind: workload                     # 类别：workload / grid_signal / power_profile
  profile: 1                         # 类别规范的版本
  origin: "2024-05-12T00:00:00+00:00"  # 类别相关属性：负载的时间零点
  citation: "..."
  conversion: {script: examples/datasets/azure_llm.py, zero_output: fail}
resources:
  - name: requests                   # 资源名由类别规定
    type: table
    path: requests.parquet           # 相对于描述文件的路径
    format: parquet                  # csv / parquet
    hash: "sha256:..."               # 出处信息（可选）
    schema:
      fields:
        - {name: arrival_s, type: number, unit: s,
           constraints: {required: true, minimum: 0}}
        - {name: input_tokens, type: integer, constraints: {required: true, minimum: 0}}
        - {name: output_tokens, type: integer, constraints: {required: true, minimum: 1}}
```

平台读取的属性如下；其余属性原样保留，平台不解释。

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("属性", "说明"),
    [`name`、`version`], [数据集的标识，运行结果以 `name@version` 记录所用数据集。],
    [`licenses`、`sources`、`title`、`description`], [许可与来源，供查阅与结果出处记录。],
    [`cpc.kind`、`cpc.profile`], [数据集类别与类别规范版本（当前为 1）。],
    [`resources[].path`], [相对于描述文件所在目录的路径。],
    [`resources[].format`], [`csv` 或 `parquet`。],
    [`resources[].hash`、`bytes`], [出处信息，采用"算法前缀"写法（如 `sha256:<十六进制摘要>`）。],
    [`schema.fields[]`], [字段名、类型（`string`、`number`、`integer`、`boolean`、`datetime`、`date`、`list` 等）、约束（`required`、`unique`、`minimum`、`maximum`、`exclusiveMinimum`、`exclusiveMaximum`、`enum`）、单位 `unit`；`list` 类型在 CSV 中以 `delimiter` 分隔，元素类型由 `itemType` 给出。],
    [`schema.missingValues`], [CSV 中表示缺失值的字符串（缺省为空串）。],
  ),
  caption: [平台读取的描述文件属性],
)

数据集路径在场景文件中给出，可以是相对于场景文件的路径，也可以使用环境变量，如 `${CPC_DATA_DIR}/azure-llm-2024-conv`。
`CPC_DATA_DIR` 未设置时取 `~/.cache/cpc/datasets`；其他未定义的环境变量使加载失败。

== 类别规范

每类数据集规定一个表格资源与其字段：*核心字段*是平台完成基本调度所必需的字段，描述文件中须声明、类型与单位须相容、
数据中不得为空；*约定扩展字段*有统一的名称与类型，参考策略可能使用；其余字段为*任意扩展字段*，平台原样保留，
并可按场景配置传给策略。`cpc data kinds` 打印全部类别的字段定义。

=== 负载（workload）

资源 `requests`，每行一个推理请求，按 `arrival_s` 非降序排列。

#figure(
  tbl(
    columns: (9.5em, 6.5em, 1fr),
    header: ("字段", "类型", "说明"),
    [`arrival_s`（核心）], [number，s], [到达时间：相对数据集时间零点的秒数，≥ 0],
    [`input_tokens`（核心）], [integer], [输入 token 数，≥ 0],
    [`output_tokens`（核心）], [integer], [输出 token 数，≥ 1],
    [`request_id`], [string / integer], [请求标识],
    [`model`], [string], [请求的模型名],
    [`session_id`], [string / integer], [会话标识（多轮对话）],
    [`priority`], [integer], [优先级，数值越大越优先],
    [`slo_class`], [string], [服务等级类别],
    [`tenant`], [string], [租户或业务标识],
    [`prefix_hashes`], [list], [前缀块哈希序列，用于 KV 缓存复用分析],
  ),
  caption: [负载数据集的字段],
)

描述文件属性 `cpc.origin`（带时区的时间）给出 `arrival_s` 的零点，可选；原始数据未给出绝对时间时省略。
场景通过 `workload.attributes` 选择需要传给策略的扩展字段，所选字段作为请求属性（`RequestInfo.attrs`）在仿真中直接可见，
在实验中由负载生成器以请求头 `x-cpc-attr-<字段名>`（JSON 值）发送，经网关转发至站点代理。

=== 电网信号（grid_signal）

资源 `signals` 为长表：每行给出一个区域在一个时段内一种信号的值。新的信号类型只是 `signal` 字段的新取值，不需要改动表结构。
这一组织方式与 OpenADR 3.0 以"时段—载荷"表达价格、温室气体与负荷限制等信号的方式一致 @openadr3。

#figure(
  tbl(
    columns: (9.5em, 6.5em, 1fr),
    header: ("字段", "类型", "说明"),
    [`region`（核心）], [string], [区域键（省份、平衡区等），与站点的 `region` 对应],
    [`start`、`end`（核心）], [datetime], [时段 \[`start`, `end`)，带时区],
    [`signal`（核心）], [string], [信号类型，如 `price`、`carbon_intensity`],
    [`value`（核心）], [number], [信号值，单位由描述文件 `cpc.signals` 声明],
    [`label`], [string], [时段标签，如分时电价的 `peak`、`valley`],
    [`is_estimated`], [boolean], [是否为估计值],
  ),
  caption: [电网信号数据集的字段],
)

描述文件属性 `cpc.signals` 声明数据集所含的每种信号及其单位，如 `price: {unit: CNY/kWh}`、
`carbon_intensity: {unit: kgCO2/kWh, basis: lifecycle}`。电价的单位须为 `<货币>/kWh`，碳强度的单位须为 `kgCO2/kWh`。
同一区域、同一信号的时段不得重叠；时段之间的空隙与覆盖范围之外视为信号不可用。

=== 功率特性（power_profile）

资源 `measurements`，每行是设备在给定并发下的一次稳态测量，用于标定服务器模型（@sec-calibration）。

#figure(
  tbl(
    columns: (9.5em, 6.5em, 1fr),
    header: ("字段", "类型", "说明"),
    [`device`（核心）], [string], [设备型号，如 `H100`],
    [`device_count`（核心）], [integer], [一个推理实例占用的设备数，≥ 1],
    [`model`（核心）], [string], [模型名],
    [`concurrency`（核心）], [number], [平均并发（运行中的序列数）],
    [`power_w`（核心）], [number，W], [全部设备的平均总功率],
    [`itl_s`（核心）], [number，s], [平均 token 间隔，> 0],
    [`max_concurrency`], [integer], [并发上限（如 vLLM 的 `max_num_seqs`）],
    [`throughput_tokens_per_s`、`ttft_s`、`energy_per_token_j`、`task`], [—], [输出吞吐、平均首 token 时延、每输出 token 能耗、负载任务名],
  ),
  caption: [功率特性数据集的字段],
)

== 校验与加载

`cpc.datasets.descriptor.load_descriptor` 读取描述文件并检查其结构与类别规范；`cpc.datasets.table.read_table`
以 Apache Arrow 读取所需的列，按声明的类型解析，检查约束与核心字段的非空要求。任何不符合都报告为 `DatasetError`，
平台不修正、不丢弃、不填补数据。负载加载后再检查到达时间的顺序；电网信号加载后检查时段与单位。
`cpc data check <路径>` 对数据集做完整校验，并报告各资源的行数与统计。

== 转换范例

`examples/datasets/` 中的脚本演示如何把公开数据整理为平台格式。脚本不属于平台核心；转换中的取舍（时区解释、
异常记录的处理、外部参数）都以命令行参数显式给出，并记录在描述文件的 `cpc.conversion` 中。

#figure(
  tbl(
    columns: (11em, 7em, 1fr),
    header: ("范例", "类别", "原始数据与要点"),
    [`azure_llm.py`], [workload], [Azure LLM Inference Trace 2023（1 小时）与 2024（1 周） @patel2024splitwise @azure2024trace。时间戳按 UTC 解释，`cpc.origin` 取最早的时间戳；输出 token 数为 0 的请求须显式选择报错或剔除。],
    [`burstgpt.py`], [workload], [BurstGPT v2.0 @wang2025burstgpt。扩展字段 `model`、`log_type`；响应 token 数为 0 的失败请求须显式处理。],
    [`mooncake.py`], [workload], [Mooncake FAST'25 trace @qin2025mooncake。`hash_ids` 转为约定扩展字段 `prefix_hashes`。],
    [`synthetic_workload.py`], [workload], [合成的非齐次泊松到达负载，只用于测试与功能演示。],
    [`cn_grid/convert.py`], [grid_signal], [省级工商业分时电价规则 @gd2021tou @gz2023tou @nm2023tou @sh2022tou 展开为逐时段电价（`label` 为时段名）；2023 年省级电力平均排放因子 @mee2025factors 作为转换时间范围内的恒定碳强度，该假设记录在描述文件中。],
    [`eia930.py`], [grid_signal], [EIA-930 各平衡区逐时分燃料净发电量 @eia930 与分燃料生命周期排放因子 @ipcc2014annex3 @electricitymaps2026factors 计算逐时碳强度；数据不完整的小时不输出。],
    [`constant_signal.py`], [grid_signal], [由参数给定的恒定信号（非实测），用于某种信号没有实测数据而取约定值的情形。],
    [`mlenergy.py`], [power_profile], [ML.ENERGY Benchmark v3 的 LLM 运行汇总 @chung2025mlenergy @mlenergy2025v3。],
  ),
  caption: [转换范例],
)

编写新的转换时，可使用 `cpc.datasets.build.write_dataset`：给出 Arrow 表、字段定义与描述属性，写出数据文件与描述文件，
并按类别规范完整校验一次。
