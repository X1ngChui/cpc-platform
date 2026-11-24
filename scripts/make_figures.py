"""生成文档插图：电网信号日曲线、策略对比、单次运行曲线。

用法：python scripts/make_figures.py [--hours 24]
输出：docs/figures/*.png；仿真原始结果在 results/docs-compare/。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cpc.config import load_scenario  # noqa: E402
from cpc.sim.report import plot_comparison, plot_run, plot_signals  # noqa: E402

RUNS = "nearest:static,least_loaded:reactive,cost_aware:reactive,weighted:grid_aware"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--skip-sim", action="store_true")
    args = ap.parse_args()
    scn_path = ROOT / "configs/scenario.yaml"
    scn = load_scenario(scn_path)
    fig_dir = ROOT / "docs/figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    print(plot_signals(scn, None, fig_dir / "signals.png"))
    out = ROOT / "results/docs-compare"
    if not args.skip_sim:
        subprocess.run([sys.executable, "-m", "cpc", "compare", "-s", str(scn_path), "--runs", RUNS, "--hours", str(args.hours),
                        "--out", str(out)], check=True, cwd=ROOT)
    shutil.copy(plot_comparison(out), fig_dir / "comparison.png")
    for combo in ("nearest__static", "weighted__grid_aware"):
        shutil.copy(plot_run(out / combo, scn), fig_dir / f"run-{combo}.png")
    print("figures written to", fig_dir)


if __name__ == "__main__":
    main()
