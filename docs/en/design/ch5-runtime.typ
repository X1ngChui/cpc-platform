#import "../../template/cpc-doc.typ": *

= Data-centre runtime <ch-runtime>

The data-centre runtime (`cpc.datacenter.Datacenter`) implements all state and rules inside a site: the
server state machine, request queueing and dispatch, execution of capacity instructions and energy
metering. It has no clock: every method takes the current time (monotonic seconds) from its driver. The
discrete-event simulator drives it on simulated time and the site agent on `time.monotonic()`, so one
piece of code determines the behaviour inside a data centre in both modes.

== Driver interface

#figure(
  tbl(
    columns: (15em, 1fr),
    header: ("Method / callback", "Description"),
    [`submit(job, now)`], [a request arrives. With an empty queue the dispatch policy chooses a server and the request starts; otherwise it queues; a full queue or a site without online or booting servers raises `Overloaded`.],
    [`finish(server, now, service_s)`], [a request on a server completed (or failed). Updates the mean service time, switches draining servers off, dispatches from the head of the queue.],
    [`apply(target, now)`], [applies an instruction: power-cap ratio and number of online servers (turned into concrete servers by the power policy).],
    [`boot_complete(server, now)`], [a server finished booting; it goes online and the queue is dispatched.],
    [`actuation_failed(server, action, error, now)`], [an executor action failed: the error and failure count are recorded; a server failing to boot returns to off.],
    [`server_failed(server, error, now)`], [an online or draining server failed (e.g. its inference instance crashed): the reason is recorded; an online server goes back to booting with a new `power_on`, a draining one goes off.],
    [`cancel(job)`], [withdraws a queued request (client disconnected, ...).],
    [`advance(now)`, `meter(now)`], [integrate energy up to `now`; return accumulated IT energy, time with valid and missing power, and time not off.],
    [`update_rate(now)`], [updates the arrival-rate estimate.],
    [`summary(now)`], [state summary `SiteSnapshot` (input of global policies).],
    [callback `on_start(job, server, now)`], [a request starts on a server. The simulator schedules its completion with the performance model; the site agent hands it to an executor.],
    [callback `on_action(action)`], [an action to perform: `power_on`, `power_off`, `set_cap`. The simulator schedules the end of booting at `server.boot_until`; the site agent calls its executor hooks.],
  ),
  caption: [Driver interface of the data-centre runtime],
)

== Server state machine

Servers have four states (@fig-states): off, booting, on and draining. Only servers that are on with a free
slot accept new requests.

#figure(image("../../figures/en/server-states.pdf", width: 92%), caption: [Server state machine]) <fig-states>

- power on: off → booting with `power_on`; emulated servers turn on after `boot_s`, real servers when their
  health check (`GET <url><ready_path>` returns 200) passes; a real server that does not pass within the
  boot deadline (4 × `boot_s`, at least 300 s) has failed to boot and returns to off with the reason. The
  site agent runs the actions of one server in the order issued (a power-on waits for a preceding power-off);
- failure: the site agent checks online real servers every 5 s; after 2 consecutive failures it calls
  `server_failed`: the server takes no more requests, the executor restarts the inference instance (e.g.
  starts the exited container), and the server serves again when its health check passes, or returns to
  off with the reason when the boot deadline expires;
- power off: booting → off, or on without requests in service → off, with `power_off`; on with requests in
  service → draining, then off with `power_off` after the last request;
- scale-up turns draining servers back on without a restart.

At the start of a live experiment the first `initial_active` servers (default: all) are active: emulated
servers on, real servers booting until their health check passes.

== Queueing and dispatch

When a request arrives and the queue is empty, the dispatch policy (@sec-local-policy) chooses among all
servers: it filters those that can accept the request and takes the lowest score. Without a candidate the
request queues; the queue is ordered by the priority of the dispatch policy (higher first), first come
first served on ties. Freed slots, completed boots and capacity changes dispatch from the head of the
queue until its first request has no candidate.

The queue limit is ⌈max(online slots, slots of the largest server) × `scheduler.queue_limit_per_slot`⌉; a
full queue rejects the request: in live experiments the site returns 503 (`type: overloaded`) and the
gateway tries another site as its retry configuration says.

== Capacity instructions

An instruction `CapacityTarget` has two items; `None` leaves an item unchanged:

- `cap_ratio`: power-cap ratio. Every server takes `max(cap_min_ratio, cap_ratio)` (at most 1) and a change
  issues `set_cap`;
- `active_nodes`: servers to keep online (booting included), clamped to \[0, servers\]. The power policy
  names the servers that change state and their target (on or off); the runtime follows the state machine.

The minimum online servers `sites[].min_active` and facility power limits are the business of the global
capacity policy; the runtime only applies instructions.

== State summary

`summary()` returns the state summary `SiteSnapshot` that global policies need:

#figure(
  tbl(
    columns: (15em, 1fr),
    header: ("Field", "Meaning"),
    [`nodes_total`, `nodes_on`, `nodes_booting`, `nodes_draining`], [servers in total and per state],
    [`slots_on`, `busy`, `queue_len`, `queue_limit`], [online slots, requests in service, queued requests and queue limit],
    [`slots_per_node`, `node_idle_w`, `node_peak_w`], [mean slots, online idle and full-load IT power over all servers (model parameters)],
    [`pools[]`], [type, backend, servers per state, slots and power parameters of each pool],
    [`cap_ratio`, `cap_min_ratio`], [current power-cap ratio; the largest lower bound of the servers],
    [`it_power_w`, `facility_power_w`, `pue`], [IT and facility power; `None` when the power of any server is missing],
    [`power_scope`], [`node` (accelerators + host part) or `accelerators` (accelerators only)],
    [`energy_it_j`, `energy_coverage`], [IT energy accumulated over valid power; share of server-seconds with valid power],
    [`arrival_rate`, `est_service_s`], [estimates of arrival rate and mean service time],
    [`nodes[]`, `components`], [state and power of every server; health of dependencies such as power sensors (live)],
  ),
  caption: [Main fields of the state summary],
)

*Arrival rate.* The arrival rate is weighted exponentially with the time constant $tau$ =
`scheduler.rate_window_s`: after $Delta t$ with $n$ arrivals,
$lambda arrow.l lambda + (1 - e^(-Delta t \/ tau)) (n \/ Delta t - lambda)$. The estimate hardly depends
on the update interval, so simulation (updated every control period) and live experiments (updated by
the periodic task) agree. The mean service time is weighted exponentially per completed request (0.05).

== Energy metering

Power is constant between two changes and integrated per server: before every change of state,
occupancy or cap ratio the power before the change is integrated up to the current time. The server
model recomputes the power of emulated servers after every change; the site agent writes the measured
power of real servers after `advance(now)`. Intervals with missing power (`None`) are not integrated but
counted as missing time, and `energy_coverage` gives the share of time with valid power. At the end of
every time bin the simulator takes the differences of the accumulated meters to get the IT energy and
server time of the bin.
