#import "../template/cpc-doc.typ": *

= 数据集 <ch-data>

平台的负载、电网信号与节点功率特性均来自数据集。数据集经统一的抽象接入（`cpc.datasets`），
仿真器、负载生成器与电网信号服务只依赖数据集接口；公开数据集优先，合成数据集只用于单元测试与示例。

== 数据集抽象

数据集分为三类：

#figure(
  tbl(
    columns: (8em, 1fr, 1fr),
    header: ("类别", "内容", "接口"),
    [`workload`], [请求到达时间、输入 / 输出 token 数，可选模型名与会话标识], [`load_trace()` → `WorkloadTrace`],
    [`grid_signal`], [按区域与时间的电价或碳强度], [`value(quantity, region, t)` → `SignalValue`；`regions()`；`coverage()`],
    [`power_profile`], [设备型号、负载（批大小、吞吐）与功率的对应关系], [`points(...)` → `PowerPoint` 列表；`calibrate()`],
  ),
  caption: [数据集类别],
)

每个数据集有一份清单（`Manifest`）：名称、版本（固定到提交、发布标签或修订号）、类别、来源、许可、引用，以及每个文件的
下载地址、大小与 SHA-256 校验和。文件分两种：

- *远程文件*下载到缓存目录 `$CPC_DATA_DIR`（缺省 `~/.cache/cpc/datasets`）下的 `<名称>/<版本>/`，不提交入库；
- *随包文件*位于 `cpc/datasets/bundled/`，是从公开文件整理的小型数据（如政策文件中的分时电价时段），同样记录校验和。

加载前校验全部文件；文件缺失或校验失败时抛出 `DatasetError` 并给出获取方式。平台不自动下载，也不以合成数据替代缺失的数据集：
下载由 `cpc data fetch` 显式执行，需要认证的数据集（Hugging Face 上的受控访问数据集）从环境变量 `HF_TOKEN` 读取令牌。
大文件解析后的数组缓存为派生文件（`derived/*.npz`），并记录所依据原始文件的校验和，原始文件变化时重新生成。

=== 负载变换

`WorkloadTrace` 为按到达时间排序的数组（相对时间、输入与输出 token 数、可选的模型名与会话标识）及其起点的绝对时间。
场景的 `workload` 段给出数据集与显式变换（`TraceTransform`）：

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("参数", "含义"),
    [`start_s`、`duration_s`], [时间窗（原始 trace 时间，秒）。时间窗超出数据集覆盖范围时报错],
    [`time_scale`], [时间压缩倍数：变换后时间 = (t − `start_s`) / `time_scale`],
    [`rate_scale`], [到达率缩放：≤ 1 时每个请求以该概率独立保留；> 1 时叠加 ⌈`rate_scale`⌉ 个稀疏化副本，每个副本保留概率 `rate_scale` / ⌈`rate_scale`⌉（取整数时各副本相同，请求在同一时刻重复）],
    [`seed`], [缩放与来源区域分配的随机种子],
  ),
  caption: [负载变换参数],
)

变换后的 $t = 0$ 对应场景时钟起点 `clock.start`。请求按场景 `sources[].share` 的比例随机分配到来源区域。
仿真时长超过变换后负载的覆盖范围时，仿真器报错而不是截断或补充请求。

=== 时间与区域的对应关系

- *负载*：trace 时间整体平移到场景时钟。默认场景取 Azure 2024 trace 中 2024-05-14 16:00 UTC 起的 24 小时，
  对应场景时钟起点 2026-07-15 00:00（北京时间，即 16:00 UTC），保持 UTC 时刻对应；
- *电网信号*：站点在数据集中的区域键缺省为站点的 `province`，可由 `grid.price.regions`、`grid.carbon.regions` 逐站点指定。
  分时电价按北京时间判定时段；EIA-930 以 UTC 小时结束时刻标注，区间 [结束时刻 − 1 h, 结束时刻) 取该小时的值；
- *覆盖范围*：有时间覆盖范围的电网数据集须覆盖场景时钟起点，否则电网信号服务启动失败；运行期某站点某时刻的数据不可用时，
  该站点列入 `/v1/signals` 的 `missing`，网关视图中其信号为 `None`，需要电网信号的容量策略跳过该周期（@sec-failure）。
  仿真器只对信号可用的时间片核算成本与碳排放，汇总中给出 `signal_coverage`（按设施电量计的覆盖率）。

== 已接入的数据集

