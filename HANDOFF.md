# 项目移交说明（v0.1.0 → v0.2.0）

## 0 启动提示词

在新会话（仓库根目录）中使用以下提示词：

> 阅读仓库根目录的 `CLAUDE.md` 与 `HANDOFF.md`，以及 `docs/design/` 中的设计说明书源文件，了解 cpc-platform 的现状与约定。
> 按 `HANDOFF.md` 第 4 节的任务顺序完成 v0.2.0 的重构：服务端收敛到 Linux、按"失败显式化与故障隔离"原则重构降级逻辑、
> 引入数据集抽象并接入公开数据集、为其余可替换部件建立抽象接口、修订文档语言。每项任务先给出设计方案（接口定义、
> 涉及文件、迁移影响）供确认，再实施；每项完成后运行 `pytest`、`python scripts/smoke_test.py`，更新 Typst 文档并编译 PDF。

## 1 当前状态（v0.1.0，2026-11-24）

| 项目 | 状态 |
|---|---|
| 代码 | `main` 分支，标签 `v0.1.0`；工作区无未提交内容 |
| 镜像 | `xingchui/cpc-platform:0.1.0`（Docker Hub）、`ghcr.io/x1ngchui/cpc-platform:0.1.0`，均含 amd64 / arm64 |
| CI | Linux / Windows / macOS 单元测试与冒烟测试、文档编译、镜像构建均通过 |
| 文档 | `docs/pdf/design.pdf`（设计说明书）、`docs/pdf/guide.pdf`（部署与使用手册） |

已验证：
- 单进程、Docker Compose、kind（1 控制面 + 4 个带 `topology.kubernetes.io/region` 标签的 worker）三种部署通过冒烟测试；
- 真实后端：NVIDIA RTX 5070 Ti + vLLM v0.34.0（Qwen2.5-0.5B-Instruct），请求转发与流式输出、`/health` 就绪检查、
  NVML 实测功率与能耗计数、通过 Docker API 启停推理容器；
- 发布镜像的 amd64、arm64 版本可运行，容器内 NVML 可读取 GPU 功率。

未在真实硬件上验证：AMD SMI、hwmon、RAPL、昇腾 DCMI、IPMI、Redfish、powermetrics、LibreHardwareMonitor 传感器（仅有解析逻辑的单元测试）。

已知局限与技术债：
- 节点状态机在 `site/core.py`（asyncio）与 `sim/des.py`（事件队列）中各实现一份；
- 网关为单实例，pending 计数仅在本实例内可见；
- 单请求能耗 `x-cpc-energy-j` 为粗略估计；
- 默认负载为合成的非齐次泊松过程，默认碳强度叠加了合成日内形状（见任务 T3）；
- 节点功率/性能模型参数与各省平段电价为示意值；蒙西小风季的峰谷时段沿用大风季划分，待核实；
- 停止推理容器后 GPU 仍有空载功耗，"关闭节点"在真实环境中不等于断电。

## 2 开发环境（WSL2）

1. 在 WSL 的 Linux 文件系统内克隆：`git clone https://github.com/X1ngChui/cpc-platform.git ~/cpc-platform`。
   仓库已包含全部源码、数据、部署清单、文档源文件、Typst 包缓存与 PDF，无需从 Windows 侧拷贝任何文件。
2. Python：`python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,nvml,docs]"`。
   文档编译需要中文字体：`sudo apt-get install -y fonts-noto-cjk`。
3. Git 身份与凭据在 WSL 中重新配置（`git config user.name/user.email`；凭据可复用 Windows 的 Git Credential Manager，或配置 SSH 密钥）。
   此前的提交使用 GitHub noreply 地址 `121439606+X1ngChui@users.noreply.github.com`。
4. Docker：启用 Docker Desktop 的 WSL Integration 即可在 WSL 中使用同一个 Docker 引擎；kind、kubectl 安装 Linux 版本。
5. GPU：WSL 通过 Windows 驱动提供 CUDA 与 NVML（`/usr/lib/wsl/lib`），`nvml` 传感器可用；DCGM、RAPL、hwmon 在 WSL 中不可用，
   这些传感器需在 Linux 物理机或云服务器上验证。
6. 代理：若 Windows 侧代理监听 `127.0.0.1`，WSL2 默认 NAT 网络下不可达，需改用宿主机地址或启用 mirrored 网络模式。
   kind 节点会继承代理环境变量；`deploy/k8s/kind_up.py` 已改为从宿主机加载全部镜像，节点不需要联网。

