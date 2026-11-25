# 变更记录

版本号遵循语义化版本。

## 未发布

### 修正

- `deploy/cloud/control-stack/docker-compose.yml` 的注释中有未注释的行，文件不是有效的 YAML，控制栈无法启动
  （自 v0.2.0）；新增测试检查仓库中全部 YAML 文件可解析。
- RAPL 传感器在容器内不可用：Docker 默认屏蔽容器内的 `/sys/devices/virtual/powercap`。传感器的 `root` 现在也接受
  该目录的层级布局，可把宿主机目录挂载到其他路径（如 `/host/powercap`）后读取。
- 镜像加入 `ipmitool`，`ipmi` 传感器可在容器内经 `/dev/ipmi0` 读取本机 BMC。
- 站点代理开启 `--enable-power-cap` 时必然启动失败：加速卡传感器的封顶能力检查在传感器启动之前执行。改为启动之后检查；
  在 NVIDIA A30 上验证了经 NVML 设置功率上限。
- 站点代理对同一服务器的执行器操作改为串行执行。此前关机尚未完成时到达的开机指令可能先执行，随后关机完成，
  服务器停留在 booting 而推理容器已退出。
- 流式请求的失败被记为成功：站点代理在连通上游之前就发出了 200 的响应头，上游连接失败只以流中的错误事件表示，
  网关与 `cpc live` 都把这类请求记为完成（e2e 约 0 秒），按负载路由的策略还会因该站点"负载低"而继续向其发送请求。
  现在站点代理先取得上游的首个片段，连接失败或上游返回错误时以相应的 HTTP 状态（502 等）返回；网关、`cpc loadgen`
  与 `cpc live` 检查流的完整性（错误事件、没有 `data: [DONE]`），这类请求记为失败；失败的请求不再计入站点的平均服务时间估计。
- 网关与控制器的 `PUT /admin/policy`：拼错的策略名或参数返回 400 与说明（v0.3.0 中为 500）。
- 流式响应停滞（如网络中断后连接既无数据也未关闭）时，请求一直等到读超时（600 秒）。新增场景参数
  `scheduler.stream_idle_timeout_s`（缺省 120 秒）：网关与站点代理在流式响应开始后，相邻数据片段的间隔超过该值即中止
  请求并记为失败。网关对站点以错误状态（如 400）回应的流式请求按普通错误返回并记录该状态。
- 在线的真实服务器失效（推理实例崩溃、容器退出）时，站点代理此前仍向其派发请求，请求全部以 502 失败，服务器
  一直显示为在线。现在站点代理每 5 秒检查在线真实服务器的就绪状态，连续 2 次失败即退出服务并重新启动推理实例，
  恢复后重新接收请求；原因记入服务器的 last_error（`health: ...`）与失败计数。
- 真实服务器的启动期限：`boot_s` 的 4 倍且不少于 300 秒内未就绪即视为开机失败（回到 off，记录原因），
  此前会无限期停留在 booting。
- 负载生成器的输入文本改为常见的英文短词：站点代理估计的输入长度恰为 trace 的输入 token 数，Qwen 分词器的
  实际 token 数也与之相同。此前站点估计为 trace 的约 1.35 倍，实验中仿真服务器的预填充时间相应偏长，与仿真不一致。
- 负载短于实验时长时，`cpc loadgen` 与 `cpc live` 报告一行错误（此前为 ValueError 调用栈）。
- 传感器不接受的参数在构造时报错并给出相近的参数名（此前被忽略）。
- `cpc.read_run()` 返回的时间序列中数值列为 float、空值为 None（此前全部为字符串，无法直接计算）。

### 精简

- 功率传感器只保留四种，每种测量一类对象，互不重叠：`nvml`（NVIDIA GPU）、`rapl`（CPU 与内存）、`ipmi`（本机 BMC 的
  整机功率）与通用的 `command`。删除 `nvidia-smi`（与 `nvml` 重复）、`amdsmi`、`hwmon`、`ascend-dcmi`、`redfish`、
  `prometheus`、`http-json` 以及 `ipmi` 的远程 BMC 模式（`IPMI_PASSWORD`）；其他设备以 `command` 适配或以 entry point 组
  `cpc.sensors` 注册。云部署站点栈删除可选的 dcgm-exporter（站点代理已导出同名的 GPU 功率指标）。

### 新增

- 站点代理从控制节点取得配置：电网信号服务提供 `GET /v1/config`（场景文本与服务地址，需管理令牌），
  `cpc site --control <地址> --site <id>` 代替 `-s` 与 `-e`。云部署的站点栈只需 `SITE_ID`、`CONTROL_URL` 与令牌，
  场景与地址文件只在控制节点上维护。
