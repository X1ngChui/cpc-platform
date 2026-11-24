#import "../template/cpc-doc.typ": *
#import "../figures/diagrams.typ": sensing

= 能耗感知子系统 <ch-energy>

能耗感知是路由与容量管理的基础：调度需要知道"现在每个站点在用多少电、花多少钱、排多少碳，
以及电网允许它用多少电"。平台把能耗感知分为四层（@fig-sensing），每层有明确的数据来源与标准化输出。

#figure(sensing, caption: [能耗感知的四层结构]) <fig-sensing>

== 设备层：多硬件功率传感器

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
  ),
  caption: [功率读数结构],
)

新增一种硬件只需实现一个 `Sensor` 子类的 `read()` 方法并用 `@register_sensor` 注册；
站点代理、通用导出器、命令行工具都通过这一层获取功率，不关心具体硬件。

=== 已支持的传感器

#figure(
  tbl(
    columns: (6.5em, 10em, 1fr, 4.5em),
    header: ("类型", "硬件 / 平台", "接口", "状态"),
    [`nvml`], [NVIDIA GPU（数据中心卡与消费卡），Linux / Windows], [NVIDIA 官方 NVML 的 Python 绑定 nvidia-ml-py：功率、累计能耗计数器、利用率、功率上限设置], [已实测],
    [`nvidia-smi`], [同上，无 Python 绑定时的后备], [`nvidia-smi --query-gpu`], [已实测],
    [`amdsmi`], [AMD Instinct / Radeon，Linux], [ROCm 自带的 AMD SMI Python 库；兼容不同版本的字段名], [按文档],
    [`hwmon`], [AMD GPU（amdgpu）、Intel 独显（i915 / xe），Linux], [内核 hwmon sysfs：`power1_average`、`power1_input`（µW）、`energy1_input`（µJ）], [按文档],
    [`rapl`], [Intel / AMD CPU 封装、内存、平台，Linux], [内核 powercap sysfs（RAPL 能量计数器，处理回绕）], [按文档],
    [`ascend-dcmi`], [华为昇腾 NPU，Linux], [昇腾驱动的 DCMI 库（ctypes 调用 `dcmi_get_device_power_info`）], [按文档],
    [`ipmi`], [服务器整机（本机或远程 BMC）], [`ipmitool dcmi power reading`], [按文档],
    [`redfish`], [服务器整机（BMC）], [DMTF Redfish：`EnvironmentMetrics.PowerWatts` 或旧版 `Power.PowerControl`], [按文档],
    [`powermetrics`], [Apple Silicon，macOS], [`powermetrics`（需 root）输出的 CPU / GPU / ANE 功率], [按文档],
    [`lhm`], [Windows 上的 CPU / GPU], [LibreHardwareMonitor 的 WMI 接口], [按文档],
    [`prometheus`], [任意已有导出器], [PromQL 查询：dcgm-exporter、Kepler、node_exporter、ipmi_exporter、昇腾 NPU-Exporter 等], [按文档],
    [`command`], [任意厂商命令行工具], [子进程 + 正则（寒武纪、海光、摩尔线程、昆仑芯等国产加速卡的管理工具）], [通用],
    [`http-json`], [智能插座、PDU、自研采集板], [HTTP GET + 字段路径], [通用],
  ),
  caption: [已支持的功率传感器。"已实测"指已在 NVIDIA GeForce RTX 5070 Ti（Windows）上验证；"按文档"指依据官方接口文档实现、解析逻辑有单元测试，需在目标硬件上验证],
)

`cpc sensors --list` 列出全部传感器及其在本机是否可用；`cpc sensors` 自动探测并打印读数。
自动探测按"同一类设备优先使用更直接的接口"的顺序进行（例如有 NVML 时不再使用 nvidia-smi；
有 AMD SMI 时 hwmon 只用于 Intel 显卡），避免同一设备被重复计入。

=== 采集方案选型

在真实环境中，平台既可以由站点代理直接读取传感器，也可以复用已部署的标准导出器：

