#import "../../template/cpc-doc.typ": *

#show: doc.with(
  lang: "en",
  title: "Energy-aware AI Inference Scheduling across Data Centres",
  cover-title: [Energy-aware AI Inference \ Scheduling across Data Centres],
  subtitle: "Design document",
  doc-id: "CPC-DS-001",
  version: "v0.4",
  date: "2026-11-25",
  org: "Compute-power coordination research group",
  authors: ("Platform team",),
  revisions: (
    ("v0.1", "2026-11-24", "Platform team", "first draft"),
    ("v0.2", "2026-11-25", "Platform team", "datasets as descriptors and kind specifications; data-centre runtime shared by simulation and live experiments; two-level scheduling and server pools; result packages and experiment descriptions"),
    ("v0.3", "2026-11-25", "Platform team", "server types as tables only; unnecessary features removed; live runs (cpc live) write the result packages of simulations"),
    ("v0.4", "2026-11-25", "Platform team", "English edition; four built-in sensors; cpc measure and cpc compare; site configuration from the control node; fewer built-in policies and deployments, all extension points kept"),
  ),
  abstract: [
    This document describes the design of a platform for energy-aware scheduling of AI inference across
    data centres. The platform takes workloads, grid signals and power profiles as datasets described by
    descriptors and kind specifications, describes data centres, server pools and the grid in scenarios,
    organises scheduling by global (across data centres) and local (inside a data centre) policy
    interfaces, and supports both discrete-event simulation and live experiments with real inference
    instances on the same server models, data-centre runtime and policy implementations. It covers the
    data specification, scenarios and models, the data-centre runtime, policy interfaces, energy
    measurement and accounting, service interfaces, operation and result formats, and extension.
  ],
)

#include "ch1-intro.typ"
#include "ch2-arch.typ"
#include "ch3-data.typ"
#include "ch4-scenario.typ"
#include "ch5-runtime.typ"
#include "ch6-policies.typ"
#include "ch7-energy.typ"
#include "ch8-interfaces.typ"
#include "ch9-workflow.typ"
#include "ch10-extension.typ"
#include "refs.typ"
