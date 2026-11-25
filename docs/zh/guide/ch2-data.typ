#import "../../template/cpc-doc.typ": *

= 数据 <g-data>

平台使用三类数据：请求负载（workload）、电网信号（grid_signal）与功率特性（power_profile）。每个数据集是一个目录，
包含一个描述文件 `datapackage.yaml` 与一个数据文件（CSV 或 Parquet）。描述文件说明数据集的名称、版本、来源、许可，
以及数据文件中每个字段的名称、类型与单位；平台按描述文件读取并检查数据。数据规范的完整定义见《平台设计说明书》"数据规范"一章。

== 数据目录

场景文件通过路径引用数据集，通常写为 `${CPC_DATA_DIR}/<数据集名>`。环境变量 `CPC_DATA_DIR` 未设置时取
`~/.cache/cpc/datasets`。如需放在其他位置（例如容量更大的磁盘），在 shell 中设置：

```bash
export CPC_DATA_DIR=/data/cpc-datasets       # 可写入 ~/.bashrc 使其长期生效
```

原始数据与转换结果都放在这个目录中，不提交到代码仓库。转换脚本把下载的原始文件放在 `${CPC_DATA_DIR}/raw/` 下。

== 三类数据集的字段

每类数据集规定了*核心字段*：完成基本调度所必需，数据中不得为空。另有一些*约定扩展字段*，名称与含义统一，参考策略可能使用；
其余字段为*任意扩展字段*，平台原样保留，可以在场景中选择传给策略。`cpc data kinds` 打印全部字段的定义。

#figure(
  tbl(
    columns: (7.5em, 9em, 1fr),
    header: ("类别", "核心字段", "说明"),
    [workload], [`arrival_s`、`input_tokens`、`output_tokens`], [每行一个请求：到达时间（相对数据集零点的秒数，须按非降序排列）、输入与输出 token 数。约定扩展字段有 `request_id`、`model`、`session_id`、`priority`、`slo_class`、`tenant`、`prefix_hashes`。],
    [grid_signal], [`region`、`start`、`end`、`signal`、`value`], [每行是某区域在时段 \[start, end) 内一种信号的值。`signal` 取 `price`（电价）、`carbon_intensity`（碳强度）或自定义的信号名；各信号的单位在描述文件的 `cpc.signals` 中声明。],
    [power_profile], [`device`、`device_count`、`model`、`concurrency`、`power_w`、`itl_s`], [每行是一次稳态测量：设备型号与数量、模型、平均并发、全部设备的平均总功率（W）、平均 token 间隔（秒）。用于生成服务器类型。],
  ),
  caption: [三类数据集],
)

== 转换公开数据集

`examples/datasets/` 中的脚本把常用的公开数据转换为平台格式。每个脚本缺省自动下载原始文件；已经下载过的，可用 `--raw` 指定文件。

#figure(
  tbl(
    columns: (15em, 1fr),
    header: ("命令（均在 examples/datasets/ 下）", "生成的数据集"),
    [`azure_llm.py --variant 2023-conv --zero-output fail`], [`azure-llm-2023-conv`：Azure 对话服务约 1 小时的请求（另有 `2023-code`；`2024-conv`、`2024-code` 为 1 周，原始文件约 1 GB）],
    [`cn_grid/convert.py --start 2026-01-01 --end 2027-01-01`], [`cn-grid-2026`：广东、贵州、内蒙古（蒙西）、上海的分时电价与碳强度],
    [`mlenergy.py`], [`mlenergy-benchmark-v3-llm`：H100、B200 上各模型的功率与 token 间隔测量],
  ),
  caption: [转换范例],
)

参数中的取舍须显式给出。例如平台要求输出 token 数至少为 1，`--zero-output fail` 表示遇到输出为 0 的请求时报错，
`--zero-output drop` 表示剔除这些请求；剔除的数量写入描述文件，便于日后查阅。`mlenergy.py` 下载的数据集需要先在
Hugging Face 的数据集页面申请访问，再把访问令牌写入环境变量 `HF_TOKEN`。