#figure(
  tbl(
    columns: (9em, 1fr, 1fr),
    header: ("方案", "适用", "说明"),
    [NVIDIA dcgm-exporter], [Kubernetes / Linux 上的数据中心 GPU], [NVIDIA GPU Operator 的组成部分，指标最全；消费级显卡支持有限。平台的 GPU 指标与其同名。],
    [平台通用导出器 `cpc exporter`], [任意服务器、开发机（含 Windows、macOS）], [依赖少，自动探测多种硬件；NVIDIA GPU 同时输出 DCGM 兼容指标。],
    [Kepler（CNCF）], [Kubernetes 节点与容器级 CPU 能耗], [基于 RAPL 与模型，能把能耗归属到容器/Pod；GPU 部分建议仍用 DCGM。],
    [node_exporter], [Linux 主机], [`node_rapl_*` 指标提供 RAPL 能量计数器（需裸金属与权限）。],
    [ipmi_exporter / Redfish], [整机功率], [反映包括风扇、电源损耗在内的服务器总功率，采样周期较长（秒级）。],
    [Zeus], [Python 程序内按代码段测量能耗], [适合在训练或推理代码中测量单个任务的能耗 @you2023zeus。],
  ),
  caption: [功率采集方案],
)

=== 测量注意事项

GPU 内置功率传感器存在误差与采样盲区：已有研究在 70 多种 GPU 上测得 nvidia-smi 的误差约为 ±5%，
且 A100、H100 上只有约 25% 的运行时间被采样 @yang2023parttime。不同来源的采样周期差别也很大：
DCGM 为 100 ms 以上，IPMI 为 1–5 s，机柜级电表约 2 s @patel2024characterizing。因此：

- 计算一段时间的能耗时，优先使用硬件累计能耗计数器（NVML 的 `nvmlDeviceGetTotalEnergyConsumption`、RAPL 的 `energy_uj`），
  而不是对功率读数做积分；平台的 `EnergyIntegrator` 对有计数器的设备直接使用计数器，并处理计数器下降（驱动重载）。
- 短请求（亚秒级）的单请求能耗无法直接测准，应以"一段时间内的总能耗 ÷ 期间处理的 token 数"计算平均值（见 @sec-accounting）。
- 报告实验结果时应注明功率来源（模型 / 传感器类型）与采样周期；站点指标中的 `source` 标签记录了这一信息。

== 服务器层：节点功率

=== 仿真后端：功率模型

仿真站点按如下模型计算节点功率（参数见 @tbl-params）。设节点占用率 $u = "busy" / "slots"$，功率封顶比例为 $c$：

$ P_"gpu" (u, c) = P_"idle" + (P_"cap" - P_"idle") dot u^alpha, quad P_"cap" = max(P_"idle", c dot P_"max") $

$ P_"host" (u) = H_"idle" + (H_"max" - H_"idle") dot u, quad P_"node" = P_"host" + n_"gpu" dot P_"gpu" $

关机节点功率为 `standby_w`（默认 0），启动中节点按空载计。指数 $alpha < 1$ 表示 GPU 功率随批大小凹增长：
大模型推理的解码阶段受显存带宽限制，增加并发对功率的边际影响递减 @patel2024characterizing；
按利用率线性换算功率往往不可靠，平台因此把 $alpha$ 作为需要标定的参数。

性能模型：预填充时间 $T_"pre" = L_"in" \/ (R_"pre" dot s(c))$，每输出 token 时间
$T_"pot" = d_0 (1 + k u) \/ s(c)$，其中 $s(c) = c^beta$ 为功率封顶后的相对速度，服务时间为 $T_"pre" + L_"out" dot T_"pot"$。

#figure(
  tbl(
    columns: (10em, 6em, 1fr),
    header: ("参数（`node_profiles`）", "默认值", "含义"),
    [`gpus`], [8], [每节点 GPU 数],
    [`gpu_idle_w` / `gpu_max_w`], [60 / 400 W], [单卡空载 / 满载功率（满载取 A100 SXM 的 TDP）],
    [`host_idle_w` / `host_max_w`], [800 / 1500 W], [主机部分空载 / 满载功率],
    [`power_exponent`（α）], [0.7], [GPU 动态功率随占用率增长的指数],
    [`slots`], [16], [并发序列数上限],
    [`prefill_tokens_per_s`], [20000], [预填充吞吐],
    [`decode_s_per_token`（$d_0$）], [0.025 s], [空载时单序列 TPOT],
    [`decode_slowdown`（k）], [0.6], [满批时 TPOT 增加比例],
    [`cap_min_ratio`], [0.5], [功率封顶比例下限],
    [`cap_perf_exponent`（β）], [0.4], [速度对封顶比例的敏感度],
    [`boot_s`], [60 s], [开机到可服务的时间],
  ),
  caption: [节点功率与性能模型参数（示意值）。按此默认值，单节点空载 1.28 kW、满载 4.7 kW，典型请求（800 输入 / 200 输出 token、半载）服务时间约 6.5 s],
) <tbl-params>

