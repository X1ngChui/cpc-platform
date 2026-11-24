"""仿真结果作图（matplotlib，输出 PNG，供实验报告/文档使用）。

配色：站点按场景顺序固定使用分类色板（不随排序或筛选变化）；同一张图只用一个 y 轴，
不同量纲分成上下对齐的小图（small multiples）；网格与坐标轴保持低对比。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from ..config import CST, Scenario  # noqa: E402

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"


def setup_style() -> None:
    plt.rcParams.update({
        "font.sans-serif": ["Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC",
                            "WenQuanYi Micro Hei", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.titlecolor": INK, "axes.titlesize": 11,
        "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.labelsize": 9,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelsize": 8.5, "ytick.labelsize": 8.5,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
        "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False, "legend.fontsize": 8.5,
        "lines.linewidth": 1.8, "figure.dpi": 100, "savefig.dpi": 200,
    })


def _site_colors(scn: Scenario) -> dict[str, str]:
    return {s.id: PALETTE[i % len(PALETTE)] for i, s in enumerate(scn.sites)}


def _label_ends(ax, xs, series: dict[str, list[float]], names: dict[str, str]) -> None:
    """在线条末端做直接标注（文字用正文墨色；颜色身份由图例与线条本身承担）。"""
    lo, hi = ax.get_ylim()
    gap = (hi - lo) * 0.08
    placed: list[float] = []
    for y, sid in sorted(((vals[-1], sid) for sid, vals in series.items()), reverse=True):
        for p in placed:
            if abs(y - p) < gap:
                y = p - gap
        placed.append(y)
        ax.annotate(names[sid], xy=(xs[-1], y), xytext=(6, 0), textcoords="offset points", va="center",
                    fontsize=8, color=INK2, annotation_clip=False)


def plot_run(run_dir: str | Path, scn: Scenario) -> Path:
    setup_style()
    run_dir = Path(run_dir)
    with open(run_dir / "timeseries.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    t = [float(r["t_h"]) for r in rows]
    colors = _site_colors(scn)
    names = {s.id: s.id for s in scn.sites}
    fig, axes = plt.subplots(3, 1, figsize=(8.2, 7.4), sharex=True, gridspec_kw={"hspace": 0.32})
    specs = [("it_kw", "各站点 IT 功率（kW）", False), ("nodes", "各站点在线节点数（含启动中）", True), ("price", "分时电价（元/kWh）", True)]
    for ax, (key, title, step) in zip(axes, specs):
        series = {}
        for sid in colors:
            ys = [float(r[f"{sid}_{key}"]) for r in rows]
            series[sid] = ys
            if step:
                ax.step(t, ys, where="post", color=colors[sid], label=sid)
            else:
                ax.plot(t, ys, color=colors[sid], label=sid)
        ax.set_title(title)
        ax.set_ylim(bottom=0)
        _label_ends(ax, t, series, names)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, ncol=len(colors), loc="lower center", bbox_to_anchor=(0.5, -0.01), handlelength=1.6)
    axes[-1].set_xlabel("时刻（h）")
    axes[-1].set_xticks(range(0, int(max(t)) + 2, 3))
    fig.suptitle(f"路由策略 {summary['routing']} ＋ 容量策略 {summary['capacity']}", x=0.01, ha="left", fontsize=12, color=INK)
    out = run_dir / "run.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_comparison(cmp_dir: str | Path) -> Path:
    setup_style()
    cmp_dir = Path(cmp_dir)
    with open(cmp_dir / "comparison.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    labels = [f"{r['routing']} + {r['capacity']}" for r in rows]
    metrics = [("cost_yuan", "电费（元）", "{:.0f}"), ("facility_kwh", "设施用电量（kWh）", "{:.0f}"),
               ("carbon_kg", "碳排放（kgCO2）", "{:.0f}"), ("slo_attainment", "SLO 达成率", "{:.2%}")]
    fig, axes = plt.subplots(1, 4, figsize=(11, 0.55 * len(rows) + 1.4), sharey=True, gridspec_kw={"wspace": 0.12})
    y = list(range(len(rows)))[::-1]
    for ax, (key, title, fmt) in zip(axes, metrics):
        vals = [float(r[key]) for r in rows]
        ax.barh(y, vals, height=0.55, color=PALETTE[0])
        for yi, v in zip(y, vals):
            ax.text(v, yi, " " + fmt.format(v), va="center", fontsize=8, color=INK2)
        ax.set_title(title, fontsize=10)
        ax.grid(axis="y", visible=False)
        if key == "slo_attainment":
            ax.set_xlim(0, 1.3)
            ax.set_xticks([0, 0.5, 1.0])
        else:
            ax.set_xlim(0, max(vals) * 1.35 if max(vals) > 0 else 1)
        ax.tick_params(axis="x", labelsize=7.5)
    axes[0].set_yticks(y)
    axes[0].set_yticklabels(labels, color=INK2)
    out = cmp_dir / "comparison.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_signals(scn: Scenario, day: datetime | None, out: str | Path) -> Path:
    from ..grid.signals import GridModel

    setup_style()
    g = GridModel(scn)
    day = (day or scn.clock.start).astimezone(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    ts = [day + timedelta(minutes=10 * i) for i in range(24 * 6 + 1)]
    hours = [i / 6 for i in range(len(ts))]
    colors = _site_colors(scn)
    names = {s.id: f"{s.id}（{s.name}）" for s in scn.sites}
    fig, axes = plt.subplots(2, 1, figsize=(8.2, 5.6), sharex=True, gridspec_kw={"hspace": 0.3})
    for key, ax, title in (("price", axes[0], "分时电价（元/kWh）"), ("carbon", axes[1], "电力碳排放因子（kgCO2/kWh，省级年均值叠加示意日内形状）")):
        series = {}
        for sid in colors:
            sig = [g.signal(sid, t) for t in ts]
            ys = [x.price_yuan_per_kwh if key == "price" else x.carbon_kg_per_kwh for x in sig]
            series[sid] = ys
            if key == "price":
                ax.step(hours, ys, where="post", color=colors[sid], label=names[sid])
            else:
                ax.plot(hours, ys, color=colors[sid], label=names[sid])
        ax.set_title(title, fontsize=10)
        ax.set_ylim(bottom=0)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, ncol=len(colors), loc="lower center", bbox_to_anchor=(0.5, -0.04), handlelength=1.6)
    axes[1].set_xlabel(f"时刻（h），{day:%Y-%m-%d}")
    axes[1].set_xticks(range(0, 25, 3))
    axes[1].set_xlim(0, 24)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out
