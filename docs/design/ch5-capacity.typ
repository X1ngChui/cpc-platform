#import "../template/cpc-doc.typ": *
#import "../figures/diagrams.typ": states

= 节点容量管理子系统 <ch-capacity>

== 管理对象

容量管理决定"每个站点开多少节点、以多大功率运行"。平台中的容量控制量有两个：

- *在线节点数*（`active_nodes`）：仿真后端对应开机的服务器台数；真实后端对应运行中的推理实例数；
  Kubernetes 原生部署中对应推理 Deployment 的副本数。
- *功率封顶比例*（`cap_ratio`）：GPU 功率上限相对最大值的比例。仿真中按功率与速度模型生效；
  真实环境中可通过 NVML 设置 GPU 功率上限（需要 root 权限，且需显式开启 `--enable-power-cap`）。

== 节点状态机 <sec-states>

#figure(states, caption: [节点状态机])

站点代理收到容量指令后按以下规则执行（仿真器中的规则与之完全相同）：

- *扩容*：优先把排空中的节点恢复为在线（无需重启）；仍不足时把关机节点置为启动中。启动完成的判定：
  仿真后端为经过 `boot_s` 秒；真实后端为推理实例的 `/health`（vLLM 提供）或 `/v1/models` 返回 200。
  真实后端若配置了容器名，启动时通过 Docker Engine API 启动容器。
- *缩容*：先取消启动中的节点，再从在线节点中选在途请求最少的；空闲的直接关机，有在途请求的进入排空状态，
  不再接收新请求，待在途请求全部完成后关机。真实后端若配置了容器名，关机时停止容器。
- 真实后端的推理实例在站点代理启动时处于启动中状态，就绪检查（`upstreams[].ready_path`，默认 `/health`）通过后才接收请求。
- 开关机或功率封顶操作失败时，错误记入节点的 `last_error` 与 `cpc_node_actuation_errors_total`；开机失败的节点回到关机状态，
  由下一个控制周期重新决策。

#warn(title: "真实环境中的节能含义")[在租用的 GPU 服务器上停止推理容器后，GPU 仍然通电并处于空载状态。
本机实测（@sec-realgpu）中，RTX 5070 Ti 在 vLLM 容器停止后仍保持约 43 W 的空载功率。因此"关闭节点"在真实环境中能节省的只是
负载相关的动态功率；要节省空载功率，必须关闭整台服务器（通过 BMC/IPMI 或云厂商 API 关机、释放实例）。
仿真后端的 `standby_w` 参数默认为 0，代表整机关机；研究"只停服务不关机"的情形时，应把它设为节点空载功率。]

== 控制回路

容量控制器每个控制周期（场景 `scheduler.control_interval_s`，默认 15 s）执行一次：

+ *采集*：并发拉取各站点 `/state`、电网信号服务 `/v1/signals`、网关 `/state`（各来源区域到达率）；
+ *检查输入*：状态不可达的站点排除在本周期之外，原因记入 `ControlView.excluded` 与决策记录；没有可用站点，
  或策略声明所需的输入（`requires` 中的 `signals`、`demand`）缺失或不完整时，跳过本周期（`result = skipped`）并记录原因
  （`no_site_available`、`missing_signals`、`incomplete_signals`、`missing_demand`），不以缺省输入继续决策；
+ *决策*：构造 `ControlView`，调用容量策略的 `decide()`，得到 `CapacityPlan`；
+ *执行*：对每个站点调用执行器（默认 `POST /admin/capacity`）；若计划含路由权重，`PUT /admin/weights` 下发给网关；
  执行结果与失败原因逐站点记录；
+ *记录*：决策日志（JSONL）记录信号时间、结果、排除的站点、目标、执行结果、观测到的站点状态（含能耗完整率）、电网信号与需求；
  更新 Prometheus 指标（`cpc_controller_cycles_total{result,reason}`、`cpc_controller_collect_errors_total{target}`、
  `cpc_controller_apply_errors_total{site}`）。

