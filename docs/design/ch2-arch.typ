#import "../template/cpc-doc.typ": *
#import "../figures/diagrams.typ": arch, loops

= 总体设计 <ch-arch>

== 设计原则

+ *标准接口优先。* 平台自研的部分只有调度逻辑与少量胶水代码；推理接口采用 OpenAI API（vLLM、SGLang 等主流推理框架均提供），
  指标采用 Prometheus 暴露格式，GPU 指标沿用 NVIDIA dcgm-exporter 的字段名，部署采用 Docker 镜像与 Kubernetes 清单。
  这样真实组件（vLLM、dcgm-exporter、Kepler 等）可以直接替换平台中的仿真组件。
+ *仿真与真实共用接口。* 仿真站点与真实站点对外提供完全相同的接口；离散事件仿真器调用与在线平台相同的策略对象。
  因此一个策略可以先在仿真中快速比较，再不改代码地放到真实系统中运行。
+ *数据面与控制面分离。* 请求经网关到站点（数据面，毫秒级）；容量控制器周期性地调整站点容量（控制面，秒到分钟级）。
  二者通过状态快照与路由权重松耦合，任一方失效不会阻断另一方。
+ *策略可插拔。* 路由策略与容量策略都是带注册装饰器的 Python 类；外部模块通过环境变量 `CPC_PLUGINS` 加载，
  平台主体无需修改。
+ *配置与部署分离。* "场景文件"描述实验世界（站点、来源区域、节点参数、电网数据、负载、策略），
  "地址文件"描述服务部署在哪里。同一场景可在不同部署方式之间复用。

== 系统组成

#figure(arch, caption: [平台总体架构])

平台由以下服务组成，全部由同一个 Python 包 `cpc` 实现、打包为同一个容器镜像，以不同子命令启动：

#figure(
  tbl(
    columns: (8em, 6em, 1fr),
    header: ("服务", "启动命令", "职责"),
    [电网信号服务], [`cpc grid`], [按信号时钟提供各站点的分时电价、碳排放因子、功率上限（需求响应）；是信号时钟的权威来源；可在运行时注入需求响应事件。],
    [站点代理], [`cpc site`], [每个站点一个。管理节点池（开关机状态机、并发槽、排队）；执行推理请求（仿真或转发给真实推理实例）；采集功率（模型或传感器）并积分能耗；执行容量指令。],
    [路由网关], [`cpc gateway`], [统一入口；按路由策略选择站点；注入广域网时延；过载重试；记录请求日志与时延、SLO 指标。],
    [容量控制器], [`cpc controller`], [周期性采集站点状态、电网信号与需求统计；调用容量策略；向站点下发容量指令、向网关下发路由权重；记录决策日志。],
    [负载生成器], [`cpc loadgen`], [按日内波动的非齐次泊松过程或 Azure LLM trace 发送请求；统计 TTFT、端到端时延与 SLO 达成率。],
    [通用功率导出器], [`cpc exporter`], [在任意服务器上自动探测功率传感器，以统一指标暴露给 Prometheus（@ch-energy）。],
    [离散事件仿真器], [`cpc sim` / `cpc compare`], [不启动服务，以仿真时间快速运行若干小时的场景并输出能耗、成本、碳排放与时延统计。],
    [监控], [Prometheus、Grafana], [抓取各服务指标，用记录规则计算标准化能耗指标；仪表盘展示功率、电价、节点、时延等。],
  ),
  caption: [服务清单],
)

== 三种运行形态

#figure(
  tbl(
    columns: (7em, 1fr, 1fr, 1fr),
    header: ("", "仿真（sim）", "虚拟化（emulation）", "真实（real）"),
    [时间], [仿真时间，24 小时约 1 分钟跑完], [真实时间；电网信号可加速], [真实时间],
    [请求], [仿真器内部事件], [真实 HTTP 请求（负载生成器）], [真实 HTTP 请求],
    [节点], [仿真], [仿真（站点代理按模型延时）], [真实推理实例（vLLM 等）],
    [功率], [功率模型], [功率模型], [传感器实测（GPU/CPU/整机）+ 模型补齐],
    [广域网时延], [按 RTT 模型计入], [网关注入], [网关注入，或真实跨地域网络],
    [部署], [单进程], [单进程 / Compose / Kubernetes], [云服务器 + Compose / Kubernetes],
    [用途], [策略的大规模、长时间比较], [验证系统行为、联调、演示], [验证功率测量与调度动作的真实效果],
  ),
  caption: [三种运行形态对比],
)

三种形态可以混合：一个场景中可以既有真实站点（`backend: openai`）又有仿真站点（`backend: emulated`），
例如用 3 台租用的 GPU 服务器扮演 3 个站点，再用仿真站点补足更多地域。

