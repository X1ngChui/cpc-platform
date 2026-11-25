#import "../../template/cpc-doc.typ": *

= 观测与运维 <g-ops>

各服务以 Prometheus 格式在 `/metrics` 提供指标；Compose 与 Kubernetes 部署包含预置的 Prometheus 与 Grafana 仪表盘。
指标与记录规则的完整定义见《平台设计说明书》"能耗测量与核算"一章。

== 常用指标

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("指标", "含义"),
    [`cpc_site_it_power_watts`、`cpc_site_facility_power_watts`], [站点 IT 功率与设施功率（W）],
    [`cpc_site_nodes_on`、`cpc_site_queue_length`], [站点在线服务器数、排队请求数],
    [`cpc_node_it_power_watts`], [服务器 IT 功率（标签 `site`、`pool`、`node`、`source`：model 为服务器模型，sensors 为实测）],
    [`cpc_node_energy_joules_total`], [服务器累计 IT 能耗（J）],
    [`DCGM_FI_DEV_POWER_USAGE`], [真实服务器的 GPU 功率（W），与 NVIDIA dcgm-exporter 同名],
    [`cpc_grid_price_per_kwh`、`cpc_grid_carbon_intensity_kg_per_kwh`], [各站点当前的电价与碳强度],
    [`cpc_gateway_ttft_seconds`、`cpc_gateway_e2e_seconds`], [首 token 时延与端到端时延的分布（直方图）],
    [`cpc_gateway_route_decisions_total`], [路由决策计数（标签 `policy`、`reason`）],
    [`cpc_sensor_up`、`cpc_upstream_up`], [传感器、上游服务是否正常（1 正常，0 故障）],
  ),
  caption: [常用指标],
)

在 Prometheus 页面中查询的示例：

```text
site:facility_power_watts:sum                       各站点设施功率
sum(site:energy_cost_per_hour)                      全局用电成本速率（每小时）
sum(site:carbon_kg_per_hour)                        全局碳排放速率（kg / 小时）
increase(cpc_node_energy_joules_total[10m]) / 3.6e6 各服务器最近 10 分钟的 IT 用电（kWh）
histogram_quantile(0.99, sum by (le, site) (rate(cpc_gateway_ttft_seconds_bucket[5m])))
```

== 功率传感器 <g-sensors>

```bash
# 全部传感器类型、依赖、本机是否具备前提条件
cpc sensors --list
cpc sensors --types nvml                            # 读取指定传感器，连续读 3 次
cpc sensors --types nvml,rapl --count 5 --interval 2
cpc sensors --types nvml --json                     # JSON 输出，便于脚本处理
```

平台不自动选择传感器：读取、导出与站点代理使用的传感器都须显式指定。传感器构造失败（驱动或库不可用、
必需能力缺失）时命令以状态码 2 退出并给出原因；读取中某个字段失败时，该值显示为 `-` 并列出错误。

平台内置四种传感器，每种测量一类对象，互不重叠：

#figure(
  tbl(
    columns: (6em, 11em, 1fr),
    header: ("类型", "测量对象", "前提条件与参数"),
    [`nvml`], [NVIDIA GPU], [NVIDIA 驱动与 `nvidia-ml-py`（`pip install -e ".[nvml]"`）；`gpus` 选择 GPU 序号（缺省全部）；
      支持功率封顶],
    [`rapl`], [CPU 封装与内存], [裸金属 Linux 的 powercap（多数云虚拟机不提供）；读取需要 root；
      容器内的挂载方法见下文常见问题；`root` 指定 powercap 目录],
    [`ipmi`], [服务器整机], [本机 BMC（`/dev/ipmi0`）与 `ipmitool`；容器内映射 `/dev/ipmi0`；部分 BMC 偶发异常读数，宜设置 `max_w`],
    [`command`], [其他任意设备], [运行命令并用正则提取功率（W）：`cmd`、`regex`、`kind`（读数种类）、`device`、`scale`],
  ),
  caption: [内置传感器],
)

所有传感器接受通用参数 `name`（同类型多个实例时区分）与 `max_w`（功率读数的合理上限，超过即记为缺失）。
没有内置传感器的设备（其他厂商的加速卡、智能 PDU 等），可用 `command` 调用厂商的命令行工具，或按 @g-policy 中注册策略的方法，
以 entry point 组 `cpc.sensors` 注册自己的传感器类（继承 `cpc.sensors.Sensor`，实现 `read()`）。WSL2 中只有 `nvml` 可用。

