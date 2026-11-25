#import "../../template/cpc-doc.typ": *

= 能耗测量与核算 <ch-energy>

功率与能耗在四个层次上获得：传感器层给出设备读数；服务器层把读数归属到服务器（仿真服务器由服务器模型给出，@sec-server-model）；
设施层以 PUE 换算设施功率；电网层以电价与碳强度换算成本与碳排放。

== 传感器层

=== 统一读数结构

所有传感器输出同一种读数结构 `Reading`：

#figure(
  tbl(
    columns: (6em, 1fr),
    header: ("字段", "含义"),
    [`sensor`], [传感器类型名，如 `nvml`、`rapl`、`ipmi`],
    [`device`], [设备标识，在同一传感器内唯一，如 `gpu0`、`package-0`、`npu0-0`、`server`],
    [`kind`], [设备类别：`gpu`、`npu`、`cpu`、`dram`、`soc`、`server`（整机）、`platform`（平台总功率）、`other`],
    [`vendor`、`model`], [厂商与型号],
    [`power_w`], [功率（W）。不同接口给出的可能是瞬时值或驱动内部的短时平均值],
    [`energy_j`], [累计能耗（J）。硬件提供计数器时直接使用，否则由上层按功率积分],
    [`util_pct`], [利用率（%），若可得],
    [`errors`], [本次读取中失败的字段及原因；失败字段的值为 `None`],
  ),
  caption: [功率读数结构],
)

新增一种硬件只需实现一个 `Sensor` 子类的 `read()` 方法，并以 entry point（组 `cpc.sensors`）注册（@ch-ext）；
站点代理、通用导出器、命令行工具都通过这一层获取功率，不依赖具体硬件。传感器的错误处理约定为：

- 构造阶段确定设备集合以及各字段使用的接口（能力探测，结果由 `capabilities()` 给出）；必需能力不可用时抛出 `SensorError`，
  使站点代理或导出器启动失败。例如 NVML 的功率读数为必需能力，累计能耗计数器（Volta 及以后的 GPU 提供）与利用率为可选能力；
- `read()` 整体失败（命令执行失败、接口不可达、输出无法解析）时抛出异常，由调用方记入该传感器的健康状态；
- 单个设备或字段读取失败时，该值为 `None`，原因记入 `errors`；计数器差分的首次采样没有前一时刻，功率为 `None` 但不计为错误。

=== 已支持的传感器

#figure(
  tbl(
    columns: (6.5em, 10em, 1fr),
    header: ("类型", "测量对象", "接口"),
    [`nvml`], [NVIDIA GPU], [NVIDIA 官方 NVML 的 Python 绑定 nvidia-ml-py：功率、累计能耗计数器、利用率、功率上限设置],
    [`rapl`], [CPU 封装、内存（Intel / AMD）], [内核 powercap sysfs（RAPL 能量计数器，处理回绕）],
    [`ipmi`], [服务器整机（本机 BMC）], [`ipmitool dcmi power reading`（经 `/dev/ipmi0`）],
    [`command`], [其他任意设备], [子进程 + 正则：调用厂商的管理工具或读取采集程序的输出，读数种类由配置给出],
  ),
  caption: [内置功率传感器（Linux）],
)

内置传感器每种测量一类对象（加速卡、CPU 与内存、整机），互不重叠，均在裸金属服务器上验证。其他硬件以 `command` 适配，
或实现 `Sensor` 子类并以 entry point 组 `cpc.sensors` 注册（@ch-ext）。

所有传感器接受通用参数 `max_w`（功率读数的合理上限）：超过上限的读数记为缺失并记录原因，不以其他值替代，
用于排除 BMC 等偶发的异常读数。
`cpc sensors --list` 列出全部传感器类型及本机是否具备其前提条件；`cpc sensors --types nvml,rapl` 读取指定传感器并打印读数。平台不自动选择传感器：站点与导出器使用的传感器均由配置显式给出，
同一设备不应由两个传感器同时报告（站点代理在启动时检查加速卡设备名是否重复）。

=== 采集方案选型

在真实环境中，平台既可以由站点代理直接读取传感器，也可以复用已部署的标准导出器：