控制周期与路由是两个时间尺度：路由逐请求决策（毫秒级），容量调整按周期进行（秒到分钟级），且节点启动需要时间。
因此容量策略需要预留余量（headroom），或利用电价、负载的可预知性提前扩容。

== 容量策略接口

```python
from cpc.sched import CapacityPlan, CapacityPolicy, register_capacity
from cpc.schemas import CapacityTarget

@register_capacity("my_capacity")
class MyCapacity(CapacityPolicy):
    requires = frozenset({"signals"})          # 所需输入；缺失时控制器跳过本周期，不调用 decide()

    def decide(self, view):
        # view: ControlView（now、interval_s、sites、signals、demand、rtt_ms、slo、min_active、excluded）
        targets = {}
        for sid, s in view.sites.items():     # s: SiteSnapshot；不可达的站点不在其中
            g = view.signals[sid]               # GridSignal：电价、碳强度……（声明了 signals 时覆盖全部站点）
            cheap = g.period in ("valley", "deep_valley")
            want = s.nodes_total if cheap else max(1, s.nodes_total // 2)
            targets[sid] = CapacityTarget(active_nodes=want, cap_ratio=1.0)
        return CapacityPlan(targets, weights=None, notes={"why": "示例"})
```

`CapacityPlan.weights` 不为空时，控制器把它下发给网关，供 `weighted` 路由策略使用；`notes` 原样写入决策日志，便于事后分析。

== 参考策略

#figure(
  tbl(
    columns: (7em, 1fr),
    header: ("策略", "行为"),
    [`static`], [全部节点常开、不封顶（基线）。不需要电网信号与需求统计。],
    [`reactive`], [阈值式自动伸缩（类似 Kubernetes HPA）：按 Little 定律由站点到达率 × 平均服务时间估计所需并发，保持目标利用率 `target_util`；扩容立即执行，缩容每周期最多减少 `scale_down_step` 个节点；遵守功率上限（需要电网信号）。上限低于 `min_active` 时以上限为准并在 `notes` 中记录。],
    [`grid_aware`], [电网信号感知的分配示例（需要电网信号与需求统计）：由全局到达率（`demand_source` 为 `gateway` 或 `sites`）估计所需并发槽数，按"等效电价 × PUE"从低到高把节点分给各站点（按各站点每节点槽数折算，受节点总数、功率上限约束，每站点至少保留 `min_active`）；按各来源 RTT 预算内各站点分到的容量生成路由权重，预算内没有站点的来源记入 `notes.rtt_budget_exceeded`；需求响应期间功率上限不足以支撑最少节点满载时，取满足上限的最大封顶比例（不低于站点的 `cap_min_ratio`），仍不可行时记入 `notes.cap_infeasible`。],
  ),
  caption: [参考容量策略],
)

所有参考策略都通过 `max_nodes_under_cap()` 遵守电网下发的设施功率上限：上限对应的 IT 功率除以节点满载功率（按封顶比例折算），
得到最多可开启的节点数。这是一个保守估计；更精细的做法可以利用站点实测功率与功率封顶。

== 执行器

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("执行器", "说明"),
    [`site-agent`（默认）], [调用站点代理 `POST /admin/capacity`。仿真站点与真实站点都走这一路径；真实站点的开关机与功率封顶由站点代理的节点钩子实现（Docker 启停容器、NVML 设置功率上限、HTTP 就绪检查）。],
    [`kubernetes`], [直接把 `active_nodes` 设为 Deployment 副本数（`deployment_template` 默认 `vllm-{site}`）。适合"一个副本 = 一个推理节点"的 Kubernetes 原生部署，此时 Kubernetes 负责调度 Pod 到带 GPU 的节点上。认证方式由 `--k8s-auth` 显式给出（`incluster` 或 `kubeconfig`），初始化失败即启动失败。],
  ),
  caption: [容量执行器],
)
