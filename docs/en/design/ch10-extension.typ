#import "../../template/cpc-doc.typ": *

= Extension <ch-ext>

== Extension points

#figure(
  tbl(
    columns: (8em, 1fr, 1fr),
    header: ("Extension point", "How", "Possible without changing the platform"),
    [data], [prepare datasets following the kind specification; add extension fields to the table schema], [new data sources, extension fields, extra grid signals (scenario `grid.extras`), request attributes (`workload.attributes`)],
    [server types], [power and time-per-token tables in `node_profiles`; generated from measurements by `cpc profile`], [new combinations of accelerator, model and inference engine],
    [routing policies], [subclass `RoutingPolicy`; entry-point group `cpc.routing`], [any routing algorithm across data centres],
    [capacity policies], [subclass `CapacityPolicy` and declare `requires`; entry-point group `cpc.capacity`], [any capacity and power allocation, optionally with routing weights],
    [dispatch policies], [subclass `DispatchPolicy`; entry-point group `cpc.dispatch`], [request dispatch and queue order inside a site],
    [power policies], [subclass `PowerPolicy`; entry-point group `cpc.power`], [choice of servers to switch inside a site],
    [power sensors], [subclass `Sensor` and implement `read()`; entry-point group `cpc.sensors`], [new hardware or collection interfaces],
    [inference backends], [any OpenAI-compatible service], [vLLM, SGLang, llama.cpp server, TGI, ...],
  ),
  caption: [Extension points],
)

== Data

Extension fields need no change to the platform: declare the field and its type in the descriptor, and the
platform validates type and constraints and keeps it. Extension fields of workloads selected by
`workload.attributes` become request attributes that policies read from `req.attrs`; other signals of a
grid-signal dataset become items of `GridSignal.extra` through `grid.extras`. Fields whose meaning the
platform should know (for instance to be used by reference policies under an agreed name) are first added
as conventional extension fields to the kind specification (`cpc.datasets.profiles.KINDS`) with a new
profile version.

A new kind of data (for example training and batch jobs: arrival, resource request and run time) needs a
resource and core fields in the kind specification and corresponding loading and use.

== Power sensors

A sensor class determines its devices and the interface of every field in its constructor (capability
probing) and raises `SensorError` when a required capability is missing; `read()` returns a list of
`Reading`, with failed fields `None` and the reason in `errors`; implementations that can set power limits
provide `set_power_limit()` and set `supports_power_limit = True`.

```python
from cpc.sensors import Reading, Sensor


class MyAccel(Sensor):
    """Accelerator power through a vendor library."""

    requires = "vendor management library libmyaccel"

    def __init__(self, cards: list[int] | None = None, **params):
        super().__init__(**params)      # generic parameters (max_w) and unknown-parameter check
        self.cards = cards or [0]

    def read(self) -> list[Reading]:
        return [Reading(self.id, f"acc{i}", "npu", "vendor", power_w=read_power(i))
                for i in self.cards]
```

```toml
[project.entry-points."cpc.sensors"]
my-accel = "my_sensors:MyAccel"
```

Sites refer to it in `measurement.sensors` as `{type: my-accel, cards: [0, 1]}`; `cpc sensors --types
my-accel` reads and prints its readings. Real servers using such accelerators declare them with
`devices: [acc0, acc1]` in their upstream.

== Executors

The executor hooks of the site agent (`cpc.site.executors.RealNodeHooks`) switch real servers on and off
(starting and stopping containers through the Docker Engine when a container is configured, otherwise
only stopping dispatch), cap power (setting accelerator power limits through the sensors when enabled)
and check readiness. The controller sends instructions to the site agent, the data-centre runtime applies
them with its state machine, and the executor hooks act on the real servers. Other actuation (e.g.
switching whole servers through out-of-band management) can implement the same hook interface
(`power_on`, `power_off`, `set_cap`, `is_ready`).
