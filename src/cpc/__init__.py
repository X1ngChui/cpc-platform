"""cpc：算电协同（能耗感知）跨数据中心 AI 计算任务调度平台。

Python 接口：
    import cpc
    res = cpc.simulate("configs/scenario.yaml", routing="cost_aware", hours=6, rate_scale=0.5)
    print(res.summary["facility_kwh"])
    run = cpc.read_run("results/sim-cost_aware__reactive")   # 读取命令行或 simulate(out=...) 写出的结果目录

模块划分：
- datasets    数据集描述、校验与加载（负载、电网信号、功率特性）
- config      场景与服务地址配置
- models      服务器模型（功率与时延，按表插值）、广域网时延模型、信号时钟
- datacenter  数据中心运行时（服务器状态机、站内排队与派发、能耗计量；仿真与实验共用）
- sched       调度策略：全局（路由、容量）与本地（派发、电源管理）
- sim         离散事件仿真器
- live        实验运行（回放负载、采样、结果数据包）
- grid / site / gateway / controller   实验服务：电网信号、站点代理、路由网关、容量控制器
- sensors     功率传感器与通用功率导出器
- loadgen     负载生成器
- results     结果数据包与实验描述
"""

__version__ = "0.3.0"


def simulate(scenario, *, routing: str | None = None, routing_params: dict | None = None, capacity: str | None = None,
             capacity_params: dict | None = None, hours: float = 24.0, bin_s: float = 300.0, out=None, **workload):
    """运行一次仿真并返回 RunResult（summary、timeseries、requests、decisions）。

    scenario 为场景文件路径或已加载的 Scenario；routing / capacity 覆盖场景中的全局策略，
    只给出 routing_params / capacity_params 时修改场景中策略的这些参数；
    workload 的关键字参数覆盖场景 workload 中的同名字段（如 rate_scale、start_s、duration_s）；
    给出 out 时同时把结果数据包写入该目录。
    """
    from .config import Scenario, load_scenario, override_policy
    from .results import write_run
    from .sched import make_capacity, make_routing
    from .sim.des import Simulator

    scn = scenario.model_copy(deep=True) if isinstance(scenario, Scenario) else load_scenario(scenario)
    for k, v in workload.items():
        if k not in type(scn.workload).model_fields:
            raise TypeError(f"workload 没有字段 {k}")
        setattr(scn.workload, k, v)
    scn.scheduler.routing = override_policy(scn.scheduler.routing, routing, routing_params)
    scn.scheduler.capacity = override_policy(scn.scheduler.capacity, capacity, capacity_params)
    r, c = scn.scheduler.routing, scn.scheduler.capacity
    res = Simulator(scn, make_routing(r.policy, r.params), make_capacity(c.policy, c.params), hours=hours, bin_s=bin_s).run()
    if out is not None:
        write_run(out, res, scn, {"name": str(out), "mode": "simulation", "hours": hours, "bin_s": bin_s,
                                  "routing": r.model_dump(), "capacity": c.model_dump()})
    return res


def read_run(path):
    """读取结果数据包：返回 summary、run、requests（pyarrow.Table）、timeseries、decisions。"""
    from .results import read_run as _read

    return _read(path)