## 3 约束

见 `CLAUDE.md`，要点：服务端只面向 Linux；不叠加降级链，失败要么在启动时暴露、要么被隔离并以显式状态暴露；
可替换部件经抽象接口接入；优先公开数据集；专业书面语，文档定位为项目技术文档。

## 4 任务（按顺序）

### T1 服务端收敛到 Linux

- 移除仅服务于桌面平台的传感器：`lhm`（Windows）、`powermetrics`（macOS）及其测试与文档内容；
- 移除 `RaplSensor` 中仅为 Windows 测试而设的 `sep` 参数，测试改用真实的 `intel-rapl:N` 目录名；
- CI 的测试矩阵收敛为 Linux（可增加 `ubuntu-24.04-arm`）；
- 手册中删除 Windows / macOS 专用步骤（PowerShell 命令、Docker Desktop 说明等），开发环境统一按 Linux / WSL2 描述。

验收：`grep -ri "windows\|powershell\|macos" src docs/guide` 不再出现服务端相关的适配内容；CI 通过。

### T2 失败显式化与故障隔离

现有降级点（v0.1.0）：

| 位置 | 当前行为 |
|---|---|
| `site/service.py::build_power` | `--power auto`：配置的传感器 → 自动探测 → 功率模型 |
| `site/power.py::SensorPower` | 单个传感器异常被吞掉；节点设备缺少读数时其功率计为 0；主机部分按 整机 → CPU/内存 → 功率模型 依次降级 |
| `sensors/base.py::autodetect` | nvml → nvidia-smi；amdsmi 与 hwmon 互斥规则 |
| `sensors/builtin.py` | NVML 字段读取失败跳过；AMD SMI 多组字段名兼容；Redfish `EnvironmentMetrics` → `Power`；hwmon `power1_input` → `power1_average` → 能量差分 |
| `sensors/base.py::EnergyIntegrator` | 无硬件计数器时按功率积分；计数器下降时累加偏移 |
| `sched/routing.py` | `nearest`、`cost_aware`、`weighted` 不满足条件时改用最小负载 |
| `sched/capacity.py::GridAware` | 无需求统计时改用站点到达率；RTT 预算内无站点时改用全部站点；功率上限不足时逐级降低封顶比例 |
| `gateway/service.py` | 站点 503 或连接失败时排除后重试至多 3 次；站点状态拉取失败沿用旧快照并标记不健康；时钟同步失败时静默使用本地时钟 |
| `controller/service.py::collect` | 站点不可达则跳过；电网信号服务不可达时以空信号继续决策（策略会把缺失电价视为 0）；网关不可达时需求为空 |
| `config.py` | `Scenario.resolve` 依次在场景目录、其上级、当前目录查找相对路径；未提供地址文件时使用本机默认端口 |
| `site/executors.py::estimate_tokens` | 输入 token 数按启发式估计；缺少 `max_tokens` 时取 128 |
| `loadgen/client.py` | 时钟同步失败时静默使用本地时钟 |

目标设计：
1. **启动期校验（fail fast）**：配置声明的传感器不可用、数据文件缺失、非本机模式缺少地址文件、电网信号服务不可达等，
   一律启动失败并给出明确错误；功率来源由配置显式指定，不做自动替换。
2. **运行期隔离**：每个传感器、上游实例、外部服务独立维护健康状态；故障只影响其所属的读数或节点，
   读数缺失时标记为缺失（`None` + 状态），不以 0、默认值或模型值替代。
3. **显式状态**：`/healthz`（存活）与 `/readyz`（就绪）分离；`/state` 中给出各子模块状态与降级原因；
   指标 `cpc_sensor_up`、`cpc_sensor_errors_total`、`cpc_upstream_up`、`cpc_controller_collect_errors_total{target}` 等。
4. **决策层面**：输入不完整时控制器跳过本周期并记录原因，而不是以缺省输入继续决策；路由策略返回带原因的结构化结果
   （选择 / 拒绝及原因），网关的重试次数、可重试错误类型由配置给出并计入指标。
5. 能耗核算区分"实测"与"缺失"时段，报告中给出数据完整率。

验收：针对上述每一处补充单元测试（注入故障，断言状态与指标）；冒烟测试增加"停止一个站点 / 一个传感器失效"场景，
断言其他站点正常服务且故障被显式报告。

### T3 数据集抽象与公开数据集

