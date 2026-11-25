#import "../../template/cpc-doc.typ": *

= 数据中心运行时 <ch-runtime>

数据中心运行时（`cpc.datacenter.Datacenter`）实现一个站点内部的全部状态与规则：服务器状态机、请求排队与派发、
容量指令的执行与能耗计量。它与时钟无关：每个方法显式接收当前时间（单调不减的秒数），由驱动方调用。
离散事件仿真器以仿真时间驱动它，站点代理以 `time.monotonic()` 驱动它，因此两种运行方式下数据中心内部的行为由同一份代码决定。

== 驱动接口

#figure(
  tbl(
    columns: (15em, 1fr),
    header: ("方法 / 回调", "说明"),
    [`submit(job, now)`], [请求到达。队列为空时由派发策略选择服务器，选中则开始服务；否则进入队列；队列已满或没有在线、启动中的服务器时抛出 `Overloaded`。],
    [`finish(server, now, service_s)`], [请求在服务器上完成（或失败）。更新平均服务时间估计，处理排空中服务器的关机，从队首派发。],
    [`apply(target, now)`], [执行数据中心指令：功率封顶比例与目标在线服务器数（由电源管理策略落实到具体服务器）。],
    [`boot_complete(server, now)`], [服务器启动完成，转为在线并从队首派发。],
    [`actuation_failed(server, action, error, now)`], [执行器操作失败：记录错误与失败计数；开机失败的服务器回到关机。],
    [`server_failed(server, error, now)`], [在线或排空中的服务器失效（如推理实例崩溃）：记录原因；在线的转为启动中并重新发出
      `power_on`，排空中的转为关机。],
    [`cancel(job)`], [撤销排队中的请求（如客户端断开）。],
    [`advance(now)`、`meter(now)`], [把能耗积分到 `now`；返回累计 IT 能耗、功率有效与缺失时长、非关机时长。],
    [`update_rate(now)`], [更新到达率估计。],
    [`summary(now)`], [状态摘要 `SiteSnapshot`（全局策略的输入）。],
    [回调 `on_start(job, server, now)`], [请求开始在服务器上服务。仿真器据性能模型安排完成事件；站点代理把请求交给执行器。],
    [回调 `on_action(action)`], [需要执行的动作：`power_on`、`power_off`、`set_cap`。仿真器在 `server.boot_until` 安排启动完成事件；站点代理调用执行器钩子。],
  ),
  caption: [数据中心运行时的驱动接口],
)

== 服务器状态机

服务器有四个状态（@fig-states）：关机（off）、启动中（booting）、在线（on）、排空中（draining）。
只有在线且有空闲并发槽的服务器接收新请求。

#figure(image("../../figures/zh/server-states.pdf", width: 92%), caption: [服务器状态机]) <fig-states>

- 开机：off → booting，发出 `power_on`；仿真服务器经 `boot_s` 后、真实服务器经就绪检查（`GET <url><ready_path>` 返回 200）后转为 on；
  真实服务器在启动期限（`boot_s` 的 4 倍，不少于 300 秒）内未通过就绪检查视为开机失败，回到 off 并记录原因；
  站点代理对同一服务器的执行器操作按发出顺序串行执行（关机完成后才执行随后的开机）；
- 失效：站点代理每 5 秒检查在线真实服务器的就绪状态，连续 2 次失败即调用 `server_failed`：服务器不再接收请求，
  执行器重新启动推理实例（如启动已退出的容器），就绪检查通过后恢复服务，启动期限内未恢复则回到 off 并记录原因；
  开机失败回到 off；
- 关机：booting → off，或 on 且没有在服务的请求 → off，发出 `power_off`；on 且有在服务的请求 → draining，
  最后一个请求完成后 → off，发出 `power_off`；
- 扩容时 draining 的服务器直接恢复为 on，无需重启。

实验开始时，前 `initial_active` 台服务器（缺省为全部）处于工作状态：仿真服务器为 on，真实服务器为 booting（待就绪检查通过）。

