#import "../template/cpc-doc.typ": *

= 能耗感知操作 <g-energy>

== 检查本机传感器

```bash
cpc sensors --list                                  # 全部传感器类型、依赖、验证状态、本机是否具备前提条件
cpc sensors --types nvml                            # 读取指定传感器，连续读 3 次
cpc sensors --types nvml,rapl --count 5 --interval 2
cpc sensors --types nvml --json                     # JSON 输出，便于脚本处理
```

平台不自动选择传感器：读取、导出与站点代理使用的传感器都须显式指定。传感器构造失败（驱动或库不可用、
必需能力缺失）时命令以状态码 2 退出并给出原因；读取中某个字段失败时，该值显示为 `-` 并列出错误。

WSL2（Ubuntu 26.04，NVIDIA GeForce RTX 5070 Ti，驱动 621.24）中的输出示例：

```text
传感器： ['nvml']
  [nvml] gpu0   gpu   nvidia   NVIDIA GeForce RTX 5070 Ti   45.62 W   6905602.1 J   1.0 %
  主机合计 45.62 W
```

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("环境", "通常可用的传感器"),
    [NVIDIA GPU], [`nvml`（需 `pip install nvidia-ml-py`）、`nvidia-smi`；裸金属另有 `rapl`、`ipmi`],
    [AMD GPU], [`amdsmi`（ROCm 自带）或 `hwmon`（无需依赖）；`rapl`],
    [Intel 独显], [`hwmon`（i915 / xe 驱动的能量计数器）；`rapl`],
    [昇腾 NPU], [`ascend-dcmi`（驱动自带 libdcmi.so）；或部署官方 NPU-Exporter 后用 `prometheus`],
    [云虚拟机], [通常只有 GPU 传感器；RAPL 与 BMC 一般不对租户开放],
    [WSL2], [`nvml`、`nvidia-smi`（由宿主机驱动经 `/usr/lib/wsl/lib` 提供）；`rapl`、`hwmon`、`ipmi` 不可用],
    [服务器 BMC], [`ipmi`（本机或远程）、`redfish`],
  ),
  caption: [各类 Linux 环境中通常可用的传感器],
)

== 用配置文件指定传感器

需要参数的传感器（远程 BMC、Prometheus 查询、自定义命令）用 YAML 描述：

```yaml
# sensors.yaml
sensors:
  - {type: nvml}
  - {type: rapl}
  - {type: ipmi, host: 10.0.0.21, user: admin}          # 密码放在环境变量 IPMI_PASSWORD 中
  - {type: redfish, url: "https://10.0.0.21", username: admin, verify: false}  # 自签名证书时显式关闭校验
  - {type: prometheus, url: "http://prom:9090", kind: gpu, device_label: gpu,
     query: "DCGM_FI_DEV_POWER_USAGE{Hostname='node-1'}"}
  - {type: command, kind: npu, vendor: huawei,             # 任意命令行工具 + 正则（以所装版本的实际输出为准）
     cmd: ["npu-smi", "info", "-t", "power", "-i", "0"], regex: "Power Dissipation\\(W\\)\\s*:\\s*([0-9.]+)"}
  - {type: http-json, url: "http://pdu-1/api/status", path: "outlets[3].power", kind: server}
```

```bash
cpc sensors --spec sensors.yaml
```

同样的列表写在场景文件中真实站点的 `power.sensors` 中，供站点代理使用，并用 `power.host` 指定主机部分功率的确定方式
（`server`：整机读数减加速卡；`cpu_dram`：CPU 与内存读数之和；`excluded`：不计主机部分）。

== 部署通用导出器

在任意需要监测的机器上运行通用导出器，Prometheus 抓取 `:9400/metrics`：

```bash
cpc exporter --port 9400 --types nvml --site gz   # 按类型
cpc exporter --port 9400 --spec sensors.yaml       # 按配置
docker run -d --gpus all -p 9400:9400 xingchui/cpc-platform:0.1.0 exporter --types nvml --site gz
```

导出器启动时逐一读取指定的传感器，任一失败即退出。导出的指标：`cpc_device_power_watts`、`cpc_device_energy_joules_total`、
`cpc_device_utilization_percent`（读数缺失的字段不输出样本）、`cpc_sensor_up`、`cpc_sensor_errors_total`；
NVIDIA GPU 另有 `DCGM_FI_DEV_POWER_USAGE` 等 DCGM 兼容指标。记录规则 `host_kind:device_power_watts:sum` 按主机与设备类别汇总。

== 新增一种硬件

