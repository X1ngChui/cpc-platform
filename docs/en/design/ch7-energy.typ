#import "../../template/cpc-doc.typ": *

= Energy measurement and accounting <ch-energy>

Power and energy are obtained at four levels: sensors give device readings; the server level attributes
readings to servers (emulated servers take power from the server model, @sec-server-model); the facility
level converts to facility power with the PUE; the grid level converts to cost and carbon with prices and
carbon intensities.

== Sensors

=== Common reading

All sensors return the same reading `Reading`:

#figure(
  tbl(
    columns: (6em, 1fr),
    header: ("Field", "Meaning"),
    [`sensor`], [sensor instance, e.g. `nvml`, `rapl`, `ipmi`],
    [`device`], [device identifier, unique within the sensor, e.g. `gpu0`, `package-0`, `npu0-0`, `server`],
    [`kind`], [device class: `gpu`, `npu`, `cpu`, `dram`, `soc`, `server` (whole server), `platform` (platform total), `other`],
    [`vendor`, `model`], [vendor and model],
    [`power_w`], [power (W); depending on the interface an instantaneous value or a short average of the driver],
    [`energy_j`], [accumulated energy (J): the hardware counter when there is one, otherwise integrated from power above],
    [`util_pct`], [utilisation (%) if available],
    [`errors`], [fields that failed in this reading and why; failed fields are `None`],
  ),
  caption: [Power reading],
)

New hardware needs one `Sensor` subclass with a `read()` method, registered as an entry point in the group
`cpc.sensors` (@ch-ext); the site agent and the command line (`cpc sensors`, `cpc measure`) obtain power
through this layer only. Sensors handle errors as follows:

- the constructor determines the devices and the interface of every field (capability probing, reported
  by `capabilities()`); a missing required capability raises `SensorError` and stops the site agent. For
  NVML, for instance, the power reading is required, the energy counter (Volta and later GPUs) and
  utilisation are optional;
- `read()` raises when the whole reading fails (command failed, interface unreachable, unparsable output),
  and the caller records it in the health of the sensor;
- a failing device or field is `None` with the reason in `errors`; the first sample of a counter has no
  predecessor, so its power is `None` without being an error.

=== Supported sensors

#figure(
  tbl(
    columns: (6.5em, 10em, 1fr),
    header: ("Type", "Measures", "Interface"),
    [`nvml`], [NVIDIA GPUs], [NVIDIA's Python bindings of NVML, nvidia-ml-py: power, energy counter, utilisation, power-limit setting],
    [`rapl`], [CPU packages and memory (Intel / AMD)], [kernel powercap sysfs (RAPL energy counters, wrap-around handled)],
    [`ipmi`], [whole server (local BMC)], [`ipmitool dcmi power reading` (through `/dev/ipmi0`)],
    [`command`], [any other device], [subprocess + regular expression: a vendor management tool or the output of a collector; the reading kind is configured],
  ),
  caption: [Built-in power sensors (Linux)],
)

Each built-in sensor measures one class of objects (accelerators, CPU and memory, whole server) without
overlap, and all were verified on bare-metal servers. Other hardware uses `command` or a `Sensor` subclass
registered in the entry-point group `cpc.sensors` (@ch-ext).

All sensors accept the generic parameter `max_w` (plausible upper bound of power readings): larger readings
count as missing with the reason and are not replaced, which removes sporadic glitches of BMCs and the like.
`cpc sensors --list` lists the sensor types and whether this host meets their prerequisites;
`cpc sensors --types nvml,rapl` reads the given sensors and prints the readings. The platform never
chooses sensors by itself: the sensors of a site are configured explicitly, and no device should be
reported by two sensors (the site agent checks for duplicate accelerator names at startup).

=== Choice of collectors

In real environments the site agent reads sensors directly; deployed standard exporters remain useful for
monitoring:

#figure(
  tbl(
    columns: (9em, 1fr, 1fr),
    header: ("Collector", "Use", "Notes"),
    [NVIDIA dcgm-exporter], [data-centre GPUs on Kubernetes / Linux], [part of the NVIDIA GPU Operator with the most complete metrics; limited support for consumer GPUs. The GPU metrics of the platform have the same names.],
    [Kepler (CNCF)], [CPU energy of Kubernetes nodes and containers], [based on RAPL and models, attributes energy to containers/pods; for GPUs DCGM remains preferable.],
    [node_exporter], [Linux hosts], [`node_rapl_*` metrics expose RAPL energy counters (bare metal and permissions needed).],
    [ipmi_exporter / Redfish], [whole-server power], [total server power including fans and power-supply losses; long sampling periods (seconds).],
    [Zeus], [energy of code sections in Python programs], [measures the energy of single tasks inside training or inference code @you2023zeus.],
  ),
  caption: [Power collectors],
)

=== Measurement error and sampling

Built-in GPU power sensors have errors and sampling gaps: a study of more than 70 GPUs found errors of
about ±5 % for nvidia-smi, with only about 25 % of the runtime sampled on A100 and H100 @yang2023parttime.
Sampling periods also differ widely: 100 ms and more for DCGM, 1-5 s for IPMI, about 2 s for rack meters
@patel2024characterizing. Therefore:

- the energy over a period should come from hardware energy counters (`nvmlDeviceGetTotalEnergyConsumption`
  of NVML, `energy_uj` of RAPL) rather than integrated power readings; the platform's `EnergyIntegrator`
  uses the counter of devices that have one and handles counter drops (driver reloads);
- the energy of a single short (sub-second) request cannot be measured accurately; averages are total
  energy over a period divided by the tokens processed (see @sec-accounting);
- results should state the power source (model or sensor type) and the sampling period; the `source`
  label of the server power metric records the source.