#figure(
  tbl(
    columns: (9em, 1fr, 1fr),
    header: ("方案", "适用", "说明"),
    [NVIDIA dcgm-exporter], [Kubernetes / Linux 上的数据中心 GPU], [NVIDIA GPU Operator 的组成部分，指标最全；消费级显卡支持有限。平台的 GPU 指标与其同名。],
    [平台通用导出器 `cpc exporter`], [任意 Linux 服务器], [依赖少，支持多种硬件；NVIDIA GPU 同时输出 DCGM 兼容指标。],
    [Kepler（CNCF）], [Kubernetes 节点与容器级 CPU 能耗], [基于 RAPL 与模型，能把能耗归属到容器/Pod；GPU 部分建议仍用 DCGM。],
    [node_exporter], [Linux 主机], [`node_rapl_*` 指标提供 RAPL 能量计数器（需裸金属与权限）。],
    [ipmi_exporter / Redfish], [整机功率], [反映包括风扇、电源损耗在内的服务器总功率，采样周期较长（秒级）。],
    [Zeus], [Python 程序内按代码段测量能耗], [适合在训练或推理代码中测量单个任务的能耗 @you2023zeus。],
  ),
  caption: [功率采集方案],
)

=== 测量误差与采样

GPU 内置功率传感器存在误差与采样盲区：已有研究在 70 多种 GPU 上测得 nvidia-smi 的误差约为 ±5%，
且 A100、H100 上只有约 25% 的运行时间被采样 @yang2023parttime。不同来源的采样周期差别也很大：
DCGM 为 100 ms 以上，IPMI 为 1–5 s，机柜级电表约 2 s @patel2024characterizing。因此：

- 计算一段时间的能耗时，优先使用硬件累计能耗计数器（NVML 的 `nvmlDeviceGetTotalEnergyConsumption`、RAPL 的 `energy_uj`），
  而不是对功率读数做积分；平台的 `EnergyIntegrator` 对有计数器的设备直接使用计数器，并处理计数器下降（驱动重载）。
- 短请求（亚秒级）的单请求能耗无法直接测准，应以"一段时间内的总能耗 ÷ 期间处理的 token 数"计算平均值（见 @sec-accounting）。
- 结果中应注明功率来源（模型或传感器类型）与采样周期；服务器功率指标的 `source` 标签记录功率来源。

== 服务器层：真实服务器的功率归属

站点 `measurement` 给出传感器列表与主机部分的确定方式，站点代理按以下规则把读数归属到真实服务器（`cpc.site.power.SensorPower`）：

1. 加速卡（gpu、npu、soc）的读数按服务器声明的 `devices`（或 `gpu_indices`）归属；未声明加速卡的服务器
   （只用 CPU 的推理实例）只有主机部分；
2. 主机部分（CPU、内存、主板、风扇等）按 `measurement.host` 确定：`server` 为整机读数（`ipmi`，或以 `command` 接入的 PDU 等）减去本机全部加速卡读数；
   `cpu_dram` 为 CPU 与内存读数（RAPL 等）之和，不含主板、风扇与电源损耗；`excluded` 表示不计主机部分，
   服务器 IT 功率只含加速卡，状态摘要的 `power_scope` 为 `accelerators`；
3. 主机部分在未关机的真实服务器之间平均分摊（全部关机时在全部真实服务器之间分摊）；
4. 任一组成部分的读数缺失时，相应服务器的功率为 `None`，缺失项（如 `device:gpu0`、`host`）记入 `power_missing`；
   站点 IT 功率在任一服务器缺失时为 `None`。平台不以服务器模型或其他传感器替代缺失的读数。

```yaml
measurement:
  sensors: [{type: nvml}, {type: ipmi, name: bmc}]   # 同类型的多个传感器以 name 区分
  host: server                                       # server / cpu_dram / excluded
```

== 设施层：PUE

设施功率 = IT 功率 × PUE，PUE 按站点在场景中给定。"东数西算"工程对东部、西部枢纽数据中心 PUE 的目标分别为不超过 1.25 和 1.2，
《数据中心绿色低碳发展专项行动计划》要求到 2025 年底国家枢纽节点数据中心 PUE 不高于 1.2 @ndrc2024dc。
有冷却与配电功率的实测数据时，可通过 `command` 传感器或自行注册的传感器接入。

== 标准化指标 <sec-metrics>

