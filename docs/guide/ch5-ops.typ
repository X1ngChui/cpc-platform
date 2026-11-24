#import "../template/cpc-doc.typ": *

= 观测与运维 <g-ops>

Prometheus 指标与记录规则的定义见《平台设计说明书》"能耗测量与核算"一章；Compose 与 Kubernetes 部署包含预置的 Prometheus 与 Grafana。

== 功率传感器

```bash
# 全部传感器类型、依赖、本机是否具备前提条件
cpc sensors --list
cpc sensors --types nvml                            # 读取指定传感器，连续读 3 次
cpc sensors --types nvml,rapl --count 5 --interval 2
cpc sensors --types nvml --json                     # JSON 输出，便于脚本处理
```

平台不自动选择传感器：读取、导出与站点代理使用的传感器都须显式指定。传感器构造失败（驱动或库不可用、
必需能力缺失）时命令以状态码 2 退出并给出原因；读取中某个字段失败时，该值显示为 `-` 并列出错误。

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
  # 密码放在环境变量 IPMI_PASSWORD 中
  - {type: ipmi, host: 10.0.0.21, user: admin}
  # 自签名证书时显式关闭校验
  - {type: redfish, url: "https://10.0.0.21", username: admin, verify: false}
  - {type: prometheus, url: "http://prom:9090", kind: gpu, device_label: gpu,
     query: "DCGM_FI_DEV_POWER_USAGE{Hostname='node-1'}"}
  # 任意命令行工具 + 正则（以所装版本的实际输出为准）
  - {type: command, kind: npu, vendor: huawei,
     cmd: ["npu-smi", "info", "-t", "power", "-i", "0"],
     regex: "Power Dissipation\\(W\\)\\s*:\\s*([0-9.]+)"}
  - {type: http-json, url: "http://pdu-1/api/status", path: "outlets[3].power",
     kind: server}
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
docker run -d --gpus all -p 9400:9400 ghcr.io/x1ngchui/cpc-platform:0.2.0 \
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
    [`cpc up`], [本机单进程启动全部服务（`--speedup`、`--routing`、`--capacity`、`--interval`、`--no-controller`）],
    [`cpc grid` / `site` / `gateway` / `controller`], [单独启动某个服务（`-s` 场景、`-e` 地址文件或 `--local`、`--host`/`--port` 覆盖监听地址）],
    [`cpc loadgen`], [负载生成（`--duration`、`--start-s`、`--duration-s`、`--time-scale`、`--rate-scale`、`--no-stream`、`--model`、`--out`）],
    [`cpc sim`], [离散事件仿真（`--routing`、`--capacity` 及其参数、`--hours`、`--bin-s`、负载变换参数、`--out`）],
    [`cpc run`], [按实验描述运行多个变体（`--jobs`、`--out`）],
    [`cpc compare`], [并行仿真多组策略（`--runs "路由:容量,..."`、负载变换参数）],
    [`cpc data`], [数据集：`kinds`、`validate`、`info`、`summary`、`fetch`、`calibrate`],
    [`cpc report`], [仿真结果作图],
    [`cpc signals` / `cpc rtt`], [打印电网信号 / RTT 矩阵],
    [`cpc event`], [注入需求响应事件（`--site`、`--cap-kw`、`--duration-min`）],
    [`cpc sensors` / `cpc exporter`], [列出或读取功率传感器（`--list`、`--types`、`--spec`）/ 通用功率导出器],
    [`python scripts/smoke_test.py`], [端到端冒烟测试（`--external -e 地址文件` 检查已部署环境）],
    [`python docs/build.py`], [编译文档 PDF],
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
    [`HF_TOKEN`], [Hugging Face 访问令牌（下载受控访问的原始数据或资源）],
    [`CPC_LOG_FORMAT`], [日志格式：`text`（默认）或 `json`（结构化日志）],
    [`CPC_ADMIN_TOKEN`], [管理接口令牌；服务端校验、控制器与命令行工具调用时自动携带],
    [`CPC_LOG_LEVEL`], [日志级别，默认 INFO],
    [`IPMI_PASSWORD` / `REDFISH_PASSWORD`], [远程 BMC 密码],
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

#qa[SLO 达成率偏低？][常见原因：容量策略缩容过快、服务器启动时间（`boot_s`）相对信号时钟加速过长、负载超过总容量。
演示时信号加速 60 倍，`reactive` 策略的扩容来不及跟上快速变化的日内负载是预期现象；正式实验应使用真实时间。]

#qa[NVML 不可用（`pynvml` 导入失败或 `NVML Shared Library Not Found`）？][安装 `pip install nvidia-ml-py`；Linux 容器中需要
NVIDIA Container Toolkit 并以 `--gpus all` 启动；WSL2 中 `libnvidia-ml.so` 由宿主机驱动经 `/usr/lib/wsl/lib` 提供。
可先用 `nvidia-smi` 验证驱动。]

#qa[RAPL 读不到？][`/sys/class/powercap/intel-rapl:*/energy_uj` 在多数发行版上仅 root 可读，且大多数云虚拟机不提供 RAPL。
在裸金属上以 root 运行，或用 `ipmi` / `redfish` 获取整机功率。]

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

#qa[提示数据集缺失或不符合规范？][按 @g-data 转换或下载数据集，并以 `cpc data validate <目录>` 检查；
错误信息给出不符合的字段、约束与取值。]