== Servers: attribution of measured power

`measurement` of a site gives the sensors and how the host part is determined; the site agent attributes
readings to real servers as follows (`cpc.site.power.SensorPower`):

1. accelerator readings (gpu, npu, soc) go to the server that declares the device in `devices` (or
   `gpu_indices`); servers without accelerators (CPU-only inference instances) have the host part only;
2. the host part (CPU, memory, mainboard, fans, ...) follows `measurement.host`: `server` is the
   whole-server reading (`ipmi`, or a PDU through `command`, ...) minus all local accelerator readings;
   `cpu_dram` is the sum of CPU and memory readings (RAPL, ...) without mainboard, fans and power-supply
   losses; `excluded` does not count the host part, so server IT power covers accelerators only and
   `power_scope` of the summary is `accelerators`;
3. the host part is shared equally among the real servers that are not off (among all of them when all are off);
4. when any component is missing, the power of the affected servers is `None` and the missing parts (e.g.
   `device:gpu0`, `host`) are listed in `power_missing`; site IT power is `None` when any server is missing.
   Missing readings are never replaced by the server model or another sensor.

```yaml
measurement:
  sensors: [{type: nvml}, {type: ipmi, name: bmc}]   # several sensors of one type differ by name
  host: server                                       # server / cpu_dram / excluded
```

== Facility: PUE

Facility power = IT power × PUE, with the PUE of each site given in the scenario. The "Eastern Data,
Western Computing" programme targets a PUE of at most 1.25 for eastern and 1.2 for western hubs, and the
special action plan for green, low-carbon data centres requires a PUE of at most 1.2 for national hub data
centres by the end of 2025 @ndrc2024dc. Measured cooling and distribution power can be connected through
`command` or a registered sensor.

== Standard metrics <sec-metrics>

Services expose Prometheus metrics; the main ones are listed below, the complete lists at `/metrics` of
each service.

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("Metric", "Meaning"),
    [`DCGM_FI_DEV_POWER_USAGE`], [GPU power of real servers (W), named as by dcgm-exporter; labels `gpu`, `modelName`, `Hostname`, `site`],
    [`DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION`], [GPU accumulated energy (mJ)],
    [`cpc_node_it_power_watts`], [server IT power; labels `pool`, `source` (model / sensors); NaN when readings are missing],
    [`cpc_node_power_valid`, `cpc_site_energy_coverage_ratio`], [whether the server power reading is complete; share of time with valid site power],
    [`cpc_sensor_up`, `cpc_sensor_errors_total`], [whether the last reading succeeded without field errors; failed readings],
    [`cpc_node_state`], [server state (0 off / 1 booting / 2 on / 3 draining)],
    [`cpc_site_it_power_watts`, `cpc_site_facility_power_watts`], [site IT and facility power],
    [`cpc_site_nodes_on`, `cpc_site_queue_length`], [online servers, queued requests],
    [`cpc_grid_price_per_kwh`, `cpc_grid_carbon_intensity_kg_per_kwh`], [price (label `currency`), carbon intensity],
    [`cpc_grid_extra_signal`], [extra signals (label `signal`)],
    [`cpc_grid_power_cap_kw`, `cpc_grid_dr_event_active`], [facility power limit (−1 without one), whether a demand-response event is active],
    [`cpc_gateway_ttft_seconds`, `cpc_gateway_e2e_seconds`], [histograms of TTFT and end-to-end latency (by source and site)],
    [`cpc_gateway_slo_total`], [SLO counts (`result` = met / violated)],
  ),
  caption: [Main metrics],
)

The Prometheus recording rules (`deploy/prometheus/rules/cpc-energy.yml`) aggregate them into standard
metrics named after the Prometheus convention `level:metric:operations`. Dashboards and analyses depend on
these standard metrics only, not on particular collectors:

#figure(
  tbl(
    columns: (18em, 1fr),
    header: ("Recording rule", "Computation"),
    [`site:gpu_power_watts:sum`], [GPU power per site],
    [`site:facility_power_watts:sum`], [facility power per site],
    [`site:energy_cost_per_hour`], [facility power (kW) × price: cost rate (currency of the price per hour)],
    [`site:carbon_kg_per_hour`], [facility power (kW) × carbon intensity],
    [`site:power_cap_headroom_watts`], [power limit − facility power (demand-response headroom)],
    [`site:joules_per_output_token:rate1m`], [facility power ÷ output token rate: facility energy per output token],
    [`global:slo_attainment:ratio5m`], [global SLO attainment (5-minute window)],
  ),
  caption: [Standard metrics (recording rules)],
)

== Accounting <sec-accounting>

- *Site energy*: the data-centre runtime integrates IT energy per server (@ch-runtime); intervals with
  missing power are not integrated but counted as missing time. Live experiments export
  `cpc_node_energy_joules_total` and `cpc_node_energy_missing_seconds_total`, and `energy_coverage` of the
  summary is the share of time with valid power. The `EnergyIntegrator` of the sensor layer uses energy
  counters where devices have them and handles counter resets.
- *Cost and carbon*: the simulator accumulates IT energy per time bin (5 minutes by default), multiplies by
  the PUE for facility energy, then by the price and carbon intensity at the middle of the bin; `cpc live`
  takes the difference of accumulated site energy per sampling interval and multiplies by the PUE and the
  price and carbon intensity at the start of the interval. Intervals without signals are not accounted;
  `signal_coverage` of the summary gives the share of facility energy with signals. The recording rules
  also give cost and carbon rates.
- *Energy per request*: the energy of a short request cannot be measured on its own, so the platform gives
  no per-request energy; energy per request or per token is site energy over a period divided by the
  requests completed or tokens produced (`wh_per_request` of the summary).