== 用配置文件指定传感器

需要参数的传感器用 YAML 描述：

```yaml
# sensors.yaml
sensors:
  - {type: nvml, gpus: [0]}
  - {type: rapl}
  - {type: ipmi, max_w: 2000}                 # 超过 2000 W 的读数记为缺失
  # 其他设备：命令行工具 + 正则（以所装版本的实际输出为准）
  - {type: command, kind: npu, vendor: huawei,
     cmd: ["npu-smi", "info", "-t", "power", "-i", "0"],
     regex: "Power Dissipation\\(W\\)\\s*:\\s*([0-9.]+)"}
```

```bash
cpc sensors --spec sensors.yaml
```

同样的列表写在场景文件中站点的 `measurement.sensors` 中，供站点代理使用，并用 `measurement.host` 指定主机部分功率的确定方式
（`server`：整机读数减加速卡；`cpu_dram`：CPU 与内存读数之和；`excluded`：不计主机部分）。

== 部署通用导出器

在任意需要监测的机器上运行通用导出器，Prometheus 抓取 `:9400/metrics`：

```bash
cpc exporter --port 9400 --types nvml --site gz   # 按类型
cpc exporter --port 9400 --spec sensors.yaml       # 按配置
docker run -d --gpus all -p 9400:9400 ghcr.io/x1ngchui/cpc-platform:0.3.0 \
       exporter --types nvml --site gz
```

导出器启动时逐一读取指定的传感器，任一失败即退出。导出的指标：`cpc_device_power_watts`、`cpc_device_energy_joules_total`、
`cpc_device_utilization_percent`（读数缺失的字段不输出样本）、`cpc_sensor_up`、`cpc_sensor_errors_total`；
NVIDIA GPU 另有 `DCGM_FI_DEV_POWER_USAGE` 等 DCGM 兼容指标。记录规则 `host_kind:device_power_watts:sum` 按主机与设备类别汇总。

== 健康状态与故障排查 <g-health>

=== 健康与就绪接口

各服务提供 `/healthz`（存活）与 `/readyz`（就绪，未就绪时返回 503 并给出原因）。故障被限制在其所属的读数或组件之内，
并以以下方式报告：

#figure(
  tbl(
    columns: (11em, 1fr),
    header: ("位置", "内容"),
    [站点 `/state`], [服务器的 `power_missing`（缺失的功率组成部分）与 `last_error`（开关机或封顶失败）；`components.sensors`（各传感器的健康状态、最近错误、探测到的能力）；`energy_coverage`（功率有效时长占比）],
    [网关 `/state`、`/readyz`], [`upstreams`：电网信号服务与各站点状态拉取的健康状态；路由视图中可用的站点],
    [控制器 `/state`], [`upstreams`；最近一次周期的 `result`（`applied` / `skipped`）、跳过原因与排除的站点],
    [指标], [`cpc_sensor_up`、`cpc_sensor_errors_total`、`cpc_upstream_up{component,upstream}`、`cpc_upstream_errors_total`、`cpc_gateway_attempts_total{site,outcome}`、`cpc_gateway_route_decisions_total{policy,reason}`、`cpc_controller_cycles_total{result,reason}`、`cpc_controller_collect_errors_total{target}`、`cpc_node_actuation_errors_total`],
    [日志], [状态变化（`sensor_down`、`upstream_up` 等）、跳过的控制周期与执行失败；`CPC_LOG_FORMAT=json` 时为每行一个 JSON 对象，含 `event` 字段],
  ),
  caption: [故障的显式报告],
)

=== 启动失败

配置或环境错误在启动时报告，命令以状态码 2（服务进程由 uvicorn 报告启动失败时为 3）退出，常见原因：

- 场景文件含未知字段或引用关系错误；数据集不符合规范、文件缺失或时间窗超出覆盖范围；
- 未给出地址文件：除 `cpc up` 外，访问其他服务的命令须用 `-e` / `CPC_ENDPOINTS` 或 `--local`；
- 有真实服务器的站点未声明 `measurement`，或声明的传感器不可用、未覆盖服务器占用的加速卡；
- 网关、控制器或负载生成器在期限内（`--startup-timeout`，默认 30 s）无法从电网信号服务同步信号时钟。
  Docker Compose 与 Kubernetes 会按重启策略重新启动这些服务，电网信号服务就绪后即恢复。

