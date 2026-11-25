#import "../../template/cpc-doc.typ": *

= Service interfaces <ch-api>

All services are FastAPI applications; `/docs` of a running service shows the generated OpenAPI
documentation for interactive use. Interfaces marked "admin" require the header
`Authorization: Bearer <token>` when the environment variable `CPC_ADMIN_TOKEN` is set; HTTP clients inside
the platform bypass the system proxy. Every service provides `/healthz` (liveness: 200 while the process
responds) and `/readyz` (readiness: 503 with the reason when not ready); `components` or `upstreams` in
`/state` give the health of each dependency (`up`, `errors_total`, `last_error`, `last_ok_age_s`).

== Site agent

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("Method", "Path", "Description"),
    [POST], [`/v1/chat/completions`], [OpenAI-compatible chat, `stream` supported. Headers `x-cpc-source` and `x-cpc-attr-<field>` (JSON values) become request information for the dispatch policy. Response headers: `x-cpc-site`, `x-cpc-node` (server), `x-cpc-queue-s` (queueing time in the site); non-streaming responses also `x-cpc-service-s` (service time). A full queue returns 503 (`type: overloaded`); sites with emulated servers return 400 for requests without `max_tokens`. Streaming responses send their headers after the first chunk from the upstream: connection failures and upstream errors return their status (502, 400, ...); once the stream has started, an upstream that breaks off or stalls longer than `scheduler.stream_idle_timeout_s` ends it with an error event.],
    [POST], [`/v1/completions`], [OpenAI-compatible completions, as above.],
    [GET], [`/v1/models`], [models (forwarded to an online inference instance when the site has real servers).],
    [GET], [`/state`], [state summary `SiteSnapshot` (@ch-runtime) with the health of dependencies such as sensors in `components`.],
    [POST], [`/admin/capacity` (admin)], [instruction `{"active_nodes": integer or null, "cap_ratio": ratio or null}`; returns the summary after applying it.],
    [GET], [`/readyz`], [200 when the startup checks passed and the periodic work (state, power sampling) runs.],
    [GET], [`/metrics`, `/healthz`], [Prometheus metrics, liveness.],
  ),
  caption: [Site-agent interface],
)

Emulated servers handle requests as follows: input tokens are the larger of whitespace words and
characters / 4, output tokens are `max_tokens` (the replay of `cpc live` also sets `ignore_eos: true` of
vLLM so that real models produce the same length); the server waits as the server model says and returns
placeholder text.

== Routing gateway

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("Method", "Path", "Description"),
    [POST], [`/v1/chat/completions`, `/v1/completions`], [route and forward. The header `x-cpc-source` names the source region (without it the request belongs to `network.default_source`; 400 if that is not set or the source is unknown); `x-cpc-attr-*` headers are request attributes for the routing policy and are forwarded to the site. The site's `x-cpc-*` response headers are passed through, plus `x-cpc-route-reason`. A rejection by the routing policy returns 503 (`type: route_rejected` with `reason`); retryable failures that exhaust the attempts 503 (`retries_exhausted`); forwarding failures that are not retried 502 (`upstream_error`); error responses list the attempts in `attempts`. Streams with an error event, without a final `data: [DONE]` or stalling longer than `scheduler.stream_idle_timeout_s` count as 502 in the request log and metrics.],
    [GET], [`/v1/models`], [forwarded to the usable site with the smallest id; 503 without one.],
    [GET], [`/state`], [demand statistics (arrival rate per source `rates`, rate per site `site_rates`), current policy and weights, RTT matrix, load and signal availability of the sites in the routing view, upstream health `upstreams`.],
    [GET], [`/readyz`], [200 when at least one site state is usable, listing the usable sites and the signal state.],
    [PUT], [`/admin/policy` (admin)], [switch the routing policy: `{"policy": name, "params": {...}}`.],
    [PUT], [`/admin/weights` (admin)], [set routing weights: `{"weights": {source: {site: non-negative weight}}}`; sources and sites must exist in the scenario.],
  ),
  caption: [Gateway interface],
)

== Capacity controller

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("Method", "Path", "Description"),
    [GET], [`/state`], [current policy and the inputs it requires (`requires`), control period, upstream health, the last period (`result` `applied` or `skipped`, with the reason and excluded sites).],
    [GET], [`/readyz`], [200 once the signal clock is synchronised.],
    [POST], [`/admin/step` (admin)], [run a control period now and return its record.],
    [PUT], [`/admin/policy` (admin)], [switch the capacity policy.],
  ),
  caption: [Controller interface],
)

== Grid-signal service

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("Method", "Path", "Description"),
    [GET], [`/v1/clock`], [signal clock: start, speed-up, wall-clock epoch, current signal time.],
    [GET], [`/readyz`], [datasets are loaded and checked at startup, so a running service returns 200.],
    [GET], [`/v1/signals[?at=time with UTC offset]`], [`GridSignal` of every site; `missing` lists the sites without signals and why, `sources` the dataset of each signal (`name@version:signal`).],
    [GET], [`/v1/events`], [registered demand-response events.],
    [POST], [`/v1/events` (admin)], [inject an event: `{"site", "cap_kw", "start"/"end" or "duration_min", "kind"}`.],
    [DELETE], [`/v1/events[?site=]` (admin)], [clear all events or those of a site.],
    [GET], [`/v1/config` (admin)], [text of the scenario file and the service addresses (`{"scenario", "endpoints"}`). Site agents on other machines started with `cpc site --control <address>` fetch their configuration here, so scenario and endpoints are maintained on the control node only.],
  ),
  caption: [Grid-signal service interface],
)

== Main data structures

The fields of the state summary `SiteSnapshot` are in @ch-runtime; the instruction `CapacityTarget` has
`active_nodes` (servers to keep online) and `cap_ratio` (power-cap ratio), and `null` leaves an item unchanged.

`GridSignal` has `site`, `time`, `price`, `price_label`, `carbon_intensity`, `currency`, `power_cap_kw`,
`dr_event`, `pue`, `carbon_price`, `extra` (extra signals, `null` when unavailable) and the computed
property `effective_price` (price + carbon price × carbon intensity).

== Endpoints file

```yaml
grid: http://grid:9000
gateway: http://gateway:8080
controller: http://controller:9300
sites:
  sh: http://site-sh:9100
  gd: http://site-gd:9100
```

`-e` or the environment variable `CPC_ENDPOINTS` names the endpoints file, which must cover all sites of the
scenario. Locally, `--local` uses the local addresses explicitly (signals 9000, gateway 8080, controller
9300, sites from 9101 in scenario order); `cpc up` without `-e` uses the same addresses.

== Environment variables

#figure(
  tbl(
    columns: (11em, 1fr),
    header: ("Variable", "Meaning"),
    [`CPC_DATA_DIR`], [dataset directory (value of `${CPC_DATA_DIR}` in scenarios), default `~/.cache/cpc/datasets`],
    [`CPC_ENDPOINTS`], [endpoints file],
    [`CPC_ADMIN_TOKEN`], [admin token; when set, admin interfaces require `Authorization: Bearer <token>`],
    [`CPC_LOG_LEVEL`, `CPC_LOG_FORMAT`], [log level; log format (`json` for structured logs)],
    [`HF_TOKEN`], [Hugging Face token used by conversion examples to download gated raw data (ML.ENERGY Benchmark)],
  ),
  caption: [Environment variables],
)