```python
# my_sensors.py
from cpc.sensors import Reading, Sensor, register_sensor

@register_sensor("my-accel")
class MyAccel(Sensor):
    requires = "厂商 SDK"         # 文档用途：依赖的驱动或库

    def __init__(self, **params):
        super().__init__(**params)
        # 初始化厂商 SDK、确定设备集合；必需能力不可用时抛出 SensorError（启动失败）

    @classmethod
    def detect(cls) -> bool:          # 可选：cpc sensors --list 中显示本机是否具备前提条件
        return True

    def read(self):
        r = Reading(self.id, "acc0", "npu", vendor="me")
        self.field(r, "power_w", lambda: 123.0)   # 调用 SDK；失败时值为 None，原因记入 r.errors
        return [r]
```

用 `CPC_PLUGINS=my_sensors cpc sensors --types my-accel` 测试。

= 数据集 <g-data>

== 查看与下载

```bash
cpc data list                                   # 全部数据集：类别、版本、本地状态（就绪 / 未下载 / 不完整 / 合成）、大小、许可
cpc data info azure-llm-2024-conv               # 清单：来源、许可、引用、文件地址与 SHA-256、本地路径
cpc data fetch azure-llm-2023-conv mooncake-fast25-conversation
cpc data verify azure-llm-2024-conv             # 重新校验本地文件
cpc data summary azure-llm-2024-conv            # 请求数、时长、平均到达率、token 均值
cpc data summary burstgpt-1 --params '{"include_failed": false}'
```

文件下载到 `$CPC_DATA_DIR`（缺省 `~/.cache/cpc/datasets`）下的 `<名称>/<版本>/`，下载后逐一校验 SHA-256。外部下载使用系统代理设置。
受控访问的数据集（`mlenergy-benchmark-v3-llm`）需先在其 Hugging Face 页面申请访问，再以环境变量 `HF_TOKEN` 提供访问令牌；
读取 parquet 文件需要 `pip install -e ".[data]"`（pyarrow）。

== 在场景中使用

```yaml
workload:
  dataset: azure-llm-2024-conv
  start_s: 230400        # 时间窗起点（原始 trace 秒）
  duration_s: 86400      # 时间窗长度
  time_scale: 1.0        # >1 表示时间压缩
  rate_scale: 0.5        # 到达率缩放
  seed: 1
grid:
  price:  {dataset: cn-tou-tariffs}
  carbon: {dataset: cn-provincial-carbon-2023}
```

美国平衡区的逐时碳强度示例见 `configs/scenario.us-eia.yaml`（`eia930-carbon-2024h1`，排放因子在场景中显式给出）。
对全部已下载的公开数据集运行仿真检验：`python scripts/dataset_check.py`（结果写入 `results/dataset-check/summary.md`）。

== 标定节点参数

```bash
cpc data calibrate mlenergy-benchmark-v3-llm --model meta-llama/Llama-3.1-405B-Instruct-FP8 \
    --gpu H100 --task lm-arena-chat --num-gpus 8 --slots 128     # 默认场景节点参数的来源
```

输出可写入场景 `node_profiles` 的参数（`gpus`、`slots`、`gpu_active_min_w`、`gpu_max_w`、`power_exponent`、
`decode_s_per_token`、`decode_slowdown`）、拟合残差与所用运行。空载功率、主机功率与预填充吞吐不在数据集中，须另行给定。

= 健康状态与故障排查 <g-health>

== 健康与就绪接口

各服务提供 `/healthz`（存活）与 `/readyz`（就绪，未就绪时返回 503 并给出原因）。故障被限制在其所属的读数或组件之内，
并以以下方式报告：

#figure(
  tbl(
    columns: (11em, 1fr),
    header: ("位置", "内容"),
    [站点 `/state`], [节点 `power_missing`（缺失的功率组成部分）与 `last_error`（开关机或封顶失败）；`components.sensors`（各传感器的健康状态、最近错误、探测到的能力）；`energy_coverage`（功率有效时长占比）],
    [网关 `/state`、`/readyz`], [`upstreams`：电网信号服务与各站点状态拉取的健康状态；路由视图中可用的站点],
    [控制器 `/state`], [`upstreams`；最近一次周期的 `result`（`applied` / `skipped`）、跳过原因与排除的站点],
    [指标], [`cpc_sensor_up`、`cpc_sensor_errors_total`、`cpc_upstream_up{component,upstream}`、`cpc_upstream_errors_total`、`cpc_gateway_attempts_total{site,outcome}`、`cpc_gateway_route_decisions_total{policy,reason}`、`cpc_controller_cycles_total{result,reason}`、`cpc_controller_collect_errors_total{target}`、`cpc_node_actuation_errors_total`],
    [日志], [状态变化（`sensor_down`、`upstream_up` 等）、跳过的控制周期与执行失败；`CPC_LOG_FORMAT=json` 时为每行一个 JSON 对象，含 `event` 字段],
  ),
  caption: [故障的显式报告],
)

