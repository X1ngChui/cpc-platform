#import "../template/cpc-doc.typ": *

= 能耗感知操作 <g-energy>

== 探测本机传感器

```bash
cpc sensors --list           # 全部传感器类型、适用平台、验证状态、本机是否可用
cpc sensors                  # 自动探测并连续读 3 次
cpc sensors --types nvml,rapl --count 5 --interval 2
cpc sensors --json           # JSON 输出，便于脚本处理
```

在一台装有 NVIDIA 显卡的 Windows 笔记本上的输出示例：

```text
传感器： ['nvml']
  [nvml] gpu0   gpu   nvidia   NVIDIA GeForce RTX 5070 Ti   43.36 W   6271566.3 J   2.0 %
  主机合计 43.36 W
```

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("平台", "通常可用的传感器"),
    [Linux + NVIDIA GPU], [`nvml`（需 `pip install nvidia-ml-py`）、`nvidia-smi`；裸金属另有 `rapl`、`ipmi`],
    [Linux + AMD GPU], [`amdsmi`（ROCm 自带）或 `hwmon`（无需依赖）；`rapl`],
    [Linux + Intel 独显], [`hwmon`（i915 / xe 驱动的能量计数器）；`rapl`],
    [Linux + 昇腾 NPU], [`ascend-dcmi`（驱动自带 libdcmi.so）；或部署官方 NPU-Exporter 后用 `prometheus`],
    [Windows], [`nvml`、`nvidia-smi`；CPU 功率需运行 LibreHardwareMonitor（管理员）后使用 `lhm`],
    [macOS（Apple Silicon）], [`powermetrics`（需 `sudo`）],
    [云虚拟机], [通常只有 GPU 传感器；RAPL 与 BMC 一般不对租户开放],
    [服务器 BMC], [`ipmi`（本机或远程）、`redfish`],
  ),
  caption: [各平台通常可用的传感器],
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

同样的列表可以写在场景文件的站点配置 `sensors:` 中，供真实站点的站点代理使用。

== 部署通用导出器

在任意需要监测的机器上运行通用导出器，Prometheus 抓取 `:9400/metrics`：

```bash
cpc exporter --port 9400 --site gz                 # 自动探测
cpc exporter --port 9400 --spec sensors.yaml       # 按配置
docker run -d --gpus all -p 9400:9400 xingchui/cpc-platform:0.1.0 exporter --types nvml --site gz
```

导出的指标：`cpc_device_power_watts`、`cpc_device_energy_joules_total`、`cpc_device_utilization_percent`、`cpc_host_power_watts`，
NVIDIA GPU 另有 `DCGM_FI_DEV_POWER_USAGE` 等 DCGM 兼容指标。记录规则 `host_kind:device_power_watts:sum` 按主机与设备类别汇总。

== 新增一种硬件

```python
# my_sensors.py
from cpc.sensors import Reading, Sensor, register_sensor

@register_sensor("my-accel")
class MyAccel(Sensor):
    platforms = "Linux"

    @classmethod
    def detect(cls) -> bool:          # 可选：本机是否可用
        return True

    def read(self):
        watts = 123.0                 # 调用厂商 SDK / 读取文件 / 解析命令输出
        return [Reading("my-accel", "acc0", "npu", vendor="me", power_w=watts)]
```

用 `CPC_PLUGINS=my_sensors cpc sensors --types my-accel` 测试。

= 常用操作速查 <g-ref>

== 命令

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("命令", "作用"),
    [`cpc up`], [本机单进程启动全部服务（`--speedup`、`--routing`、`--capacity`、`--interval`、`--no-controller`）],
    [`cpc grid` / `site` / `gateway` / `controller`], [单独启动某个服务（`-s` 场景、`-e` 地址文件、`--host`/`--port` 覆盖监听地址）],
    [`cpc loadgen`], [负载生成（`--duration`、`--rps`、`--trace`、`--no-stream`、`--model`、`--out`）],
    [`cpc sim`], [离散事件仿真（`--routing`、`--capacity`、`--hours`、`--rps`、`--out`）],
    [`cpc compare`], [并行仿真多组策略（`--runs "路由:容量,..."`）],
    [`cpc report`], [仿真结果作图],
    [`cpc signals` / `cpc rtt`], [打印电网信号 / RTT 矩阵],
    [`cpc event`], [注入需求响应事件（`--site`、`--cap-kw`、`--duration-min`）],
    [`cpc sensors` / `cpc exporter`], [功率传感器探测 / 通用功率导出器],
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
    [`CPC_ADMIN_TOKEN`], [管理接口令牌；服务端校验、控制器与命令行工具调用时自动携带],
    [`CPC_PLUGINS`], [逗号分隔的插件模块名（策略、传感器）],
    [`CPC_LOG_LEVEL`], [日志级别，默认 INFO],
    [`IPMI_PASSWORD` / `REDFISH_PASSWORD`], [远程 BMC 密码],
  ),
  caption: [环境变量],
)

== 场景文件常改的字段

- 改站点：`sites[]`（增删站点后，Kubernetes 部署需重新运行 `deploy/k8s/generate.py`，Compose 需相应增删 `site-*` 服务）；
- 改负载：`workload.rps`、`diurnal_amplitude`、`peak_hour`、token 分布；
- 改日期：`clock.start`（决定季节与分时时段）；
- 加需求响应：`grid.dr_events[]`；
- 改默认策略：`scheduler.routing`、`scheduler.capacity`；
- 改碳价：`scheduler.carbon_price_yuan_per_kg`（影响 `effective_price`）。
