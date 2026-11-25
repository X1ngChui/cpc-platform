"""cpc: energy-aware scheduling of AI inference across data centres (compute-power coordination).

Python interface:
    import cpc
    res = cpc.simulate("configs/scenario.yaml", routing="cost_aware", hours=6, rate_scale=0.5)
    print(res.summary["facility_kwh"])
    run = cpc.read_run("results/sim-cost_aware__reactive")  # a result directory of the CLI or simulate(out=...)

Modules:
- datasets    dataset descriptors, validation and loading (workload, grid signals, power profiles)
- config      scenario and endpoint configuration
- models      server model (power and latency from tables), wide-area latency model, signal clock
- datacenter  data-centre runtime (server states, local queueing and dispatch, energy metering),
              shared by simulation and live experiments
- sched       policies: global (routing, capacity) and local (dispatch, power management)
- sim         discrete-event simulator
- live        live experiments (workload replay, sampling, result packages)
- grid / site / gateway / controller   services: grid signals, site agent, routing gateway, capacity controller
- sensors     power sensors
- measure     measurement of real servers
- results     result packages and experiment descriptions
"""

__version__ = "0.4.0.dev0"


def simulate(scenario, *, routing: str | None = None, routing_params: dict | None = None, capacity: str | None = None,
             capacity_params: dict | None = None, hours: float = 24.0, bin_s: float = 300.0, out=None, **workload):
    """Run a simulation and return a RunResult (summary, timeseries, requests, decisions).

    scenario is a scenario file or a loaded Scenario. routing / capacity replace the global policies
    of the scenario; routing_params / capacity_params alone update the parameters of the scenario's
    policies. Keyword arguments override fields of the scenario workload (rate_scale, start_s,
    duration_s, ...). With out, the result package is written to that directory as well.
    """
    from .config import Scenario, load_scenario, override_policy
    from .results import write_run
    from .sched import make_capacity, make_routing
    from .sim.des import Simulator

    scn = scenario.model_copy(deep=True) if isinstance(scenario, Scenario) else load_scenario(scenario)
    for k, v in workload.items():
        if k not in type(scn.workload).model_fields:
            raise TypeError(f"workload has no field {k}")
        setattr(scn.workload, k, v)
    sched = scn.scheduler
    sched.routing = override_policy(sched.routing, routing, routing_params)
    sched.capacity = override_policy(sched.capacity, capacity, capacity_params)
    r, c = sched.routing, sched.capacity
    res = Simulator(scn, make_routing(r.policy, r.params), make_capacity(c.policy, c.params), hours=hours, bin_s=bin_s).run()
    if out is not None:
        write_run(out, res, scn, {"name": str(out), "mode": "simulation", "hours": hours, "bin_s": bin_s,
                                  "routing": r.model_dump(), "capacity": c.model_dump()})
    return res


def read_run(path):
    """Read a result package: summary, run, requests (pyarrow.Table), timeseries, decisions."""
    from .results import read_run as _read

    return _read(path)
