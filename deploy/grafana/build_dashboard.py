"""生成 Grafana 仪表盘 JSON（deploy/grafana/dashboards/cpc-overview.json）。

用脚本生成而不是手工编辑 JSON，便于增删面板、统一配色。站点颜色固定（不随排序变化）：
站点按场景文件中的顺序依次使用分类色板的第 1–8 个颜色。

用法：python deploy/grafana/build_dashboard.py [-s configs/scenario.yaml]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# 分类色板（固定顺序；已通过相邻配对的色觉缺陷区分度校验）
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
        stat(1, "设施总功率", "global:facility_power_watts:sum", "watt", 0, desc="各站点 IT 功率 × PUE 之和"),
        stat(2, "用电成本速率（元/小时）", "global:energy_cost_yuan_per_hour", "none", 4, 2),
        stat(3, "碳排放速率（kg/小时）", "global:carbon_kg_per_hour", "none", 8, 2),
        stat(4, "请求速率", "sum(site:requests:rate1m)", "reqps", 12, 2),
        stat(5, "SLO 达成率（5 分钟）", "global:slo_attainment:ratio5m", "percentunit", 16, 3),
        stat(6, "信号时钟（时）", "cpc_grid_signal_hour", "none", 20, 2, desc="电网信号时间的小时数；演示时可加速"),
        ts(10, "站点设施功率", "site:facility_power_watts:sum", "watt", 0, 4, sites),
        ts(11, "分时电价（元/kWh）", "max by (site) (cpc_grid_price_yuan_per_kwh)", "none", 12, 4, sites, step=True),
        ts(12, "在线节点数", "max by (site) (cpc_site_nodes_on)", "none", 0, 12, sites, step=True),
        ts(13, "电力碳排放因子（kgCO2/kWh）", "max by (site) (cpc_grid_carbon_kg_per_kwh)", "none", 12, 12, sites),
        ts(14, "各站点请求速率", "site:requests:rate1m", "reqps", 0, 20, sites),
        ts(15, "首 token 时延 P99", "site:ttft_seconds:p99_1m", "s", 12, 20, sites),
        ts(16, "需求响应：功率上限余量", "site:power_cap_headroom_watts", "watt", 0, 28, sites,
           desc="设施功率上限 − 实际设施功率；为负表示越限。仅在有上限的时段显示"),
        ts(17, "每输出 token 设施能耗（J/token）", "site:joules_per_output_token:rate1m", "none", 12, 28, sites),
        ts(18, "排队请求数", "max by (site) (cpc_site_queue_length)", "none", 0, 36, sites),
        ts(19, "GPU 功率（按站点汇总，DCGM 兼容指标）", "site:gpu_power_watts:sum", "watt", 12, 36, sites),
    ]
    return {
        "uid": "cpc-overview", "title": "算电协同调度平台", "tags": ["cpc"], "timezone": "browser",
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