== 排队与派发

请求到达时若队列为空，由派发策略（@sec-local-policy）在全部服务器中选择：先过滤可接收该请求的服务器，再打分取最小者。
没有可选服务器时请求进入队列；队列按派发策略给出的优先级排序（数值大者先出队），同优先级先到先出。
服务器空出并发槽、启动完成或容量变化后，从队首依次派发，直至队首请求没有可选服务器。

队列上限 = ⌈max(在线并发槽数, 最大单台服务器并发槽数) × `scheduler.queue_limit_per_slot`⌉；队列已满时请求被拒绝，
实验中站点返回 503（`type: overloaded`），网关按重试配置选择其他站点。

== 容量指令

数据中心指令 `CapacityTarget` 包含两项，任一项为 `None` 时不改变：

- `cap_ratio`：功率封顶比例。每台服务器取 `max(cap_min_ratio, cap_ratio)`（不超过 1），比例改变时发出 `set_cap`；
- `active_nodes`：目标在线服务器数（含启动中），截断到 \[0, 服务器总数\]。电源管理策略给出需要改变状态的服务器及其目标状态
  （on 或 off），运行时按状态机执行。

最少在线服务器数 `sites[].min_active` 与设施功率上限由全局容量策略处理；运行时只执行指令。

== 状态摘要

`summary()` 给出全局策略所需的状态摘要 `SiteSnapshot`：

#figure(
  tbl(
    columns: (15em, 1fr),
    header: ("字段", "含义"),
    [`nodes_total`、`nodes_on`、`nodes_booting`、`nodes_draining`], [服务器总数与各状态的服务器数],
    [`slots_on`、`busy`、`queue_len`、`queue_limit`], [在线并发槽数、在服务请求数、排队请求数与队列上限],
    [`slots_per_node`、`node_idle_w`、`node_peak_w`], [全部服务器的平均并发槽数、在线空载与满载 IT 功率（模型参数）],
    [`pools[]`], [各服务器池的类型、后端、各状态服务器数、并发槽数与功率参数],
    [`cap_ratio`、`cap_min_ratio`], [当前功率封顶比例；各服务器封顶比例下限中的最大值],
    [`it_power_w`、`facility_power_w`、`pue`], [IT 功率与设施功率；任一服务器功率缺失时为 `None`],
    [`power_scope`], [`node`（加速卡 + 主机部分）或 `accelerators`（只含加速卡）],
    [`energy_it_j`、`energy_coverage`], [功率有效区间内的累计 IT 能耗；功率有效时长占比（服务器·秒加权）],
    [`arrival_rate`、`est_service_s`], [到达率与平均服务时间的估计],
    [`nodes[]`、`components`], [各服务器的状态与功率；功率传感器等依赖的健康状态（实验）],
  ),
  caption: [状态摘要的主要字段],
)

*到达率估计。* 到达率以时间常数 $tau$ = `scheduler.rate_window_s` 指数加权：两次更新相隔 $Delta t$、期间到达 $n$ 个请求时，
$lambda arrow.l lambda + (1 - e^(-Delta t \/ tau)) (n \/ Delta t - lambda)$。该估计与更新间隔基本无关，
因此仿真（每个控制周期更新）与实验（每个周期任务更新）给出的估计一致。
平均服务时间以每个完成请求的服务时长指数加权（系数 0.05）。

== 能耗计量

功率在相邻两次变化之间视为恒定，按服务器积分：每次状态、占用率或封顶比例变化前，先以变化前的功率积分到当前时刻。
仿真服务器的功率在每次变化后由服务器模型重算；真实服务器的功率由站点代理在 `advance(now)` 之后写入传感器实测值。
功率缺失（`None`）的区间不积分，计入缺失时长，`energy_coverage` 给出功率有效时长占比。
仿真器在每个时间片结束时读取累计计量的差分，得到该时间片的 IT 能耗与非关机服务器时长。