== 关键数据通路

*请求通路。* 负载生成器（或用户）向网关发送 OpenAI 兼容请求，请求头 `x-cpc-source` 指明来源区域；
网关构造集群视图（各站点状态快照 + 电网信号 + 来源到站点的 RTT + 控制器下发的权重），调用路由策略得到目标站点；
等待 RTT/2 后转发；站点代理把请求放入并发槽或排队，在节点上执行（仿真延时或转发给 vLLM）；
响应（或流式输出的首个片段）返回网关后再等待 RTT/2 返回用户。

*状态通路。* 网关每秒拉取一次各站点 `/state` 快照；两次快照之间，网关自行累计已派发但尚未体现在快照中的请求数（pending），
避免同一时刻大量请求涌向同一站点。

*控制通路。* 控制器每个控制周期拉取站点快照、电网信号与网关的需求统计（各来源区域到达率），调用容量策略，
向各站点 `POST /admin/capacity`，并可向网关 `PUT /admin/weights`。

*观测通路。* Prometheus 抓取所有服务的 `/metrics`；记录规则把各种来源的功率指标汇总为按站点的标准化能耗指标（@sec-metrics）。

#figure(loops, caption: [数据面与控制面的两个时间尺度])

== 部署视图

#figure(
  tbl(
    columns: (9em, 1fr, 1fr),
    header: ("部署方式", "适用场景", "入口"),
    [本机单进程], [开发调试、课程演示；无需 Docker], [`cpc up`],
    [Docker Compose], [单机完整环境（含 Prometheus、Grafana）], [`deploy/compose/docker-compose.yml`],
    [Kubernetes（kind）], [熟悉 Kubernetes 原生部署；每个站点固定在带 region 标签的节点上], [`deploy/k8s/kind_up.py`],
    [云服务器], [接入真实 GPU：每台服务器一个站点栈，另有一个控制栈], [`deploy/cloud/`],
  ),
  caption: [部署方式],
)

Kubernetes 部署中，每个站点对应集群中带 `topology.kubernetes.io/region=<站点 id>` 标签的节点，站点代理通过
`nodeSelector` 固定在该节点上，这与真实多地域集群中用 region/zone 标签表示地理位置的惯例一致。

== 技术选型

#figure(
  tbl(
    columns: (8em, 9em, 1fr),
    header: ("方面", "选择", "理由"),
    [实现语言], [Python 3.10+], [学习门槛低；科研生态（NumPy、SciPy、matplotlib）完善；跨平台。],
    [Web 框架], [FastAPI + Uvicorn], [异步 I/O 适合大量并发的流式请求；自动生成 OpenAPI 文档（各服务 `/docs`）。],
    [HTTP 客户端], [httpx], [同时支持同步与异步、流式响应。],
    [数据校验], [Pydantic v2], [配置与接口数据结构的类型校验。],
    [指标], [prometheus-client], [Prometheus 官方 Python 客户端。],
    [推理后端], [vLLM（OpenAI 兼容）], [主流开源推理框架，支持连续批处理 @kwon2023vllm；可替换为任何 OpenAI 兼容服务。],
    [容器编排], [Docker Compose / Kubernetes], [事实标准；kind 可在单机上创建多节点 Kubernetes 集群。],
    [监控], [Prometheus + Grafana], [云原生监控的事实标准。],
    [文档], [Typst], [比 LaTeX 编译快、语法简单；通过 Python 包安装，无需单独的 TeX 发行版。],
  ),
  caption: [技术选型],
)

== 代码结构

```text
cpc-platform/
├── src/cpc/
│   ├── config.py        场景与地址配置（Pydantic 模型）
│   ├── models.py        功率模型、性能模型、广域网时延模型、信号时钟
│   ├── schemas.py       服务间数据结构（站点快照、容量指令、需求统计）
│   ├── grid/            电网信号（分时电价、排放因子、需求响应）与信号服务
│   ├── site/            站点代理：节点池状态机、执行器、功率来源、HTTP 服务
│   ├── sensors/         多硬件功率传感器、自动探测、通用导出器
│   ├── gateway/         路由网关
│   ├── controller/      容量控制器与执行器
│   ├── sched/           路由策略与容量策略（插件）
│   ├── sim/             离散事件仿真器与作图
│   ├── loadgen/         负载生成器
│   └── cli.py           命令行入口（cpc ...）
├── configs/             场景文件与地址文件
├── data/grid/           电价时段结构、省级排放因子（含出处）
├── deploy/              compose / k8s / cloud / prometheus / grafana
├── scripts/             冒烟测试、文档插图
├── tests/               单元测试（pytest）
└── docs/                文档源文件（Typst）与 PDF
```
