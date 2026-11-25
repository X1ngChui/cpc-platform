"""Generate the Grafana dashboard JSON (deploy/grafana/dashboards/cpc-overview.json).

Generated rather than edited by hand, so panels and colours stay consistent. Site colours are fixed
(independent of sorting): sites take colours 1-8 of the categorical palette in scenario order.

Usage: python deploy/grafana/build_dashboard.py [-s configs/scenario.yaml]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# categorical palette (fixed order; adjacent pairs checked for colour-vision deficiency)
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
DS = {"type": "prometheus", "uid": "prometheus"}


def site_overrides(sites: list[str]) -> list[dict]:
    return [
        {"matcher": {"id": "byName", "options": s}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": PALETTE[i % 8]}}]}
        for i, s in enumerate(sites)
    ]


def ts(pid, title, expr, unit, x, y, sites, w=12, h=8, legend="{{site}}", step=False, desc=""):
    return {
        "id": pid, "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": "A", "expr": expr, "legendFormat": legend, "datasource": DS}],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"lineWidth": 2, "fillOpacity": 0, "showPoints": "never", "spanNulls": True,
                           "lineInterpolation": "stepAfter" if step else "linear", "axisSoftMin": 0},
                "color": {"mode": "palette-classic"},
            },
            "overrides": site_overrides(sites),
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def stat(pid, title, expr, unit, x, decimals=1, desc=""):
    return {
        "id": pid, "type": "stat", "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": 0, "w": 4, "h": 4},
        "targets": [{"refId": "A", "expr": expr, "datasource": DS}],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "color": {"mode": "fixed", "fixedColor": "#52514e"}}, "overrides": []},
        "options": {"colorMode": "none", "graphMode": "area", "reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "value"},
    }


def build(sites: list[str]) -> dict:
    p = [
        stat(1, "Facility power", "global:facility_power_watts:sum", "watt", 0, desc="sum of IT power x PUE over sites"),
        stat(2, "Energy cost per hour", "global:energy_cost_per_hour", "none", 4, 2),
        stat(3, "Carbon (kg per hour)", "global:carbon_kg_per_hour", "none", 8, 2),
        stat(4, "Request rate", "sum(site:requests:rate1m)", "reqps", 12, 2),
        stat(5, "SLO attainment (5 min)", "global:slo_attainment:ratio5m", "percentunit", 16, 3),
        stat(6, "Signal clock (hour)", "cpc_grid_signal_hour", "none", 20, 2, desc="hour of the grid-signal time; may be accelerated"),
        ts(10, "Facility power per site", "site:facility_power_watts:sum", "watt", 0, 4, sites),
        ts(11, "Price per kWh", "max by (site) (cpc_grid_price_per_kwh)", "none", 12, 4, sites, step=True),
        ts(12, "Online servers", "max by (site) (cpc_site_nodes_on)", "none", 0, 12, sites, step=True),
        ts(13, "Carbon intensity (kgCO2/kWh)", "max by (site) (cpc_grid_carbon_intensity_kg_per_kwh)", "none", 12, 12, sites),
        ts(14, "Request rate per site", "site:requests:rate1m", "reqps", 0, 20, sites),
        ts(15, "TTFT P99", "site:ttft_seconds:p99_1m", "s", 12, 20, sites),
        ts(16, "Demand response: power headroom", "site:power_cap_headroom_watts", "watt", 0, 28, sites,
           desc="facility power limit - facility power; negative values exceed the limit. Shown while a limit applies"),
        ts(17, "Facility energy per output token (J/token)", "site:joules_per_output_token:rate1m", "none", 12, 28, sites),
        ts(18, "Queued requests", "max by (site) (cpc_site_queue_length)", "none", 0, 36, sites),
        ts(19, "GPU power per site (DCGM-compatible metric)", "site:gpu_power_watts:sum", "watt", 12, 36, sites),
        ts(20, "Upstream availability (gateway / controller)", "min by (component, upstream) (cpc_upstream_up)", "none", 0, 44, sites,
           legend="{{component}} -> {{upstream}}", step=True, desc="1 healthy, 0 failed; unavailable sites leave the routing view and the control period"),
        ts(21, "Power sensor health", "min by (site, sensor) (cpc_sensor_up)", "none", 12, 44, sites,
           legend="{{site}} {{sensor}}", step=True, desc="1 healthy, 0 failed; missing readings make site power NaN, never another value"),
        ts(22, "Energy data coverage (share of time with valid power)", "max by (site) (cpc_site_energy_coverage_ratio)", "percentunit", 0, 52, sites),
        ts(23, "Gateway forwarding attempts", "sum by (outcome) (rate(cpc_gateway_attempts_total[1m]))", "reqps", 12, 52, sites,
           legend="{{outcome}}"),
    ]
    return {
        "uid": "cpc-overview", "title": "cpc platform", "tags": ["cpc"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "refresh": "5s", "time": {"from": "now-30m", "to": "now"},
        "panels": p, "templating": {"list": []}, "annotations": {"list": []}, "editable": True,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", default=str(ROOT / "configs/scenario.yaml"))
    args = ap.parse_args()
    with open(args.scenario, encoding="utf-8") as f:
        sites = [s["id"] for s in yaml.safe_load(f)["sites"]]
    out = HERE / "dashboards" / "cpc-overview.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(sites), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out}")


if __name__ == "__main__":
    main()