#figure(
  tbl(
    columns: (13em, 6.5em, 1fr, 6em),
    header: ("名称", "类别", "内容与规模", "许可"),
    [`azure-llm-2023-conv` / `-code`], [workload], [Azure LLM Inference Trace 2023 @patel2024splitwise：2023-11-16 对话 / 代码服务各 1 小时，19 366 / 8 819 个请求], [CC-BY-4.0],
    [`azure-llm-2024-conv` / `-code`], [workload], [Azure LLM Inference Trace 2024 @stojkovic2025dynamollm @azure2024trace：2024-05 各 1 周，2730 万 / 1680 万个请求（平均 45.1 / 27.8 请求/秒）], [CC-BY-4.0],
    [`burstgpt-1`], [workload], [BurstGPT v2.0 的 `BurstGPT_1.csv` @wang2025burstgpt：ChatGPT 与 GPT-4 服务前 2 个月，排除失败请求后 140 万个请求；时间戳无绝对日期。参数 `include_failed` 须显式给出], [CC-BY-4.0],
    [`mooncake-fast25-*`], [workload], [Mooncake FAST'25 trace @qin2025mooncake：conversation、toolagent 各 1 小时采样（12 031 / 23 608 个请求，平均输入 12 035 / 8 596 token），synthetic 为由公开数据集构造的泊松到达], [Apache-2.0],
    [`cn-tou-tariffs`], [grid_signal], [广东、贵州、内蒙古（蒙西）、上海工商业分时电价的时段结构与浮动比例（@sec-tou），随包提供], [政府公开信息],
    [`cn-provincial-carbon-2023`], [grid_signal], [2023 年 30 个省级电力平均二氧化碳排放因子 @mee2025factors（年均值），随包提供], [政府公开信息],
    [`eia930-carbon-2024h1`], [grid_signal], [EIA-930 2024 年 1–6 月各平衡区逐时分燃料净发电量 @eia930；碳强度 = Σ 发电量 × 排放因子 / Σ 发电量，排放因子由参数显式给出], [公有领域],
    [`mlenergy-benchmark-v3-llm`], [power_profile], [ML.ENERGY Benchmark v3 @chung2025mlenergy @mlenergy2025v3 的 LLM 运行汇总（1 138 次运行，NVIDIA H100、B200）；受控访问，需 `HF_TOKEN`], [Apache-2.0],
    [`synthetic-poisson`], [workload], [日内波动的非齐次泊松到达与对数正态 token 长度（合成，只用于测试）], [—],
    [`synthetic-diurnal-carbon`、`fixed-price`], [grid_signal], [年均排放因子叠加日内正弦形状；参数给定的固定电价（合成或参数化，只用于测试与示例）], [—],
  ),
  caption: [已接入的数据集（`cpc data list` 查看版本与本地状态）],
)

EIA-930 不含排放数据，碳强度依赖排放因子。平衡区从未上报的燃料类别视为没有此类发电；已上报的类别在某小时为空、
有正发电量的燃料没有给出排放因子、或总发电量不为正时，该小时的碳强度不可用。示例场景 `configs/scenario.us-eia.yaml`
采用 Electricity Maps 公布的默认生命周期排放因子 @electricitymaps2026factors（其中煤、天然气、核电、水电、光伏、风电取自
IPCC 第五次评估报告 @ipcc2014annex3）；EIA 的 "Other Fuel Sources" 取生物质的因子，属于假设，在场景文件中注明。
EIA 2024 年下半年的文件改用了更细的燃料分类，列结构不同，未纳入该数据集。

== 节点参数标定 <sec-calibration>

`cpc data calibrate` 对功率特性数据集中同一（模型、GPU、任务、GPU 数）的各次运行，以平均批大小 / 并发槽数为占用率 $u$，拟合：

$ P(u) = P_a + (P_"max" - P_a) dot u^alpha, quad u > 0 $

$ T_"pot" (u) = d_0 (1 + k u) $

其中 $P_a$ 为在服务时的单卡功率下限（`gpu_active_min_w`），$alpha$ 在 [0.1, 2] 上网格搜索、其余参数线性最小二乘。
数据集只含在服务期间的测量，不含没有请求时的空载功率，因此空载功率 `gpu_idle_w` 不由标定给出；
节点功率模型在占用率为 0 时取 `gpu_idle_w`，否则按上式计算。主机功率与预填充吞吐也不在数据集中。

默认场景的节点为一台 8×H100 服务器（Llama-3.1-405B-Instruct-FP8，张量并行 8，`max_num_seqs` 不超过 128 的 6 次运行）：

#figure(
  tbl(
    columns: (12em, 7em, 1fr),
    header: ("参数", "标定值", "说明"),
    [`gpu_active_min_w`], [306.4 W], [单卡在服务时的功率下限],
    [`gpu_max_w`], [581.6 W], [单卡满载功率],
    [`power_exponent`（$alpha$）], [0.38], [功率随批大小凹增长],
    [`decode_s_per_token`（$d_0$）], [31.63 ms], [低负载时的 token 间隔],
    [`decode_slowdown`（$k$）], [1.039], [满批时 token 间隔约为 2 倍],
    [拟合残差], [5.4 W / 0.69 ms], [单卡功率 / token 间隔的均方根误差],
  ),
  caption: [由 `mlenergy-benchmark-v3-llm@f4db020` 标定的节点参数],
)

`gpu_idle_w`（70 W）、`host_idle_w` / `host_max_w`（800 / 1600 W）、`prefill_tokens_per_s`（5 000）、`boot_s`（180 s）
仍为示意值，在场景文件中注明；接入真实服务器后应以实测值替换。

== 逐时电网数据的获取

逐时电价与碳强度是研究时间维度调度的基础。调研与接入情况如下：

- *美国*：EIA-930 提供各平衡区逐时分燃料发电量，以半年为单位发布 CSV 文件，无需注册，已接入（碳强度）；
  逐时电价不在其中，示例场景使用参数给定的固定电价。
- *欧洲*：ENTSO-E 透明度平台提供日前电价与分燃料发电量，通过其 API 获取需要注册后申请的访问令牌，
  可按本章的接口实现为需要认证的数据集。
- *中国*：省级分时电价政策与生态环境部年度排放因子已接入；各省电力现货市场价格由电力交易机构发布，
  公开范围与获取方式因省而异，尚未接入可固定版本并校验的逐时文件。研究市场电价时，应把取得的逐时价格整理为数据集后通过
  `grid.price` 引用。
