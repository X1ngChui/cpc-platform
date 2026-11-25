#import "../../template/cpc-doc.typ": *

#show: doc.with(
  lang: "en",
  title: "Energy-aware AI Inference Scheduling across Data Centres",
  cover-title: [Energy-aware AI Inference \ Scheduling across Data Centres],
  subtitle: "User guide",
  doc-id: "CPC-UG-001",
  version: "v0.4",
  date: "2026-11-25",
  org: "Compute-power coordination research group",
  authors: ("Platform team",),
  revisions: (
    ("v0.1", "2026-11-24", "Platform team", "first draft"),
    ("v0.2", "2026-11-25", "Platform team", "data preparation by conversion scripts and dataset validation; result packages and experiment descriptions; server pools and real servers"),
    ("v0.3", "2026-11-25", "Platform team", "reorganised along the workflow: quick start, data, scenarios, simulation, live experiments, policies; cpc check, cpc live, cpc profile"),
    ("v0.4", "2026-11-25", "Platform team", [English edition; `cpc measure`, `cpc compare` and `cpc live --local`; cloud deployment with CPU inference and power capping; local Compose and Kubernetes deployments removed]),
  ),
  abstract: [
    This guide describes installing and using the platform: preparing datasets from public or your own
    data, writing scenarios, running simulations and analysing results, running live experiments with real
    inference instances and power sensors, writing your own scheduling policies, and observation, operation
    and development. Every chapter gives complete commands and configuration examples in the order of the
    workflow. The design of the platform is described in the design document (CPC-DS-001).
  ],
)

#include "ch1-start.typ"
#include "ch2-data.typ"
#include "ch3-scenario.typ"
#include "ch4-sim.typ"
#include "ch5-live.typ"
#include "ch6-policy.typ"
#include "ch7-ops.typ"
#include "ch8-dev.typ"
