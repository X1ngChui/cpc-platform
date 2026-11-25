#import "../../template/cpc-doc.typ": *

= Overview <ch-intro>

== Background

China's "Eastern Data, Western Computing" programme placed eight national computing hubs in
Beijing-Tianjin-Hebei, the Yangtze River Delta, the Guangdong-Hong Kong-Macao Greater Bay Area,
Chengdu-Chongqing, Inner Mongolia, Guizhou, Gansu and Ningxia @ndrc2021hub. Eastern hubs are close to
users but have higher electricity prices and less renewable energy; western hubs have cheaper electricity
and abundant renewable resources but are far from users. AI inference workloads can be shifted in time
and space to some extent: as long as latency objectives are met, requests can be routed to data centres in
different regions, and servers inside a data centre can be switched on and off or power-capped on
demand. A central question of compute-power coordination is how to use the price, carbon and flexibility
signals of the grid to schedule computing load across data centres.

Related work covers the limits of spatio-temporal load shifting @sukprasert2024limits, the power
characteristics of large-model inference @patel2024characterizing @stojkovic2025dynamollm and the
interaction of AI data centres with the grid @colangelo2026grid. Most studies use their own simulators
and data pipelines, so policies are hard to compare under the same conditions and simulation results are
hard to validate on real systems. This platform therefore provides one data specification, one set of
scheduling interfaces, and two modes of operation, simulation and live experiments, that share the same
models and policy implementations.

== Design goals

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("Goal", "Consequence for the design"),
    [Specified and general], [The core defines contracts only: data formats, state and decision structures, policy and device interfaces. Concrete data, algorithms and hardware are plugged in; new implementations do not modify the platform.],
    [Driven by data descriptions], [Datasets declare their structure and provenance in a descriptor. Each kind of data specifies the minimal core fields needed for basic scheduling; everything else is carried in extension fields, which can reach policies. Converting raw data into the platform format is the user's task; the platform provides validation and conversion examples.],
    [Shared by simulation and live experiments], [Scenarios, server models, the state machine inside data centres, global and local policies and the accounting of metrics are one implementation in both modes, which differ only in clock, request execution, power measurement, actions and communication.],
    [Two-level scheduling], [Global scheduling across data centres depends only on state summaries of the data centres; dispatch of requests and power management of servers inside a data centre are local policies. A data centre can be studied as a whole or in its internal scheduling.],
    [Explicit failures], [Configuration and environment errors are reported at startup; local faults at run time are confined to their component and exposed by state fields, health checks and metrics; missing data are marked missing and never replaced by estimates or defaults.],
    [Reproducible], [Results are written as data packages recording the scenario, policy parameters, dataset versions and platform version; random processes use explicit seeds.],
  ),
  caption: [Design goals],
)

== Scope

The platform includes: access to grid signals (price, carbon intensity, extra signals, demand response),
workload data and power profiles; models of data centres and servers; policy interfaces and reference
implementations for request routing and capacity management across data centres and for request dispatch
and power management inside data centres; discrete-event simulation; a live system of grid-signal
service, gateway, controller and site agents that connects real inference instances and power sensors;
metrics, logs and result packages.

Out of scope: the design and optimisation of particular scheduling algorithms (the platform provides
interfaces and reference implementations); scheduling of training and batch jobs (see @ch-ext for
extensions); power-flow computation and electricity-market clearing; cleaning rules for raw data (decided
by whoever uses the data).

== Terms

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("Term", "Meaning"),
    [site], [A data centre with server pools, a location, a PUE, a grid region and local policies.],
    [server], [A service unit in a site: an emulated server takes power and latency from the server model; a real server is an OpenAI-compatible inference instance (e.g. one vLLM process) with the accelerators it uses. Interfaces and metrics also call it node.],
    [pool], [The servers of one server type (an entry of `node_profiles`) in a site. A site may have several pools (heterogeneous).],
    [slot], [The number of requests a server processes concurrently at most, i.e. the largest continuous batch (e.g. `max_num_seqs` of vLLM).],
    [source region], [The geographic origin of user requests, used for the round-trip time (RTT) to each site.],
    [global / local policy], [Global policies decide across data centres (routing, capacity); local policies decide inside a data centre (dispatch, power management).],
    [state summary / instruction], [What a data centre reports to the global policies (`SiteSnapshot`); what the global policies set for a data centre (`CapacityTarget`: online servers, power-cap ratio).],
    [dataset descriptor], [The `datapackage.yaml` (or `.json`) of a dataset directory, declaring metadata, resources and fields (@ch-data).],
    [TTFT / TPOT], [Time to first token / mean time per output token.],
    [SLO], [Service-level objective; the scenario gives limits for TTFT and TPOT.],
    [IT / facility power], [Power drawn by the servers / IT power × PUE, the power the data centre takes from the grid.],
    [demand response (DR)], [The grid asks consumers to reduce load during given periods; the platform models it as a facility power limit of a site.],
    [signal clock], [The time of the grid signals. Live experiments may accelerate it (60: one real minute is one signal hour); in simulation it is the simulated time.],
  ),
  caption: [Terms],
)

== Conventions

This document describes the design of the platform: concepts, data and interface contracts, mechanisms
and extension points. Installation, data preparation, simulation, deployment and operation are described
in the guide (CPC-UG-001). Configuration fields are written as paths in the scenario file (YAML), e.g.
`sites[].pools[].profile`; code locations as Python modules, e.g. `cpc.datacenter`.
