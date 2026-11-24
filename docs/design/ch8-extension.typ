#import "../template/cpc-doc.typ": *

= 扩展指南与后续工作 <ch-ext>

== 扩展点一览

#figure(
  tbl(
    columns: (8em, 13em, 1fr),
    header: ("扩展什么", "怎么做", "说明"),
    [路由策略], [继承 `RoutingPolicy`，`@register_routing("名")`], [放在自己的模块中，用环境变量 `CPC_PLUGINS=模块名` 加载；场景文件 `scheduler.routing.policy` 引用。],
    [容量策略], [继承 `CapacityPolicy`，`@register_capacity("名")`], [同上；可返回路由权重，与 `weighted` 路由配合形成两级调度。],
    [功率传感器], [继承 `Sensor`，实现 `read()`，`@register_sensor("名")`], [如需自动探测，实现类方法 `detect()` 并加入 `AUTO_ORDER`；能做功率封顶的实现 `set_power_limit()`。],
    [电网数据], [修改 `data/grid/*`，或提供 `grid.traces` CSV], [新增省份：在 `tou_tariffs.yaml` 增加一项，键与站点的 `province` 一致。],
    [推理后端], [任何 OpenAI 兼容服务], [站点配置 `backend: openai` 与 `upstreams`；SGLang、llama.cpp server、TGI 等均可。],
    [部署], [修改 `deploy/` 下的清单], [Kubernetes 清单由 `deploy/k8s/generate.py` 按场景生成，修改生成器而不是生成结果。],
  ),
  caption: [扩展点],
)

一个完整的策略插件示例（文件 `my_policies.py`，与 `configs/` 同级即可）：

```python
from cpc.sched import RoutingPolicy, register_routing

@register_routing("greenest")
class Greenest(RoutingPolicy):
    """在 RTT 预算内选碳排放因子最低、且未满载的站点。"""
    def __init__(self, rtt_budget_ms: float = 30, **params):
        super().__init__(**params)
        self.budget = rtt_budget_ms

    def choose(self, req, view, exclude=()):
        cands = [sv for sv in self.candidates(view, exclude)
                 if view.rtt(req.source, sv.id) <= self.budget and sv.load < 0.9]
        if not cands:
            return self.least_loaded(self.candidates(view, exclude))
        return min(cands, key=lambda sv: sv.signal.carbon_kg_per_kwh if sv.signal else 0).id
```

```bash
# 仿真中比较（需在 my_policies.py 所在目录运行）
CPC_PLUGINS=my_policies cpc compare --runs "nearest:reactive,greenest:reactive" --hours 24
# 在线平台中使用
CPC_PLUGINS=my_policies cpc up --routing greenest
```

== 尚未覆盖、值得扩展的方向

+ *训练与批处理任务*：目前平台只调度在线推理请求。可以增加"作业"类型（GPU 数、时长、截止时间、是否可中断），
  在容量管理中按电价、碳强度在时间与空间上安排作业，并与推理负载共享节点。
+ *预测*：电价与分时时段是可预知的，但负载与可再生能源出力需要预测；可在控制器中加入预测模块，为容量策略提供未来若干周期的输入。
+ *更真实的电网信号*：接入电力现货市场价格、逐时碳强度（通过 `grid.traces`），或与电力系统仿真工具（如 PyPSA、Vessim）联合仿真。
+ *大规模 Kubernetes 仿真*：用 KWOK（Kubernetes WithOut Kubelet）模拟成百上千个带 GPU 的节点，研究调度器层面的问题。
+ *容器级能耗归属*：在 Kubernetes 中部署 Kepler 与 dcgm-exporter，通过 `prometheus` 传感器把容器级能耗接入平台。
+ *多网关*：当前网关是单实例；多网关时 pending 计数不再全局可见，需要研究分布式路由的一致性与羊群效应。
+ *功率封顶的真实效果*：在租用的数据中心 GPU 上（需 root 权限）测量功率封顶对吞吐与时延的影响，标定性能模型中的 β。

== 建议的上手路线

下面是给新加入同学的建议顺序，每一步都有可检查的结果：

#figure(
  tbl(
    columns: (3em, 12em, 1fr),
    header: ("步骤", "任务", "完成标准"),
    [1], [本机安装并运行单元测试与冒烟测试], [`pytest` 全部通过；`python scripts/smoke_test.py` 输出"冒烟测试通过"。],
    [2], [阅读 `configs/scenario.yaml`，用 `cpc signals`、`cpc rtt` 理解电网信号与时延], [能解释某站点某时段电价的来源（政策文件与浮动比例）。],
    [3], [运行 `cpc compare` 与 `cpc report`], [能读懂对比表与运行曲线，说明各策略组合差异的原因。],
    [4], [写一个自己的路由策略插件，在仿真中与参考策略比较], [插件通过 `CPC_PLUGINS` 加载，结果可复现（固定种子）。],
    [5], [Docker Compose 部署并在 Grafana 中观察运行时切换策略、注入需求响应事件的效果], [能在仪表盘上指出功率上限生效的时刻与站点在线节点的变化。],
    [6], [在本机或一台云 GPU 服务器上部署 vLLM 与站点栈，用 `cpc sensors` 检查功率读数], [真实站点通过就绪检查上线，功率曲线随负载变化。],
    [7], [用实测数据标定节点功率模型参数，更新仿真站点的 `node_profiles`], [仿真站点在相同负载下的功率与实测站点的偏差在可接受范围内，并在报告中注明。],
    [8], [3 台云服务器 + 控制节点的完整实验], [按《部署与使用手册》完成部署，得到可复现的实验数据与日志。],
  ),
  caption: [建议的上手路线],
)
