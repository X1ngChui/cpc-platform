"""端到端冒烟测试（跨平台）：启动全部服务 → 压测 → 注入需求响应事件 → 检查指标 → 退出。

用法：
    python scripts/smoke_test.py                       # 本机单进程模式（cpc up）
    python scripts/smoke_test.py --external            # 服务已由 Docker Compose / Kubernetes 启动，只做检查
    python scripts/smoke_test.py --external -e configs/endpoints.compose-host.yaml

检查项：各服务健康；请求全部成功；控制器下发指令成功；站点导出 DCGM 兼容功率指标；
需求响应事件生效后站点设施功率不超过上限（留出一个控制周期与开关机时间）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cpc.config import load_endpoints, load_scenario  # noqa: E402


def wait_ok(url: str, timeout: float = 60) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"服务未就绪：{url}")


def check(cond: bool, msg: str) -> None:
    print(("  [通过] " if cond else "  [失败] ") + msg)
    if not cond:
        check.failed = True  # type: ignore[attr-defined]


check.failed = False  # type: ignore[attr-defined]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", default=str(ROOT / "configs/scenario.yaml"))
    ap.add_argument("-e", "--endpoints", default=None)
    ap.add_argument("--external", action="store_true", help="不启动服务，只检查已运行的服务")
    ap.add_argument("--duration", type=float, default=40)
    ap.add_argument("--rps", type=float, default=10)
    args = ap.parse_args()

    scn = load_scenario(args.scenario)
    ep = load_endpoints(args.endpoints, scn)
    proc = None
    if not args.external:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        cmd = [sys.executable, "-m", "cpc", "up", "-s", args.scenario, "--speedup", "60", "--interval", "5",
               "--routing", "weighted", "--capacity", "grid_aware", "--log-dir", str(ROOT / "logs/smoke")]
        print("启动：", " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        print("1. 健康检查")
        for name, url in [("grid", ep.grid), ("gateway", ep.gateway), ("controller", ep.controller), *ep.sites.items()]:
            wait_ok(url + "/healthz")
            print(f"  [通过] {name} {url}")

        print("2. 压测")
        out = subprocess.run([sys.executable, "-m", "cpc", "loadgen", "-s", args.scenario, *(["-e", args.endpoints] if args.endpoints else []),
                              "--duration", str(args.duration), "--rps", str(args.rps), "--out", str(ROOT / "logs/smoke/loadgen.jsonl")],
                             cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        summary = json.loads(out.stdout[out.stdout.index("{"):])
        print("  ", json.dumps(summary, ensure_ascii=False))
        check(summary["sent"] > 0 and summary["failed"] == 0, f"请求全部成功（{summary['ok']}/{summary['sent']}）")

        print("3. 控制器")
        st = httpx.get(ep.controller + "/state").json()
        applied = (st.get("last") or {}).get("applied", {})
        check(bool(applied) and all(applied.values()), f"容量指令下发成功：{applied}")

        print("4. 能耗感知指标")
        site0 = scn.sites[0].id
        m = httpx.get(ep.sites[site0] + "/metrics").text
        check("DCGM_FI_DEV_POWER_USAGE{" in m, "站点导出 DCGM 兼容 GPU 功率指标")
        check("cpc_site_facility_power_watts{" in m, "站点导出设施功率指标")
        g = httpx.get(ep.grid + "/metrics").text
        check("cpc_grid_price_yuan_per_kwh{" in g and "cpc_grid_carbon_kg_per_kwh{" in g, "电网信号服务导出电价与碳强度")

        print("5. 需求响应事件")
        target = scn.sites[-1].id
        snap = httpx.get(ep.sites[target] + "/state").json()
        cap_kw = round(snap["node_peak_w"] * 1.05 * snap["pue"] / 1000, 2)  # 只够 1 个节点满载
        r = httpx.post(ep.grid + "/v1/events", json={"site": target, "cap_kw": cap_kw, "duration_min": 600})
        check(r.status_code == 200, f"注入事件：站点 {target} 设施功率上限 {cap_kw} kW")
        ctl_interval = st.get("interval_s", 15)
        httpx.post(ep.controller + "/admin/step", timeout=10)
        time.sleep(min(ctl_interval, 5) + 2)
        snap = httpx.get(ep.sites[target] + "/state").json()
        live = snap["nodes_on"] + snap["nodes_booting"] + snap["nodes_draining"]
        check(live <= 1 or snap["nodes_draining"] > 0, f"站点 {target} 在线节点降至 {snap['nodes_on']}（排空中 {snap['nodes_draining']}）")
        check(snap["facility_power_w"] / 1000 <= cap_kw + 1e-6 or snap["nodes_draining"] > 0,
              f"设施功率 {snap['facility_power_w'] / 1000:.2f} kW ≤ 上限 {cap_kw} kW")
        httpx.delete(ep.grid + "/v1/events")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    print("冒烟测试" + ("失败" if check.failed else "通过"))  # type: ignore[attr-defined]
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