- GPU 站点栈的功率封顶补充文件 `docker-compose.powercap.yml`（`--enable-power-cap` 与容器的 `SYS_ADMIN` 权限，
  NVML 设置功率上限需要该权限）；NVML 请求的上限低于设备最小上限时在日志中警告。
- 云部署新增 CPU 推理的站点栈 `deploy/cloud/site-stack/docker-compose.cpu.yml`（llama.cpp，RAPL 与本机 BMC 测量功率）。
- `cpc compare`：并列比较两个或多个结果目录（仿真或实验）的总体与各站点指标（请求、时延、能耗、电费），两个目录时给出比值；
  Python 接口 `cpc.results.compare_runs()`。
- `cpc run` 的对比表按显示宽度对齐，与《使用手册》一致。
- `logs/` 目录随仓库提供：此前 Docker Compose 以 root 创建该目录，之后以普通用户运行的冒烟测试与 `cpc up` 无法写入日志。
- `cpc measure`：在站点的一台真实服务器上依次保持固定并发，测量实测功率、token 间隔、首 token 时延与吞吐，
  可选测量关机功率与开机时间，写出 power_profile 数据集；`cpc profile` 对这类数据集可省略 `--slots`、`--idle-w`、
  `--off-w`、`--boot-s`
  （取自测量，输出中注明来源），`--where` 改为可选。
- `cpc measure` 在各级功率读数几乎相同时给出警告并记入数据集（Clemson c6320 的 BMC 读数在加负载前后完全不变），
  `cpc profile` 生成服务器类型时重复该警告。
- 传感器通用参数 `max_w`：功率读数超过上限时记为缺失（不积分、不替代），原因记入读数错误与健康状态。
  用于排除 BMC 偶发的异常读数（Dell C6220 的 DCMI 读数间歇性地返回 16462 W）。
- 只用 CPU 的真实服务器：upstream 可以不声明加速卡，服务器功率为主机部分（`measurement.host` 为 `server` 或
  `cpu_dram`；为 `excluded` 时报错）。没有加速卡的服务器不能开启硬件功率封顶。

## v0.3.0（2026-11-25）

### 精简

- 服务器类型只以表给出：`power_w`（占用率 → 服务器 IT 功率，首点为在线空载）与 `tpot_s`（占用率 → token 间隔），
  点间线性插值；删除参数式模型的字段（`gpus`、`gpu_idle_w`、`gpu_max_w`、`power_exponent`、`host_*`、
  `decode_s_per_token`、`decode_slowdown` 等）。功率封顶表示为功率上限 = 空载 + 封顶比例 × (满载 − 空载)。
- 删除控制器的 Kubernetes 执行器、`cpc data fetch` 与 URL 资源、`cpc compare`、`cpc signals`、`cpc rtt`、
  电网信号服务的 `/v1/forecast`、路由策略 `random`、响应头 `x-cpc-energy-est-j`、仿真服务器的模拟加速卡指标。

### 新增

- `cpc check`：检查场景与数据并打印概要；`cpc live`：运行一次实验并写出与仿真相同格式的结果目录；
  `cpc profile`：由功率特性数据集生成服务器类型（取代 `cpc data calibrate`）。
- Python 接口 `cpc.simulate()`、`cpc.read_run()`；`cpc data` 子命令为 `kinds`、`check`、`show`；
  `cpc report` 不再需要场景文件。
- 快速上手场景 `examples/quickstart/`；《使用手册》按操作流程重写。

### 错误信息

- 常见的使用错误以一行说明报告并以状态 2 退出，不再输出 Python 调用栈：未知的策略名（给出相近的名称与全部可选项）、
  策略不接受的参数（给出相近的参数名与可用参数）、命令行 JSON 参数格式错误、仿真时长超过负载长度、
  场景 YAML 语法错误、数值参数取值越界。
- 场景、实验描述与地址文件的字段错误逐条列出字段路径与原因（缺少必填字段、不认识的字段及相近的正确字段名、
  类型或取值范围不符）；未知的策略名与参数统一报告为 `ConfigError`（此前为 `KeyError` / `TypeError`）。
- `cpc run` 在启动子进程之前检查全部变体的策略名、参数与负载字段。
- `--routing-params` / `--capacity-params`（及 `cpc.simulate` 的 `routing_params` / `capacity_params`）
  单独给出时修改场景中策略的参数；此前在未给出策略名时被忽略。
- `clock.speedup` 须大于 0。

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
