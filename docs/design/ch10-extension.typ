#import "../template/cpc-doc.typ": *

= 扩展 <ch-ext>

== 扩展点

#figure(
  tbl(
    columns: (8em, 1fr, 1fr),
    header: ("扩展点", "方式", "不需要修改平台的范围"),
    [数据], [按类别规范整理数据集；扩展字段直接加入表结构], [新的数据来源、扩展字段、扩展电网信号（场景 `grid.extras`）、请求属性（`workload.attributes`）],
    [服务器类型], [场景 `node_profiles` 中的参数或曲线；`cpc data calibrate` 由测量标定], [新的加速卡、模型与推理引擎组合],
    [路由策略], [继承 `RoutingPolicy`；entry point 组 `cpc.routing`], [任意跨数据中心路由算法],
    [容量策略], [继承 `CapacityPolicy`，声明 `requires`；entry point 组 `cpc.capacity`], [任意容量与功率分配算法，可同时给出路由权重],
    [派发策略], [继承 `DispatchPolicy`；entry point 组 `cpc.dispatch`], [站内请求派发与排队顺序],
    [电源管理策略], [继承 `PowerPolicy`；entry point 组 `cpc.power`], [站内服务器开关选择],
    [功率传感器], [继承 `Sensor`，实现 `read()`；entry point 组 `cpc.sensors`], [新的硬件或采集接口],
    [推理后端], [任何 OpenAI 兼容服务], [vLLM、SGLang、llama.cpp server、TGI 等],
  ),
  caption: [扩展点],
)

== 数据扩展

扩展字段不需要修改平台：在描述文件中声明字段及其类型，平台校验类型与约束后原样保留。负载数据集的扩展字段经场景
`workload.attributes` 选择后成为请求属性，策略通过 `req.attrs` 读取；电网信号数据集中的其他信号类型经 `grid.extras`
成为 `GridSignal.extra` 的项。需要平台理解其语义的字段（例如以统一名称参与参考策略），应先作为约定扩展字段加入类别规范
（`cpc.datasets.profiles.KINDS`），并提高规范版本号。

新增一类数据（例如训练与批处理作业：到达时间、资源请求与运行时长）需要在类别规范中定义资源与核心字段，
并提供相应的加载与使用方式。

== 功率传感器

传感器类在构造时确定设备集合与各字段使用的接口（能力探测），必需能力不可用时抛出 `SensorError`；
`read()` 返回 `Reading` 列表，单个字段读取失败时该值为 `None` 并在 `errors` 中记录原因；能设置功率上限的实现提供
`set_power_limit()` 并设 `supports_power_limit = True`。

```python
from cpc.sensors import Reading, Sensor


class MyAccel(Sensor):
    """通过厂商库读取加速卡功率。"""

    requires = "厂商管理库 libmyaccel"

    def __init__(self, cards: list[int] | None = None):
        super().__init__()
        self.cards = cards or [0]

    def read(self) -> list[Reading]:
        return [Reading(self.id, f"acc{i}", "npu", "vendor", power_w=read_power(i))
                for i in self.cards]
```

```toml
[project.entry-points."cpc.sensors"]
my-accel = "my_sensors:MyAccel"
```

站点 `measurement.sensors` 中以 `{type: my-accel, cards: [0, 1]}` 引用；`cpc sensors --types my-accel` 读取并打印读数。

== 执行器

站点代理的执行器钩子（`cpc.site.executors.RealNodeHooks`）实现真实服务器的开关机（配置了容器名时通过 Docker Engine 启停容器，
否则只停止派发）、功率封顶（显式开启时通过传感器设置加速卡功率上限）与就绪检查。控制器的执行器决定指令的下发对象：
`site-agent` 向站点代理下发指令；`kubernetes` 按站点伸缩推理实例 Deployment 的副本数。
其他执行方式（如通过服务器的带外管理接口开关整机）可按同一接口实现。