== 启动失败

配置或环境错误在启动时报告，命令以状态码 2（服务进程由 uvicorn 报告启动失败时为 3）退出，常见原因：

- 场景文件含未知字段或引用的文件不存在（相对路径相对于场景文件所在目录）；
- 未给出地址文件：除 `cpc up` 外，访问其他服务的命令须用 `-e` / `CPC_ENDPOINTS` 或 `--local`；
- 真实后端站点未声明 `power`，或声明的传感器不可用、未覆盖节点占用的加速卡；
- 网关、控制器或负载生成器在期限内（`--startup-timeout`，默认 30 s）无法从电网信号服务同步信号时钟。
  Docker Compose 与 Kubernetes 会按重启策略重新启动这些服务，电网信号服务就绪后即恢复。

= 常用操作速查 <g-ref>

== 命令

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("命令", "作用"),
    [`cpc up`], [本机单进程启动全部服务（`--speedup`、`--routing`、`--capacity`、`--interval`、`--no-controller`）],
    [`cpc grid` / `site` / `gateway` / `controller`], [单独启动某个服务（`-s` 场景、`-e` 地址文件或 `--local`、`--host`/`--port` 覆盖监听地址）],
    [`cpc loadgen`], [负载生成（`--duration`、`--start-s`、`--duration-s`、`--time-scale`、`--rate-scale`、`--no-stream`、`--model`、`--out`）],
    [`cpc sim`], [离散事件仿真（`--routing`、`--capacity`、`--hours`、负载变换参数、`--out`）],
    [`cpc compare`], [并行仿真多组策略（`--runs "路由:容量,..."`、负载变换参数）],
    [`cpc data`], [数据集：`list`、`info`、`fetch`、`verify`、`summary`、`calibrate`],
    [`cpc report`], [仿真结果作图],
    [`cpc signals` / `cpc rtt`], [打印电网信号 / RTT 矩阵],
    [`cpc event`], [注入需求响应事件（`--site`、`--cap-kw`、`--duration-min`）],
    [`cpc sensors` / `cpc exporter`], [列出或读取功率传感器（`--list`、`--types`、`--spec`）/ 通用功率导出器],
    [`python scripts/smoke_test.py`], [端到端冒烟测试（`--external -e 地址文件` 检查已部署环境）],
    [`python scripts/make_figures.py`], [复现文档插图],
    [`python docs/build.py`], [编译文档 PDF],
  ),
  caption: [命令速查],
)

== 环境变量

#figure(
  tbl(
    columns: (12em, 1fr),
    header: ("变量", "作用"),
    [`CPC_ENDPOINTS`], [地址文件路径（等价于 `-e`）],
    [`CPC_DATA_DIR`], [数据集缓存目录，缺省 `~/.cache/cpc/datasets`],
    [`HF_TOKEN`], [Hugging Face 访问令牌（下载受控访问的数据集）],
    [`CPC_LOG_FORMAT`], [日志格式：`text`（默认）或 `json`（结构化日志）],
    [`CPC_ADMIN_TOKEN`], [管理接口令牌；服务端校验、控制器与命令行工具调用时自动携带],
    [`CPC_PLUGINS`], [逗号分隔的插件模块名（策略、传感器）],
    [`CPC_LOG_LEVEL`], [日志级别，默认 INFO],
    [`IPMI_PASSWORD` / `REDFISH_PASSWORD`], [远程 BMC 密码],
  ),
  caption: [环境变量],
)

== 场景文件常改的字段

- 改站点：`sites[]`（增删站点后，Kubernetes 部署需重新运行 `deploy/k8s/generate.py`，Compose 需相应增删 `site-*` 服务）；
- 改负载：`workload.dataset` 与时间窗、`time_scale`、`rate_scale`；
- 改日期：`clock.start`（决定季节与分时时段）；
- 加需求响应：`grid.dr_events[]`；
- 改默认策略：`scheduler.routing`、`scheduler.capacity`；
- 改碳价：`scheduler.carbon_price_yuan_per_kg`（影响 `effective_price`）。