各服务以 Prometheus 格式暴露指标，主要指标如下；完整列表见各服务的 `/metrics`。

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("指标", "含义"),
    [`DCGM_FI_DEV_POWER_USAGE`], [真实服务器的 GPU 功率（W），与 dcgm-exporter 同名；标签 `gpu`、`modelName`、`Hostname`、`site`],
    [`DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION`], [GPU 累计能耗（mJ）],
    [`cpc_device_power_watts`], [通用导出器：任意设备功率；标签 `sensor`、`device`、`kind`、`vendor`、`host`],
    [`cpc_device_energy_joules_total`], [通用导出器：任意设备累计能耗（J）],
    [`cpc_node_it_power_watts`], [服务器 IT 功率；标签 `pool`、`source`（model / sensors）；读数缺失时为 NaN],
    [`cpc_node_power_valid`、`cpc_site_energy_coverage_ratio`], [服务器功率读数是否完整；站点功率有效时长占比],
    [`cpc_sensor_up`、`cpc_sensor_errors_total`], [传感器最近一次读取是否成功且无字段错误；读取失败次数],
    [`cpc_node_state`], [服务器状态（0 关机 / 1 启动中 / 2 在线 / 3 排空）],
    [`cpc_site_it_power_watts`、`cpc_site_facility_power_watts`], [站点 IT 功率、设施功率],
    [`cpc_site_nodes_on`、`cpc_site_queue_length`], [在线服务器数、排队请求数],
    [`cpc_grid_price_per_kwh`、`cpc_grid_carbon_intensity_kg_per_kwh`], [电价（标签 `currency`）、碳强度],
    [`cpc_grid_extra_signal`], [扩展信号（标签 `signal`）],
    [`cpc_grid_power_cap_kw`、`cpc_grid_dr_event_active`], [设施功率上限（无上限为 −1）、需求响应是否生效],
    [`cpc_gateway_ttft_seconds`、`cpc_gateway_e2e_seconds`], [首 token 时延、端到端时延直方图（按来源与站点）],
    [`cpc_gateway_slo_total`], [SLO 达成计数（`result` = met / violated）],
  ),
  caption: [主要指标],
)

Prometheus 记录规则（`deploy/prometheus/rules/cpc-energy.yml`）把它们汇总为标准化指标，命名遵循 Prometheus 的
`level:metric:operations` 约定。调度器、仪表盘和实验分析只依赖这些标准化指标，与具体采集器解耦：

#figure(
  tbl(
    columns: (18em, 1fr),
    header: ("记录规则", "计算"),
    [`site:gpu_power_watts:sum`], [按站点汇总 GPU 功率],
    [`site:facility_power_watts:sum`], [按站点汇总设施功率],
    [`site:energy_cost_per_hour`], [设施功率（kW）× 电价，即用电成本速率（电价的货币单位 / 小时）],
    [`site:carbon_kg_per_hour`], [设施功率（kW）× 碳强度],
    [`site:power_cap_headroom_watts`], [功率上限 − 设施功率（需求响应余量）],
    [`site:joules_per_output_token:rate1m`], [设施功率 ÷ 输出 token 速率，即每输出 token 的设施能耗],
    [`host_kind:device_power_watts:sum`], [通用导出器：按主机与设备类别汇总],
    [`global:slo_attainment:ratio5m`], [全局 SLO 达成率（5 分钟窗口）],
  ),
  caption: [标准化指标（记录规则）],
)

== 能耗核算 <sec-accounting>

- *站点能耗*：数据中心运行时按服务器积分 IT 能耗（@ch-runtime），功率缺失区间不积分并计入缺失时长；
  实验中由 `cpc_node_energy_joules_total` 与 `cpc_node_energy_missing_seconds_total` 导出，站点状态摘要的 `energy_coverage`
  为功率有效时长占比。传感器层的 `EnergyIntegrator` 对有累计能耗计数器的设备直接使用计数器，并处理计数器复位。
- *成本与碳排放*：仿真器按时间片（缺省 5 分钟）累计 IT 能耗，乘以 PUE 得设施能耗，再乘以时间片中点的电价与碳强度；
  `cpc live` 按采样区间取站点累计能耗的差分，乘以 PUE 与区间起点的电价与碳强度。信号缺失的区间不核算成本与碳排放，
  汇总中的 `signal_coverage` 给出有信号的设施电量占比。Prometheus 记录规则另给出成本速率与碳排放速率。
- *单请求能耗*：短请求的能耗无法单独测准，平台不给出逐请求的能耗；每请求或每 token 能耗以一段时间的站点能耗
  除以期间完成的请求数或输出的 token 数计算（结果汇总中的 `wh_per_request`）。
