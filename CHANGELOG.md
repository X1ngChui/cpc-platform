# 变更记录

版本号遵循语义化版本。

## v0.2.0（2026-11-25）

### 数据

- 数据集改为描述文件驱动：数据集目录包含 `datapackage.yaml`（Frictionless Data Package v2 子集，扩展属性 `cpc`）与
  CSV / Parquet 数据文件。三类数据（`workload`、`grid_signal`、`power_profile`）各规定核心字段与约定扩展字段，
  其余字段为任意扩展字段；平台按类别规范校验后以 Apache Arrow 列式加载。
- 平台不再包含原始数据的解析代码与随包数据；公开数据集的转换范例移至 `examples/datasets/`（Azure LLM Inference Trace
  2023 / 2024、BurstGPT、Mooncake、EIA-930、ML.ENERGY Benchmark v3、中国省级分时电价与排放因子、恒定信号、合成负载）。
- 场景以路径引用数据集（支持 `${CPC_DATA_DIR}`）；电网信号源给出数据集与信号名，可声明扩展信号（`grid.extras`）；
  负载可选择扩展字段作为请求属性（`workload.attributes`），实验中经 `x-cpc-attr-*` 请求头传递。
- `cpc data` 子命令改为 `kinds`、`validate`、`info`、`summary`、`fetch`、`calibrate`，参数为数据集路径；
  `calibrate --curves` 输出查表式服务器模型参数。pyarrow 成为必需依赖。

### 模型与运行

- 新增数据中心运行时 `cpc.datacenter`：服务器状态机、站内排队与派发、容量指令执行、能耗计量，与时钟无关，
  由离散事件仿真器与站点代理共同驱动（取代两处分别实现的节点状态机）。
- 站点由服务器池组成，可异构；仿真服务器与真实服务器可在同一站点共存；功率测量在站点级声明（`measurement`）。
  站点的 `region` 取代 `province`。
- 服务器模型增加查表式功率与 token 间隔曲线（`gpu_power_curve`、`tpot_curve`）。
- 到达率估计改为与更新间隔无关的指数加权（`scheduler.rate_window_s`）。
- 仿真器按 trace 顺序流式读取到达事件；结果写为结果数据包（`requests.parquet`、`timeseries.csv`、`summary.json`、
  `decisions.jsonl`、`run.yaml`）。新增实验描述与 `cpc run`；`cpc compare` 基于同一机制。

### 调度

- 策略分为全局（路由、容量）与本地（派发、电源管理）两级。新增派发策略 `least_loaded`、`packing`、`efficient_first`、
  `affinity`、`priority` 与电源管理策略 `ordered`、`efficient`。
- 策略与功率传感器以 entry points 注册（组 `cpc.routing`、`cpc.capacity`、`cpc.dispatch`、`cpc.power`、`cpc.sensors`），
  取消环境变量 `CPC_PLUGINS`。

### 失败处理与故障隔离

- 配置与环境错误在启动阶段报错；运行期故障（传感器、站点、电网信号）隔离并以健康检查、状态字段与指标报告；
  缺失的读数与信号标记为缺失，不以模型值或默认值替代。网关重试、控制器的输入完整性检查与跳过记录。
- 服务端组件只面向 Linux（x86_64、aarch64）。

### 接口与部署

- 电网信号字段与指标改为与货币无关：`price`、`price_label`、`carbon_intensity`、`currency`；
  `cpc_grid_price_per_kwh`、`cpc_grid_carbon_intensity_kg_per_kwh`、`site:energy_cost_per_hour`。
- 状态摘要增加服务器池摘要 `pools` 与服务器的 `pool`；服务器指标增加 `pool` 标签。
- Compose、Kubernetes（kind）与云服务器部署中，电网信号服务挂载数据集目录。
- 冒烟测试增加 `--offline`（测试场景，不需要下载数据）。

### 文档

- 《平台设计说明书》与《使用手册》按设计与使用重写；示意图改为 draw.io 源文件并导出为矢量图。

## v0.1.0

- 首个版本：电网信号服务、站点代理、路由网关、容量控制器、离散事件仿真器、多硬件功率传感器与通用功率导出器；
  本机、Docker Compose、Kubernetes（kind）与云服务器部署。