== 检查与查看数据集

```bash
cpc data check ${CPC_DATA_DIR}/azure-llm-2023-conv     # 完整检查并打印统计
cpc data show  ${CPC_DATA_DIR}/azure-llm-2023-conv     # 打印描述文件：来源、许可、字段
cpc data kinds                                         # 打印三类数据集的字段定义
```

`cpc data check` 的输出示例：

```text
azure-llm-2023-conv@7909210 (workload) is valid: requests 19366 rows
{
  "ref": "azure-llm-2023-conv@7909210",
  "requests": 19366,
  "duration_s": 3501.7,
  "mean_rps": 5.53,
  ...
}
```

数据不符合规范时，输出指出数据集、字段、违反的约束与具体取值，例如
"dataset my-trace\@1, resource requests, field output_tokens: value 0 violates specification minimum 1"。

== 整理自己的数据

整理自己的数据只需两步：把数据写成一张表，再写一个描述文件。以下以一份 CSV 格式的请求日志为例。

*第一步：准备数据文件。* 列名与类别规范的字段名一致；可以加入其他列作为扩展字段。

```text
my-trace/requests.csv
arrival_s,input_tokens,output_tokens,tenant
0.000,812,210,a
0.814,95,64,b
1.502,2048,512,a
...
```

*第二步：编写描述文件。* 在同一目录下创建 `datapackage.yaml`：

```yaml
name: my-trace                     # 数据集名称（小写字母、数字与连字符）
version: "2026.11"                 # 版本，任意字符串
title: 业务系统的推理请求日志
licenses: [{name: internal}]
sources: [{title: 业务系统日志导出，2026-11-01}]
cpc:
  kind: workload                   # 类别
  profile: 1                       # 类别规范的版本，当前为 1
  origin: "2026-11-01T00:00:00+08:00"   # arrival_s = 0 对应的时刻（可选）
resources:
  - name: requests                 # 负载数据集的资源名固定为 requests
    path: requests.csv
    format: csv
    schema:
      fields:
        - {name: arrival_s, type: number, unit: s}
        - {name: input_tokens, type: integer}
        - {name: output_tokens, type: integer}
        - {name: tenant, type: string, description: 业务标识}
```

然后用 `cpc data check my-trace` 检查。也可以在 Python 中用 `cpc.datasets.build.write_dataset` 一次写出数据文件与描述文件
并完成检查，`examples/datasets/` 中的脚本都使用这种方式：

```python
import pyarrow as pa
from cpc.datasets.build import write_dataset

table = pa.table({"arrival_s": [0.0, 0.814, 1.502], "input_tokens": [812, 95, 2048],
                  "output_tokens": [210, 64, 512], "tenant": ["a", "b", "a"]})
fields = [{"name": "arrival_s", "type": "number", "unit": "s"},
          {"name": "input_tokens", "type": "integer"},
          {"name": "output_tokens", "type": "integer"},
          {"name": "tenant", "type": "string"}]
write_dataset("my-trace", name="my-trace", version="2026.11", kind="workload",
              table=table, fields=fields, licenses=[{"name": "internal"}],
              cpc={"origin": "2026-11-01T00:00:00+08:00"})
```

电网信号的数据文件是一张长表，每行一个区域、一个时段、一种信号。例如两个区域的逐时电价：

```text
region,start,end,signal,value
east,2026-07-15T00:00:00+08:00,2026-07-15T01:00:00+08:00,price,0.31
east,2026-07-15T01:00:00+08:00,2026-07-15T02:00:00+08:00,price,0.29
west,2026-07-15T00:00:00+08:00,2026-07-15T01:00:00+08:00,price,0.22
...
```

描述文件中 `cpc.kind` 为 `grid_signal`，资源名为 `signals`，`start`、`end` 字段的类型为 `datetime`，并声明信号的单位：

