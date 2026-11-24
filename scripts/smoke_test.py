"""端到端冒烟测试：启动全部服务 → 压测 → 注入需求响应事件 → 检查指标 → 故障隔离 → 退出。

用法：
    python scripts/smoke_test.py                       # 本机：各服务以独立进程启动（含故障注入阶段），负载为场景的公开数据集
    python scripts/smoke_test.py --synthetic-workload  # 同上，负载换为合成数据集（不需要下载，CI 使用）
    python scripts/smoke_test.py --external -e configs/endpoints.compose-host.yaml   # 服务已由 Compose / Kubernetes 启动，只做 1–5

本机模式的场景由 configs/scenario.yaml 派生，另加一个真实后端站点 lab：其上游与功率计（http-json 传感器）
由本脚本内置的桩服务提供，用于注入传感器故障。

检查项：
1. 各服务就绪（/readyz）；
2. 压测请求全部成功；
3. 控制器完成决策并下发指令；
4. 站点导出 DCGM 兼容功率指标、设施功率指标，电网信号服务导出电价与碳强度，真实站点传感器正常；
5. 需求响应事件生效后站点在线节点与设施功率不超过上限；
6. 传感器故障：lab 的功率标记为缺失并在状态与指标中报告，lab 仍正常服务；
7. 站点故障：停止站点 sh 的进程后，网关与控制器将其标记为不可用并排除，其余站点正常服务。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml

http = httpx.Client(trust_env=False, timeout=10)  # 平台内部通信不走系统代理

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cpc.common import admin_headers  # noqa: E402
from cpc.config import load_endpoints, load_scenario  # noqa: E402

OUT = ROOT / "logs" / "smoke"
STUB_PORT = 9199


def check(cond: bool, msg: str) -> None:
    print(("  [通过] " if cond else "  [失败] ") + msg)
    if not cond:
        check.failed = True  # type: ignore[attr-defined]


check.failed = False  # type: ignore[attr-defined]


def wait_until(fn, timeout: float, interval: float = 0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except httpx.HTTPError:
            pass
        time.sleep(interval)
    return None


def wait_ready(name: str, url: str, timeout: float = 60) -> None:
    if not wait_until(lambda: http.get(url + "/readyz", timeout=1).status_code == 200, timeout):
        raise SystemExit(f"服务未就绪：{name} {url}")
    print(f"  [通过] {name} {url}")


def metric(url: str, name: str, **labels) -> float | None:
    from prometheus_client.parser import text_string_to_metric_families

    for fam in text_string_to_metric_families(http.get(url + "/metrics").text):
        for s in fam.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return None


# ------------------------------------------------------------------ 桩服务：OpenAI 兼容上游 + 功率计


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    meter_fail = False

    def log_message(self, *a) -> None:
        return None

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path == "/meter":
            if Stub.meter_fail:
                self._json(500, {"error": "meter offline"})
            else:
                self._json(200, {"power_w": 120.0})
        elif self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": "cpc", "object": "model"}]})
        else:
            self._json(404, {})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        n = int(body.get("max_tokens") or 16)
        usage = {"prompt_tokens": 8, "completion_tokens": n, "total_tokens": 8 + n}
        if not body.get("stream"):
            self._json(200, {"id": "stub", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant",
                             "content": "ok"}, "finish_reason": "length"}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(3):
            chunk = {"id": "stub", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": f"t{i} "}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(f"data: {json.dumps({'id': 'stub', 'choices': [], 'usage': usage})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def smoke_scenario(base: Path, synthetic: bool) -> Path:
    raw = yaml.safe_load(base.read_text(encoding="utf-8"))
    if synthetic:  # 合成负载（与单元测试相同的数据集与参数），到达率 6 请求/秒
        raw["workload"] = {**yaml.safe_load((ROOT / "tests/data/scenario.yaml").read_text(encoding="utf-8"))["workload"],
                           "rate_scale": 0.5}
    raw["sites"].append({
        "id": "lab", "name": "真实后端（桩服务）", "province": "guizhou", "lat": 26.6, "lon": 106.6, "pue": 1.2,
        "backend": "openai", "min_active": 1, "profile": next(iter(raw["node_profiles"])),
        "upstreams": [{"id": "lab-0", "url": f"http://127.0.0.1:{STUB_PORT}", "devices": ["gpu0"], "max_inflight": 32}],
        "power": {"sensors": [{"type": "http-json", "name": "meter", "url": f"http://127.0.0.1:{STUB_PORT}/meter",
                               "path": "power_w", "kind": "gpu", "device": "gpu0", "vendor": "test"}], "host": "excluded"},
    })
    p = base.parent / ".scenario.smoke.yaml"  # 与基础场景同目录，场景中的相对路径保持有效
    p.write_text("# 由 scripts/smoke_test.py 生成\n" + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


class Services:
    def __init__(self, scenario: Path):
        self.scenario = str(scenario)
        self.procs: dict[str, subprocess.Popen] = {}

    def start(self, name: str, *args: str) -> None:
        log = open(OUT / f"{name}.log", "w", encoding="utf-8")
        cmd = [sys.executable, "-m", "cpc", *args, "-s", self.scenario, "--local"]
        self.procs[name] = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)

    def stop(self, name: str) -> None:
        p = self.procs.pop(name)
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.stop(name)


# ------------------------------------------------------------------ 检查


def basic_checks(scn, ep, args, dr_site: str, real_site: str | None) -> None:
    print("2. 压测")
    out = subprocess.run([sys.executable, "-m", "cpc", "loadgen", "-s", args.scenario_used, "--gateway", ep.gateway, "--grid", ep.grid,
                          "--duration", str(args.duration), *(["--rate-scale", str(args.rate_scale)] if args.rate_scale else []),
                          "--out", str(OUT / "loadgen.jsonl")],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        print(out.stderr[-2000:])
    summary = json.loads(out.stdout[out.stdout.index("{"):])
    print("  ", json.dumps(summary, ensure_ascii=False))
    check(summary["sent"] > 0 and summary["failed"] == 0, f"请求全部成功（{summary['ok']}/{summary['sent']}）")

    print("3. 控制器")
    rec = http.post(ep.controller + "/admin/step", headers=admin_headers()).json()
    check(rec.get("result") == "applied", f"控制周期完成决策（result={rec.get('result')}，reason={rec.get('reason')}）")
    applied = rec.get("applied", {})
    check(bool(applied) and all(v["ok"] for v in applied.values()), f"容量指令下发成功：{ {k: v['ok'] for k, v in applied.items()} }")

    print("4. 能耗感知指标")
    m = http.get(ep.sites[dr_site] + "/metrics").text
    check("DCGM_FI_DEV_POWER_USAGE{" in m, "站点导出 DCGM 兼容 GPU 功率指标")
    check("cpc_site_facility_power_watts{" in m, "站点导出设施功率指标")
    g = http.get(ep.grid + "/metrics").text
    check("cpc_grid_price_yuan_per_kwh{" in g and "cpc_grid_carbon_kg_per_kwh{" in g, "电网信号服务导出电价与碳强度")
    if real_site:
        check(metric(ep.sites[real_site], "cpc_sensor_up", site=real_site, sensor="meter") == 1, f"真实站点 {real_site} 的功率计正常")

    print("5. 需求响应事件")
    snap = http.get(ep.sites[dr_site] + "/state").json()
    cap_kw = round(snap["node_peak_w"] * 1.05 * snap["pue"] / 1000, 2)  # 只够 1 个节点满载
    r = http.post(ep.grid + "/v1/events", json={"site": dr_site, "cap_kw": cap_kw, "duration_min": 600}, headers=admin_headers())
    check(r.status_code == 200, f"注入事件：站点 {dr_site} 设施功率上限 {cap_kw} kW")
    time.sleep(2.5)  # 网关与控制器刷新电网信号
    http.post(ep.controller + "/admin/step", timeout=10, headers=admin_headers())
    time.sleep(2)
    snap = http.get(ep.sites[dr_site] + "/state").json()
    live = snap["nodes_on"] + snap["nodes_booting"] + snap["nodes_draining"]
    check(live <= 1 or snap["nodes_draining"] > 0, f"站点 {dr_site} 在线节点降至 {snap['nodes_on']}（排空中 {snap['nodes_draining']}）")
    check(snap["facility_power_w"] / 1000 <= cap_kw + 1e-6 or snap["nodes_draining"] > 0,
          f"设施功率 {snap['facility_power_w'] / 1000:.2f} kW ≤ 上限 {cap_kw} kW")
    http.delete(ep.grid + "/v1/events", headers=admin_headers())


def fault_checks(scn, ep, svc: Services) -> None:
    print("6. 传感器故障（lab 的功率计不可用）")
    Stub.meter_fail = True
    st = wait_until(lambda: (s := http.get(ep.sites["lab"] + "/state").json())["it_power_w"] is None and s, 10)
    check(bool(st) and st["nodes"][0]["power_missing"] == ["device:gpu0"], "lab 节点功率标记为缺失（device:gpu0），未以其他值替代")
    check(bool(st) and st["components"]["sensors"]["meter"]["up"] is False, "lab 的 /state 报告功率计故障及原因")
    check(metric(ep.sites["lab"], "cpc_sensor_up", site="lab", sensor="meter") == 0, "指标 cpc_sensor_up{sensor=meter} = 0")
    check((metric(ep.sites["lab"], "cpc_sensor_errors_total", site="lab", sensor="meter") or 0) >= 1, "指标 cpc_sensor_errors_total 递增")
    r = http.post(ep.sites["lab"] + "/v1/chat/completions", json={"model": "cpc", "messages": [{"role": "user", "content": "x"}],
                                                                   "max_tokens": 4})
    check(r.status_code == 200, "lab 在功率计故障期间仍正常服务请求")
    Stub.meter_fail = False
    check(bool(wait_until(lambda: http.get(ep.sites["lab"] + "/state").json()["it_power_w"] is not None, 10)), "功率计恢复后读数恢复")

    print("7. 站点故障（停止站点 sh）")
    svc.stop("site-sh")
    down = wait_until(lambda: http.get(ep.gateway + "/state").json()["upstreams"]["site:sh"]["up"] is False, 15)
    check(bool(down), "网关将 sh 标记为不可用")
    check(metric(ep.gateway, "cpc_upstream_up", component="gateway", upstream="site:sh") == 0, "指标 cpc_upstream_up{upstream=site:sh} = 0")
    time.sleep(scn.scheduler.state_stale_s)
    codes, sites = [], set()
    for src in [s.id for s in scn.sources] * 5:
        r = http.post(ep.gateway + "/v1/chat/completions", headers={"x-cpc-source": src},
                      json={"model": "cpc", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 4})
        codes.append(r.status_code)
        sites.add(r.headers.get("x-cpc-site"))
    check(all(c == 200 for c in codes) and "sh" not in sites, f"其余站点正常服务（{codes.count(200)}/{len(codes)}，站点 {sorted(s for s in sites if s)}）")
    rec = http.post(ep.controller + "/admin/step", headers=admin_headers()).json()
    check("sh" in rec.get("excluded", {}), f"控制器排除 sh 并记录原因（result={rec.get('result')}）")
    check((metric(ep.controller, "cpc_controller_collect_errors_total", target="site:sh") or 0) >= 1, "指标 cpc_controller_collect_errors_total{target=site:sh} 递增")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", default=str(ROOT / "configs/scenario.yaml"))
    ap.add_argument("-e", "--endpoints", default=None)
    ap.add_argument("--external", action="store_true", help="不启动服务，只检查已运行的服务（检查项 1–5）")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--rate-scale", type=float, default=None, help="覆盖场景负载的到达率缩放")
    ap.add_argument("--synthetic-workload", action="store_true", help="负载换为合成数据集（不需要下载公开数据集）")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    if args.external:
        args.scenario_used = args.scenario
        scn = load_scenario(args.scenario)
        ep = load_endpoints(args.endpoints, scn)
        print("1. 就绪检查")
        for name, url in [("grid", ep.grid), ("gateway", ep.gateway), ("controller", ep.controller), *ep.sites.items()]:
            wait_ready(name, url)
        emulated = [s.id for s in scn.sites if s.backend == "emulated"]
        basic_checks(scn, ep, args, emulated[-1], None)
    else:
        stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        path = smoke_scenario(Path(args.scenario), args.synthetic_workload)
        args.scenario_used = str(path)
        scn = load_scenario(path)
        ep = load_endpoints(None, scn, local=True)
        svc = Services(path)
        try:
            print("1. 启动与就绪检查（各服务独立进程）")
            svc.start("grid", "grid", "--speedup", "60")
            wait_ready("grid", ep.grid)
            for s in scn.sites:
                svc.start(f"site-{s.id}", "site", "--site", s.id)
            for s in scn.sites:
                wait_ready(s.id, ep.sites[s.id])
            svc.start("gateway", "gateway", "--policy", "weighted", "--log", str(OUT / "gateway.jsonl"))
            svc.start("controller", "controller", "--policy", "grid_aware", "--interval", "5", "--log", str(OUT / "controller.jsonl"))
            wait_ready("gateway", ep.gateway)
            wait_ready("controller", ep.controller)
            basic_checks(scn, ep, args, "nm", "lab")
            fault_checks(scn, ep, svc)
        finally:
            svc.stop_all()
            stub.shutdown()
    print("冒烟测试" + ("失败" if check.failed else "通过"))  # type: ignore[attr-defined]
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