1. 定义数据集接口，统一描述、获取、加载与变换：
   - `WorkloadDataset`：请求到达时间、输入/输出 token 数，可选模型名、会话标识；
   - `GridSignalDataset`：按区域与时间的电价、碳强度、需求响应事件；
   - `PowerProfileDataset`：设备型号、负载（批大小 / 吞吐）与功率的对应关系，用于标定节点模型；
   - 数据集清单（名称、版本、来源 URL、许可、文件校验和、引用）；下载到 `~/.cache/cpc/datasets`（或 `CPC_DATA_DIR`），不提交入库；
   - 变换：时间窗截取、时间压缩、到达率缩放、来源区域分配，均为显式参数。
2. 候选公开数据集（接入前逐一核实版本、字段、许可与可获取性）：
   - 推理负载：Azure LLM Inference Trace 2023 / 2024（`github.com/Azure/AzurePublicDataset`）、BurstGPT（`github.com/HPMLL/BurstGPT`）、
     Mooncake traces（`github.com/kvcache-ai/Mooncake`）；
   - 训练 / 批处理作业（后续扩展）：Alibaba `cluster-trace-gpu-v2020`、Microsoft Philly、SenseTime Helios、Shanghai AI Lab Acme；
   - 功率特性：ML.ENERGY 基准数据（`github.com/ml-energy/leaderboard`）、MIT Supercloud 数据集；
   - 电网信号：现有的省级分时电价政策与生态环境部省级排放因子；逐时数据需调研可公开获取的来源
     （国内电力交易中心公开的现货价格、国外 ENTSO-E、EIA Hourly Electric Grid Monitor 等），并明确时间与区域对应关系。
3. 现有 `workload.*` 合成参数与 `carbon_profile: synthetic` 退出默认配置；合成生成器保留为测试用数据集实现。
4. 仿真器、负载生成器、电网信号服务改为只依赖数据集接口。

验收：默认场景使用公开数据集运行仿真与在线负载；数据集清单中每项均有出处与校验和；无网络时给出明确错误而非退回合成数据。

### T4 其余可替换部件的抽象接口

- 节点后端（仿真 / OpenAI 兼容上游 / 后续其他推理接口）、功率来源、执行器（站点代理 / Kubernetes / 云厂商 API 整机开关机）、
  状态来源（直接拉取 / Prometheus）、时钟、结果输出；
- 插件注册由环境变量 `CPC_PLUGINS` 改为 Python 标准的 entry points（`pyproject.toml` 中的 `[project.entry-points."cpc.*"]`）；
- 消除 `site/core.py` 与 `sim/des.py` 中节点状态机的重复实现：抽出与时钟无关的状态机，由在线与仿真两侧共用。

### T5 文档修订

- 全部文档、README、注释改为专业、技术性的书面语，删除面向特定读者的表述（如设计说明书"编写目的"、
  "建议的上手路线"一节，封面"平台搭建资料""平台搭建小组"等）；
- 同步 T1–T4 的设计变更，补充失败处理、数据集与接口章节；重新编译 PDF。

### T6 回归与发布

- `pytest`、冒烟测试、文档编译、CI 全部通过；更新 `CHANGELOG`（新建）；
- 打标签 `v0.2.0`，由 GitHub Actions 发布 GHCR 镜像；如需 Docker Hub 镜像，手动执行多架构构建并推送。

## 5 关键文件

| 路径 | 内容 |
|---|---|
| `src/cpc/config.py` | 场景与地址配置模型 |
| `src/cpc/models.py` | 功率、性能、广域网时延模型与信号时钟 |
| `src/cpc/grid/` | 分时电价、排放因子、需求响应与电网信号服务 |
| `src/cpc/site/` | 站点代理：节点池状态机、执行器、功率来源 |
| `src/cpc/sensors/` | 功率传感器、自动探测、通用导出器 |
| `src/cpc/gateway/`、`src/cpc/controller/` | 路由网关、容量控制器与执行器 |
| `src/cpc/sched/` | 路由策略与容量策略 |
| `src/cpc/sim/` | 离散事件仿真器与作图 |
| `src/cpc/loadgen/` | 负载生成器 |
| `data/grid/` | 分时电价时段结构、2023 年省级排放因子（含出处） |
| `deploy/` | compose、k8s（生成器）、cloud（站点栈 / 控制栈）、Prometheus 规则、Grafana 仪表盘 |
| `scripts/` | 冒烟测试、真实 GPU 检查、文档插图 |
| `docs/` | Typst 模板、文档源文件、插图、参考文献、PDF |