#warn[以上默认值只用于让仿真"量级合理"，不是任何具体设备的实测结果。接入真实服务器后，应按《部署与使用手册》中"标定节点功率模型"一节
用负载生成器扫描并发度、记录实测功率，拟合 $P_"idle"$、$P_"max"$、$alpha$ 等参数，再用于仿真站点。]

=== 真实后端：传感器归属

真实站点的功率来源为 `SensorPower`：

1. 加速卡（gpu / npu / soc）的读数按场景配置中每个推理实例的 `devices`（或 `gpu_indices`）归属到对应节点；
2. 若有整机读数（IPMI、Redfish、PDU），主机部分 = 整机功率 − 全部加速卡功率；否则若有 CPU / 内存读数（RAPL 等），
   主机部分 = 其和；都没有时用功率模型估计主机部分（节点 `power_source` 标为 `sensors+model`）；
3. 主机部分在本机在线节点之间平均分摊。

传感器列表可在场景文件的站点配置中给出，不给出时自动探测：

```yaml
sites:
  - id: gz
    backend: openai
    sensors: [{type: nvml}, {type: rapl}, {type: ipmi}]
    upstreams:
      - {id: gz-vllm0, url: "http://vllm-0:8000", gpu_indices: [0]}
```

== 设施层：PUE

设施功率 = IT 功率 × PUE。PUE 在场景文件中按站点给定。"东数西算"工程对东部、西部枢纽数据中心 PUE 的目标分别为
不超过 1.25 和 1.2；《数据中心绿色低碳发展专项行动计划》进一步要求到 2025 年底国家枢纽节点数据中心 PUE 不高于 1.2
@ndrc2024dc。默认场景中东部站点取 1.25、西部站点取 1.2。若有实测的冷却/配电功率，
可通过 `prometheus` 或 `http-json` 传感器接入，替代固定 PUE。

== 电网层：电价、碳排放与需求响应

=== 分时电价

`data/grid/tou_tariffs.yaml` 收录了四个省份工商业用户的分时电价时段与浮动比例，均取自省级发改委文件：

#figure(
  tbl(
    columns: (5em, 1fr, 9em, 1fr),
    header: ("省份", "依据", "浮动比例（相对平段）", "时段（夏季 7 月）"),
    [广东], [粤发改价格〔2021〕331号 @gd2021tou], [峰 1.7，谷 0.38；尖峰 = 峰 × 1.25], [峰 10–12、14–19；谷 0–8；尖峰（7–9 月）11–12、15–17],
    [贵州], [黔发改价格〔2023〕481号 @gz2023tou], [峰 1.6，谷 0.4], [峰 10–13、17–22；谷 0–8],
    [内蒙古（蒙西）], [内蒙古自治区发改委 2023-12-28 通知 @nm2023tou], [大风季 峰 1.68、谷 0.48；小风季 峰 1.54、谷 0.44；尖峰 = 峰 × 1.2，深谷 = 谷 × 0.8], [峰 6–8、18–22；谷 11–16；小风季尖峰 19–21、深谷 13–15],
    [上海], [沪发改价管〔2022〕50号 @sh2022tou], [夏、冬季 峰 1.8、谷 0.4；其他月份 峰 1.6、谷 0.5；尖峰 = 峰 × 1.25], [峰 8–15、18–21；谷 22–6；尖峰（7、8 月）12–14],
  ),
  caption: [收录的分时电价结构（两部制 / 一般工商业用户）],
)

需要注意三点简化：（1）平段电价 `flat_price` 是示意值，实际应按目标月份的电网企业代理购电价格表填写；
（2）模型把浮动比例作用于整个平段电价，而实际政策中政府性基金及附加、容量（需量）电价不参与浮动；
（3）2026 年起部分省份对直接参与市场交易的工商业用户取消了固定分时电价，价格随电力现货市场浮动。
研究市场价格时，可通过场景中的 `grid.traces` 提供逐时价格 CSV 覆盖分时电价。国家层面对分时电价机制的要求见 @ndrc2021tou。

=== 碳排放因子

