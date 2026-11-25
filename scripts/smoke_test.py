"""End-to-end smoke test: start all services -> load -> demand-response event -> metrics -> fault isolation -> exit.

Usage:
    python scripts/smoke_test.py             # local: one process per service (with fault injection);
                                             # scenario configs/scenario.yaml (its datasets must be prepared)
    python scripts/smoke_test.py --offline   # as above with tests/data/scenario.yaml (synthetic workload and
                                             # grid data committed with the tests; used by CI)
    python scripts/smoke_test.py --external -e endpoints.yaml -s scenario.yaml
                                             # services already running elsewhere; checks 1-5 only

Local mode derives the scenario from the base scenario and adds a site lab with one real server whose
inference instance and power meter are stubs served by this script (the meter is read by a command
sensor), so that sensor faults can be injected.

Checks:
1. every service is ready (/readyz);
2. a short live experiment (cpc live) serves every request;
3. the controller decides and applies instructions;
4. sites export server and facility power (real servers also DCGM-compatible accelerator metrics);
   the grid-signal service exports price and carbon intensity;
5. after a demand-response event the online servers and facility power of the site respect the limit;
6. sensor fault: the power of lab is reported missing in state and metrics while lab keeps serving;
7. site fault: after the process of site sh stops, gateway and controller mark it unavailable and
   exclude it, and the other sites keep serving.
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

http = httpx.Client(trust_env=False, timeout=10)  # no system proxy between platform services

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cpc import read_run  # noqa: E402
from cpc.common import admin_headers  # noqa: E402
from cpc.config import load_endpoints, load_scenario  # noqa: E402

OUT = ROOT / "logs" / "smoke"
STUB_PORT = 9199
# reads the stub meter; a 500 from the stub makes the command exit non-zero and the sensor fail
METER_CMD = f"import urllib.request as u; print(u.urlopen('http://127.0.0.1:{STUB_PORT}/meter', timeout=2).read().decode())"
CHAT = {"model": "cpc", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 4}
failed = False


def check(cond: bool, msg: str) -> None:
    global failed
    print(("  [ok]   " if cond else "  [FAIL] ") + msg)
    failed |= not cond


def wait_until(fn, timeout: float, interval: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if v := fn():
                return v
        except httpx.HTTPError:
            pass
        time.sleep(interval)
    return None


def wait_ready(name: str, url: str, timeout: float = 60) -> None:
    if not wait_until(lambda: http.get(url + "/readyz", timeout=1).status_code == 200, timeout):
        raise SystemExit(f"service not ready: {name} {url}")
    print(f"  [ok]   {name} {url}")


def metric(url: str, name: str, **labels) -> float | None:
    from prometheus_client.parser import text_string_to_metric_families

    for fam in text_string_to_metric_families(http.get(url + "/metrics").text):
        for s in fam.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return None


# ------------------------------------------------------------------ stubs: OpenAI-compatible upstream + power meter


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
        match self.path:
            case "/health":
                self._json(200, {"status": "ok"})
            case "/meter":
                self._json(500, {"error": "meter offline"}) if Stub.meter_fail else self._json(200, {"power_w": 120.0})
            case "/v1/models":
                self._json(200, {"object": "list", "data": [{"id": "cpc", "object": "model"}]})
            case _:
                self._json(404, {})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        n = int(body.get("max_tokens") or 16)
        usage = {"prompt_tokens": 8, "completion_tokens": n, "total_tokens": 8 + n}
        if not body.get("stream"):
            self._json(200, {"id": "stub", "object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "length"}]})
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


def smoke_scenario(base: Path, offline: bool) -> Path:
    raw = yaml.safe_load(base.read_text(encoding="utf-8"))
    if offline:  # synthetic workload 12 requests/s x 0.5
        sys.path.insert(0, str(ROOT / "tests"))
        from conftest import ensure_test_workload

        ensure_test_workload()
        raw["workload"]["rate_scale"] = 0.5
    raw["sites"].append({
        "id": "lab", "name": "real server (stub)", "region": "guizhou", "lat": 26.6, "lon": 106.6, "pue": 1.2, "min_active": 1,
        "pools": [{"id": "gpu", "profile": next(iter(raw["node_profiles"])), "backend": "openai",
                   "upstreams": [{"id": "lab-0", "url": f"http://127.0.0.1:{STUB_PORT}", "devices": ["gpu0"], "max_inflight": 32}]}],
        "measurement": {"sensors": [{"type": "command", "name": "meter", "kind": "gpu", "device": "gpu0", "vendor": "test",
                                     "cmd": [sys.executable, "-c", METER_CMD], "regex": r'"power_w":\s*([0-9.]+)'}],
                        "host": "excluded"},
    })
    p = base.parent / ".scenario.smoke.yaml"  # next to the base scenario, so its relative paths stay valid
    p.write_text("# generated by scripts/smoke_test.py\n" + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return p


class Services:
    def __init__(self, scenario: Path):
        self.scenario = str(scenario)
        self.procs: dict[str, subprocess.Popen] = {}

    def start(self, name: str, *args: str) -> None:
        log = (OUT / f"{name}.log").open("w", encoding="utf-8")
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


# ------------------------------------------------------------------ checks


def basic_checks(scn, ep, endpoints_file: Path, args, dr_site: str, real_site: str | None) -> None:
    print("2. Live experiment (cpc live)")
    out = OUT / "live"
    cmd = [sys.executable, "-m", "cpc", "live", "-s", args.scenario_used, "-e", str(endpoints_file),
           "--duration", str(args.duration), "--sample-s", "5", "--out", str(out),
           *(["--rate-scale", str(args.rate_scale)] if args.rate_scale else [])]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        print(p.stdout[-2000:], p.stderr[-2000:])
        check(False, "cpc live finished")
        return
    s = read_run(out)["summary"]
    print(f"     arrived {s['arrived']}, served {s['served']}, failed {s['failed']}, "
          f"TTFT P50 {s['ttft_p50_s'] or 0:.3f} s, facility energy {s['facility_kwh']:.4f} kWh")
    check(s["arrived"] > 0 and s["served"] == s["arrived"], f"every request served ({s['served']}/{s['arrived']})")

    print("3. Controller")
    rec = http.post(ep.controller + "/admin/step", headers=admin_headers()).json()
    check(rec.get("result") == "applied", f"control period applied (result={rec.get('result')}, reason={rec.get('reason')})")
    applied = rec.get("applied", {})
    check(bool(applied) and all(v["ok"] for v in applied.values()),
          f"capacity instructions applied: { {k: v['ok'] for k, v in applied.items()} }")

    print("4. Energy metrics")
    m = http.get(ep.sites[dr_site] + "/metrics").text
    check("cpc_node_it_power_watts{" in m, "sites export server power")
    check("cpc_site_facility_power_watts{" in m, "sites export facility power")
    g = http.get(ep.grid + "/metrics").text
    check("cpc_grid_price_per_kwh{" in g and "cpc_grid_carbon_intensity_kg_per_kwh{" in g,
          "the grid-signal service exports price and carbon intensity")
    if real_site:
        check(metric(ep.sites[real_site], "cpc_sensor_up", site=real_site, sensor="meter") == 1,
              f"the power meter of real site {real_site} is healthy")
        check("DCGM_FI_DEV_POWER_USAGE{" in http.get(ep.sites[real_site] + "/metrics").text,
              f"real site {real_site} exports DCGM-compatible accelerator power")

    print("5. Demand-response event")
    snap = http.get(ep.sites[dr_site] + "/state").json()
    cap_kw = round(snap["node_peak_w"] * 1.05 * snap["pue"] / 1000, 2)  # room for one server at full load
    r = http.post(ep.grid + "/v1/events", json={"site": dr_site, "cap_kw": cap_kw, "duration_min": 600}, headers=admin_headers())
    check(r.status_code == 200, f"event injected: facility limit of site {dr_site} {cap_kw} kW")
    time.sleep(2.5)  # gateway and controller refresh the grid signals
    http.post(ep.controller + "/admin/step", timeout=10, headers=admin_headers())
    time.sleep(2)
    snap = http.get(ep.sites[dr_site] + "/state").json()
    live = snap["nodes_on"] + snap["nodes_booting"] + snap["nodes_draining"]
    check(live <= 1 or snap["nodes_draining"] > 0,
          f"site {dr_site} reduced to {snap['nodes_on']} online servers ({snap['nodes_draining']} draining)")
    check(snap["facility_power_w"] / 1000 <= cap_kw + 1e-6 or snap["nodes_draining"] > 0,
          f"facility power {snap['facility_power_w'] / 1000:.2f} kW <= limit {cap_kw} kW")
    http.delete(ep.grid + "/v1/events", headers=admin_headers())


def fault_checks(scn, ep, svc: Services) -> None:
    lab = ep.sites["lab"]
    print("6. Sensor fault (the power meter of lab is unavailable)")
    Stub.meter_fail = True
    st = wait_until(lambda: (s := http.get(lab + "/state").json())["it_power_w"] is None and s, 10)
    check(bool(st) and st["nodes"][0]["power_missing"] == ["device:gpu0"],
          "the power of lab is missing (device:gpu0), not replaced by another value")
    check(bool(st) and st["components"]["sensors"]["meter"]["up"] is False, "/state of lab reports the meter fault and its reason")
    check(metric(lab, "cpc_sensor_up", site="lab", sensor="meter") == 0, "metric cpc_sensor_up{sensor=meter} = 0")
    check((metric(lab, "cpc_sensor_errors_total", site="lab", sensor="meter") or 0) >= 1, "metric cpc_sensor_errors_total increases")
    check(http.post(lab + "/v1/chat/completions", json=CHAT).status_code == 200, "lab keeps serving during the meter fault")
    Stub.meter_fail = False
    check(bool(wait_until(lambda: http.get(lab + "/state").json()["it_power_w"] is not None, 10)), "readings return with the meter")

    print("7. Site fault (site sh stopped)")
    svc.stop("site-sh")
    check(bool(wait_until(lambda: http.get(ep.gateway + "/state").json()["upstreams"]["site:sh"]["up"] is False, 15)),
          "the gateway marks sh unavailable")
    check(metric(ep.gateway, "cpc_upstream_up", component="gateway", upstream="site:sh") == 0,
          "metric cpc_upstream_up{upstream=site:sh} = 0")
    time.sleep(scn.scheduler.state_stale_s)
    rs = [http.post(ep.gateway + "/v1/chat/completions", headers={"x-cpc-source": src}, json=CHAT)
          for src in [s.id for s in scn.sources] * 5]
    sites = {r.headers.get("x-cpc-site") for r in rs} - {None}
    check(all(r.status_code == 200 for r in rs) and "sh" not in sites,
          f"the other sites keep serving ({sum(r.status_code == 200 for r in rs)}/{len(rs)}, sites {sorted(sites)})")
    rec = http.post(ep.controller + "/admin/step", headers=admin_headers()).json()
    check("sh" in rec.get("excluded", {}), f"the controller excludes sh with the reason (result={rec.get('result')})")
    check((metric(ep.controller, "cpc_controller_collect_errors_total", target="site:sh") or 0) >= 1,
          "metric cpc_controller_collect_errors_total{target=site:sh} increases")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", default=str(ROOT / "configs/scenario.yaml"))
    ap.add_argument("-e", "--endpoints", default=None)
    ap.add_argument("--external", action="store_true", help="do not start services; check running ones (checks 1-5)")
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--rate-scale", type=float, default=None, help="arrival-rate factor instead of the scenario's")
    ap.add_argument("--offline", action="store_true", help="base on the test scenario (synthetic workload and committed grid "
                                                           "data; no download)")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    if args.external:
        args.scenario_used = args.scenario
        scn = load_scenario(args.scenario)
        ep = load_endpoints(args.endpoints, scn)
        print("1. Readiness")
        for name, url in [("grid", ep.grid), ("gateway", ep.gateway), ("controller", ep.controller), *ep.sites.items()]:
            wait_ready(name, url)
        basic_checks(scn, ep, Path(args.endpoints), args, [s.id for s in scn.sites if not s.real][-1], None)
    else:
        stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        path = smoke_scenario(ROOT / "tests/data/scenario.yaml" if args.offline else Path(args.scenario), args.offline)
        args.scenario_used = str(path)
        scn = load_scenario(path)
        ep = load_endpoints(None, scn, local=True)
        endpoints_file = OUT / "endpoints.yaml"
        endpoints_file.write_text(yaml.safe_dump(ep.model_dump()), encoding="utf-8")
        svc = Services(path)
        try:
            print("1. Startup and readiness (one process per service)")
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
            basic_checks(scn, ep, endpoints_file, args, "nm", "lab")
            fault_checks(scn, ep, svc)
        finally:
            svc.stop_all()
            stub.shutdown()
    print("Smoke test " + ("FAILED" if failed else "passed"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