== 命令与环境变量 <g-ref>

=== 命令

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("命令", "作用"),
    [`cpc check`], [检查场景与数据，打印服务器类型、站点、往返时延、电网信号与负载概要（`-s`、`--date`）],
    [`cpc data`], [数据集：`kinds`（字段定义）、`check`（检查并统计）、`show`（描述文件摘要）],
    [`cpc measure`], [测量站点中一台真实服务器各级并发下的功率与 token 间隔，写出 power_profile 数据集（`--site`、`--boot`）],
    [`cpc compare`], [并列比较两个或多个结果目录（仿真或实验）的总体与各站点指标；两个目录时给出比值],
    [`cpc profile`], [由功率特性数据集生成服务器类型（`--where`、`--slots`、`--idle-w`、`--host-w`）],
    [`cpc sim`], [运行一次仿真（`--hours`、`--routing`、`--capacity` 及其参数、负载变换参数、`--out`）],
    [`cpc run`], [按实验描述运行多个仿真变体（`--jobs`、`--out`）],
    [`cpc report`], [由结果目录作图],
    [`cpc live`], [运行一次实验并写出结果目录（`--duration`、`--speedup`、`-e` 连接已部署的服务）],
    [`cpc up`], [本机单进程启动全部服务，直到 Ctrl+C（`--speedup`、`--routing`、`--capacity`、`--interval`、`--no-controller`）],
    [`cpc loadgen`], [向网关回放负载（`--local` 或 `-e`、`--duration`、负载变换参数、`--model`、`--out`）],
    [`cpc event`], [注入需求响应事件（`--site`、`--cap-kw`、`--duration-min`）],
    [`cpc grid` / `site` / `gateway` / `controller`], [单独启动某个服务（`-e` 地址文件或 `--local`；`--host`、`--port` 覆盖监听地址；
      `cpc site --control <电网信号服务地址>` 从控制节点取得场景与地址）],
    [`cpc sensors` / `cpc exporter`], [列出或读取功率传感器（`--list`、`--types`、`--spec`）/ 通用功率导出器],
    [`python scripts/smoke_test.py`], [端到端冒烟测试（`--offline` 使用测试数据；`--external -e 地址文件` 检查已部署的环境）],
  ),
  caption: [命令速查],
)

=== 环境变量

#figure(
  tbl(
    columns: (12em, 1fr),
    header: ("变量", "作用"),
    [`CPC_ENDPOINTS`], [地址文件路径（等价于 `-e`）],
    [`CPC_DATA_DIR`], [数据集目录，缺省 `~/.cache/cpc/datasets`],
    [`HF_TOKEN`], [Hugging Face 访问令牌（转换范例下载受控访问的原始数据）],
    [`CPC_LOG_FORMAT`], [日志格式：`text`（默认）或 `json`（结构化日志）],
    [`CPC_ADMIN_TOKEN`], [管理接口令牌；服务端校验、控制器与命令行工具调用时自动携带],
    [`CPC_LOG_LEVEL`], [日志级别，默认 INFO],
  ),
  caption: [环境变量],
)

== 常见问题 <g-faq>

#let qa(q, a) = block(below: 1em, { set par(justify: false, first-line-indent: 0em); [#strong(q) \ #a] })

#qa[端口被占用怎么办？][`cpc up` 默认使用 9000、8080、9300、9101 起的端口。可准备一个地址文件指定其他端口，用 `-e` 传入；
Compose 部署修改 `docker-compose.yml` 中的端口映射。占用端口的进程可用 `ss -ltnp | grep :8080` 查看。]

#qa[请求很慢或连接失败，且机器上设置了 HTTP 代理？][平台内部调用已不读取代理环境变量；但 `curl` 等工具仍会走代理，
访问本机服务时加 `--noproxy '*'`。Docker 容器与 kind 节点继承的 `127.0.0.1` 代理地址在容器内不可达，见 @g-kind 的说明。]

#qa[Compose 启动后 Grafana 没有数据？][确认 Prometheus 的 Targets 页面（`http://localhost:9090/targets`）各目标为 UP；
仪表盘的时间范围默认最近 30 分钟，需先运行负载生成器。]

#qa[SLO 达成率偏低？][常见原因：容量策略缩容过快、服务器启动时间（`boot_s`）较长、负载超过总容量。
`cpc check` 给出各站点的并发槽数，可与负载的平均到达率 × 平均服务时间比较；仿真结果的 `route_reasons` 中的 `overflow_*`
与 `rejected` 表示容量不足。]