`data/grid/carbon_factors_2023.csv` 收录了生态环境部、国家统计局发布的 2023 年 30 个省级电力平均二氧化碳排放因子
@mee2025factors（全国平均 0.5306 kgCO#sub[2]/kWh；默认场景涉及的四省：上海 0.5737、广东 0.4419、贵州 0.5683、内蒙古 0.6479）。
官方因子是年均值，不随时间变化。为了让碳信号具有日内变化以便演示，`carbon_profile: synthetic` 在年均值上叠加一个
日均为零的示意形状（午间光伏出力高时偏低），幅度由站点的 `carbon_amplitude` 控制；该形状不是实测数据，
正式研究应使用 `flat` 或通过 `grid.traces` 接入逐时数据。

=== 需求响应事件

需求响应事件抽象为"某站点在某时段内设施功率不得超过 `cap_kw`"，可在场景文件中预先登记，也可在运行时通过
`POST /v1/events` 注入（`cpc event` 命令）。电网信号中的 `power_cap_kw` 为并网容量上限与生效事件上限中的较小者。

=== 信号时钟

信号时间 = 场景起点 + 真实经过时间 × 加速倍数。真实实验取 1；演示时可取 60（真实 1 分钟 = 信号 1 小时），
24 小时的电价与碳强度变化在 24 分钟内走完。电网信号服务是时钟的权威来源，其他服务启动后从 `/v1/clock` 同步。
加速只影响信号随时间的变化速度，请求的服务时间、节点开机时间与能耗积分仍按真实时间计算。

#figure(image("../figures/signals.png", width: 92%), caption: [默认场景（2026 年 7 月 15 日）四个站点的电价与碳排放因子])

== 标准化指标 <sec-metrics>

各服务以 Prometheus 格式暴露指标，主要指标如下；完整列表见各服务的 `/metrics`。

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("指标", "含义"),
    [`DCGM_FI_DEV_POWER_USAGE`], [GPU 功率（W），与 dcgm-exporter 同名；标签 `gpu`、`modelName`、`Hostname`、`site`],
    [`DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION`], [GPU 累计能耗（mJ）],
    [`cpc_device_power_watts`], [通用导出器：任意设备功率；标签 `sensor`、`device`、`kind`、`vendor`、`host`],
    [`cpc_device_energy_joules_total`], [通用导出器：任意设备累计能耗（J）],
    [`cpc_node_it_power_watts`], [节点 IT 功率；标签 `source` 记录功率来源],
    [`cpc_node_state`], [节点状态（0 关机 / 1 启动中 / 2 在线 / 3 排空）],
    [`cpc_site_it_power_watts`、`cpc_site_facility_power_watts`], [站点 IT 功率、设施功率],
    [`cpc_site_nodes_on`、`cpc_site_queue_length`], [在线节点数、排队请求数],
    [`cpc_grid_price_yuan_per_kwh`、`cpc_grid_carbon_kg_per_kwh`], [电价、碳排放因子],
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
    [`site:energy_cost_yuan_per_hour`], [设施功率（kW）× 电价，即用电成本速率],
    [`site:carbon_kg_per_hour`], [设施功率（kW）× 碳排放因子],
    [`site:power_cap_headroom_watts`], [功率上限 − 设施功率（需求响应余量）],
    [`site:joules_per_output_token:rate1m`], [设施功率 ÷ 输出 token 速率，即每输出 token 的设施能耗],
    [`host_kind:device_power_watts:sum`], [通用导出器：按主机与设备类别汇总],
    [`global:slo_attainment:ratio5m`], [全局 SLO 达成率（5 分钟窗口）],
  ),
  caption: [标准化指标（记录规则）],
)

== 能耗核算 <sec-accounting>

- *站点能耗*：站点代理在每次功率变化时先按旧功率积分再重新采样（功率分段恒定），累计节点能耗 `cpc_node_energy_joules_total`。
- *成本与碳排放*：仿真器按 5 分钟时间片累计 IT 能耗，乘以 PUE 得设施能耗，再乘以时间片中点的电价与碳排放因子；
  在线平台由 Prometheus 记录规则给出成本速率与碳排放速率，对时间积分即得总量（Grafana 中可用 `increase` / `sum_over_time`）。
- *单请求能耗*：站点在非流式响应头 `x-cpc-energy-j` 中给出粗略估计（请求开始时节点功率按在途请求数均摊 × 服务时间 × PUE），
  仅供参考；严谨的核算应按"一段时间的站点能耗 ÷ 期间输出 token 数"计算每 token 能耗，再按请求的 token 数分摊。