```yaml
cpc:
  kind: grid_signal
  profile: 1
  signals:
    price: {unit: CNY/kWh}              # 电价单位须为 <货币>/kWh
    carbon_intensity: {unit: kgCO2/kWh} # 碳强度单位须为 kgCO2/kWh
```

== 把扩展字段传给策略

负载数据集中的扩展字段可以作为请求属性交给调度策略使用，在场景的 `workload.attributes` 中列出：

```yaml
workload:
  dataset: /data/my-trace
  attributes: [tenant]        # 须是描述文件中声明过的字段
```

策略通过 `req.attrs["tenant"]` 读取（@g-policy）。仿真中属性随请求直接传递；实验中 `cpc live` 的负载回放以请求头
`x-cpc-attr-tenant` 发送，网关转发给站点代理。电网信号数据集中的其他信号（例如可再生能源占比）在场景的 `grid.extras`
中声明后，策略可以从 `signal.extra` 中读取（@g-scenario）。

== 生成服务器类型 <g-profile>

服务器类型以两张表描述服务器：`power_w` 是服务器 IT 功率随占用率的变化，`tpot_s` 是 token 间隔随占用率的变化。
表的点之间线性插值。生成表有三种方式。

*由测量数据生成。* 功率特性数据集中的每次测量（某个并发下的功率与 token 间隔）就是表中的一个点：

```bash
cpc profile ${CPC_DATA_DIR}/mlenergy-benchmark-v3-llm \
    --slots 128 --idle-w 1360 --host-w 800 \
    --where device=H100 device_count=8 model=meta-llama/Llama-3.1-405B-Instruct-FP8 \
            task=lm-arena-chat \
    --prefill-tokens-per-s 5000 --boot-s 180 --name h100x8
```

`--where` 选出同一组测量（同一设备、设备数、模型；同一组中如有多个任务，还须指定 `task`）；`--slots` 为服务器的并发槽数，
占用率 = 测量的并发 / 并发槽数；测量只包含设备在服务时的功率，因此在线空载功率（`--idle-w`，表的第一个点）与主机部分的功率
（`--host-w`，加到每个测量点上）须另行给出。命令打印一段 YAML，复制到场景的 `node_profiles` 下即可：

```yaml
h100x8:
  description: 8xH100, meta-llama/Llama-3.1-405B-Instruct-FP8; ...
  slots: 128
  power_w:
  - [0.0, 1360.0]
  - [0.0624, 4060.4]
  ...
  tpot_s:
  - [0.0624, 0.03406]
  ...
  prefill_tokens_per_s: 5000.0
  boot_s: 180.0
```

`cpc data check <数据集>` 列出功率特性数据集中有哪些测量组。自己的服务器用 `cpc measure` 测量（@g-calibrate），
它写出的 power_profile 数据集同时记录了并发槽数、在线空载功率与开机时间，`cpc profile` 直接使用这些测量值。

*手工填写。* 已知几个占用率下的功率与 token 间隔时直接填写，至少需要空载（占用率 0）与一个在服务的点：

```yaml
my-server:
  slots: 32
  power_w: [[0, 180], [0.25, 260], [0.5, 320], [1, 400]]
  tpot_s: [[0, 0.03], [1, 0.05]]
  prefill_tokens_per_s: 4000
  boot_s: 90
```

*按公式计算。* 如果用某个公式描述功率，在若干占用率上计算后写入表即可，例如：

```python
# 单卡功率 = 空载 + (满载 − 空载) × 占用率^alpha
gpus, gpu_idle, gpu_max, alpha = 8, 60, 400, 0.7
host_idle, host_max = 800, 1500                         # 主机功率随占用率线性增长
us = [0, 0.25, 0.5, 0.75, 1]
def server_w(u):
    gpu = gpu_idle + (gpu_max - gpu_idle) * u**alpha
    host = host_idle + (host_max - host_idle) * u
    return round(gpus * gpu + host, 1)

table = [[u, server_w(u)] for u in us]
# [[0, 1280.0], [0.25, 2485.7], [0.5, 3304.4], [0.75, 4028.9], [1, 4700.0]]
print(table)
```