#qa[NVML 不可用（`pynvml` 导入失败或 `NVML Shared Library Not Found`）？][安装 `pip install nvidia-ml-py`；Linux 容器中需要
NVIDIA Container Toolkit 并以 `--gpus all` 启动；WSL2 中 `libnvidia-ml.so` 由宿主机驱动经 `/usr/lib/wsl/lib` 提供。
可先用 `nvidia-smi` 验证驱动。]

#qa[功率读数不随负载变化？][部分服务器的 BMC 读数长期不更新（DCMI 与 SDR 读数在加负载前后完全相同）。
接入真实服务器前，在空载与满负载时各运行一次 `cpc sensors --types ipmi,rapl --count 5 --interval 2`，确认读数随负载变化；
BMC 读数不变时只用 RAPL（`measurement.host: cpu_dram`）。`cpc measure` 在各级功率几乎相同时给出警告。]

#qa[整机功率偶尔出现极大的读数（如 16462 W）？][部分服务器的 BMC 会偶发返回异常的 DCMI 读数。给传感器设置合理的
功率上限，如 `{type: ipmi, max_w: 2000}`：超过上限的读数记为缺失，不计入能耗，并在健康状态与 `cpc_sensor_errors_total`
中体现；结果中的 `energy_coverage` 相应小于 1。`max_w` 是所有传感器的通用参数。]

#qa[容器内 RAPL 报"没有 package / dram / psys 域"？][Docker 默认以空目录屏蔽容器内的 `/sys/devices/virtual/powercap`。
把宿主机的该目录挂载到其他路径（`-v /sys/devices/virtual/powercap:/host/powercap:ro`），传感器配置为
`{type: rapl, root: /host/powercap}`。]

#qa[RAPL 读不到？][`/sys/class/powercap/intel-rapl:*/energy_uj` 在多数发行版上仅 root 可读，且大多数云虚拟机不提供 RAPL。
在裸金属上以 root 运行，或用 `ipmi` 获取整机功率。]

#qa[vLLM 启动失败或显存不足？][减小 `MAX_MODEL_LEN`、降低 `--gpu-memory-utilization`、换更小的模型；查看 `docker compose logs vllm-0`。
新架构 GPU 需要较新的 vLLM 版本。]

#qa[模型下载慢？][在宿主机用 `huggingface-cli download` 预先下载并挂载缓存目录，容器设置 `HF_HUB_OFFLINE=1`；
或使用 `HF_ENDPOINT=https://hf-mirror.com` / `VLLM_USE_MODELSCOPE=True`。]

#qa[管理接口返回 401？][服务端设置了 `CPC_ADMIN_TOKEN`。调用时加 `-H "Authorization: Bearer <令牌>"`；
控制器与 `cpc event` 会自动读取同名环境变量。]

#qa[kind 部署卡在等待就绪？][`kubectl --context kind-cpc -n cpc get pods` 查看状态；`ErrImagePull` 多为镜像未加载，重新运行
`python deploy/k8s/kind_up.py --skip-build`；`describe pod` 查看事件。]

#qa[仿真结果每次都一样吗？][数据集固定版本，到达率缩放与来源区域分配使用场景中的 `workload.seed`，
相同场景、数据集与策略的仿真结果相同；改变种子可评估随机性的影响。结果目录中的 `run.yaml` 记录了全部输入。]

#qa[提示数据集缺失或不符合规范？][按 @g-data 准备数据集，并以 `cpc data check <目录>` 检查；
错误信息给出不符合的字段、约束与取值。`cpc check -s <场景>` 一次检查场景引用的全部数据集。]

#qa[实验中大量请求失败或超时？][到达率超出本机的处理能力时会出现这种情况，用 `--rate-scale` 降低到达率；
也可能是推理实例未就绪或模型名不一致（`--model` 须与推理实例的模型名相同）。结果中 `failed` 的请求在 `requests.parquet`
中可以按 `site` 与 `server` 查看分布。]

#qa[实验结果的电费为 0 或 `signal_coverage` 小于 1？][采样时电网信号不可用：检查 `clock.start` 与 `--speedup` 下的信号时间
是否超出电网信号数据的覆盖范围（`http://localhost:9000/v1/signals` 中的 `missing` 给出原因）。]
