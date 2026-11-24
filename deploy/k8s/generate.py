"""根据场景文件生成 Kubernetes 清单（deploy/k8s/generated/*.yaml）。

拓扑：每个虚拟数据中心站点对应集群中带 topology.kubernetes.io/region=<站点 id> 标签的节点（kind 中为一个 worker），
站点代理 Pod 通过 nodeSelector 固定在该节点上；电网信号服务、网关、控制器、Prometheus、Grafana 放在控制面所在节点之外的任意节点。

用法：python deploy/k8s/generate.py [-s configs/scenario.yaml] [--image cpc-platform:latest]
然后：kubectl apply -f deploy/k8s/generated/
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NS = "cpc"


def svc(name: str, port: int, node_port: int | None = None) -> dict:
    spec = {"selector": {"app": name}, "ports": [{"name": "http", "port": port, "targetPort": port}]}
    if node_port:
        spec["type"] = "NodePort"
        spec["ports"][0]["nodePort"] = node_port
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS, "labels": {"app": name}}, "spec": spec}


def deploy(name: str, image: str, args: list[str], port: int, node_selector: dict | None = None, extra: dict | None = None,
           volumes: list | None = None, mounts: list | None = None, env: list | None = None, probe: str = "/readyz",
           liveness: str | None = "/healthz") -> dict:
    c = {
        "name": name, "image": image, "imagePullPolicy": "IfNotPresent", "args": args,
        "ports": [{"containerPort": port, "name": "http"}],
        "env": env or [{"name": "CPC_ENDPOINTS", "value": "/etc/cpc/endpoints.yaml"}],
        "volumeMounts": mounts if mounts is not None else [{"name": "cpc-endpoints", "mountPath": "/etc/cpc"}],
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
    }
    if probe:
        c["readinessProbe"] = {"httpGet": {"path": probe, "port": port}, "periodSeconds": 5}
    if liveness:
        c["livenessProbe"] = {"httpGet": {"path": liveness, "port": port}, "periodSeconds": 10, "failureThreshold": 3}
    c.update(extra or {})
    pod = {
        "containers": [c],
        "volumes": volumes if volumes is not None else [{"name": "cpc-endpoints", "configMap": {"name": "cpc-endpoints"}}],
    }
    if node_selector:
        pod["nodeSelector"] = node_selector
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": NS, "labels": {"app": name, "app.kubernetes.io/part-of": "cpc"}},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name, "app.kubernetes.io/part-of": "cpc"},
                                           "annotations": {"prometheus.io/scrape": "true", "prometheus.io/port": str(port)}},
                              "spec": pod}},
    }


def cm(name: str, data: dict[str, str]) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NS}, "data": data}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", default=str(ROOT / "configs/scenario.yaml"))
    ap.add_argument("--image", default="cpc-platform:latest")
    ap.add_argument("--speedup", default="60")
    ap.add_argument("--routing", default="nearest")
    ap.add_argument("--capacity", default="reactive")
    args = ap.parse_args()
    with open(args.scenario, encoding="utf-8") as f:
        scn = yaml.safe_load(f)
    sites = [s["id"] for s in scn["sites"]]
    scn_in_image = "/app/configs/" + Path(args.scenario).name
    out = HERE / "generated"
    out.mkdir(exist_ok=True)

    endpoints = {"grid": f"http://grid.{NS}.svc:9000", "gateway": f"http://gateway.{NS}.svc:8080",
                 "controller": f"http://controller.{NS}.svc:9300", "sites": {s: f"http://site-{s}.{NS}.svc:9100" for s in sites}}
    prom = {
        "global": {"scrape_interval": "5s", "evaluation_interval": "5s"},
        "rule_files": ["/etc/prometheus/rules/*.yml"],
        "scrape_configs": [
            {"job_name": "cpc-grid", "static_configs": [{"targets": [f"grid.{NS}.svc:9000"]}]},
            {"job_name": "cpc-site", "static_configs": [{"targets": [f"site-{s}.{NS}.svc:9100" for s in sites]}]},
            {"job_name": "cpc-gateway", "static_configs": [{"targets": [f"gateway.{NS}.svc:8080"]}]},
            {"job_name": "cpc-controller", "static_configs": [{"targets": [f"controller.{NS}.svc:9300"]}]},
        ],
    }
    rules = (ROOT / "deploy/prometheus/rules/cpc-energy.yml").read_text(encoding="utf-8")
    ds = (ROOT / "deploy/grafana/provisioning/datasources/prometheus.yml").read_text(encoding="utf-8").replace(
        "http://prometheus:9090", f"http://prometheus.{NS}.svc:9090")
    dbp = (ROOT / "deploy/grafana/provisioning/dashboards/cpc.yml").read_text(encoding="utf-8")
    dash = (ROOT / "deploy/grafana/dashboards/cpc-overview.json").read_text(encoding="utf-8")

    docs: dict[str, list[dict]] = {
        "00-namespace.yaml": [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS}}],
        "10-config.yaml": [
            cm("cpc-endpoints", {"endpoints.yaml": yaml.safe_dump(endpoints, sort_keys=False)}),
            cm("prometheus-config", {"prometheus.yml": yaml.safe_dump(prom, sort_keys=False)}),
            cm("prometheus-rules", {"cpc-energy.yml": rules}),
            cm("grafana-datasources", {"prometheus.yml": ds}),
            cm("grafana-dashboard-provider", {"cpc.yml": dbp}),
            cm("grafana-dashboards", {"cpc-overview.json": dash}),
        ],
        "20-grid.yaml": [
            deploy("grid", args.image, ["grid", "-s", scn_in_image, "--host", "0.0.0.0", "--speedup", args.speedup], 9000),
            svc("grid", 9000, 30900),
        ],
        "21-sites.yaml": [],
        "22-gateway.yaml": [
            deploy("gateway", args.image, ["gateway", "-s", scn_in_image, "--host", "0.0.0.0", "--policy", args.routing], 8080),
            svc("gateway", 8080, 30080),
        ],
        "23-controller.yaml": [
            deploy("controller", args.image, ["controller", "-s", scn_in_image, "--host", "0.0.0.0", "--policy", args.capacity], 9300),
            svc("controller", 9300, 30930),
        ],
        "30-monitoring.yaml": [
            deploy("prometheus", "prom/prometheus:v3.5.0",
                   ["--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.retention.time=2d"], 9090,
                   volumes=[{"name": "config", "configMap": {"name": "prometheus-config"}},
                            {"name": "rules", "configMap": {"name": "prometheus-rules"}}],
                   mounts=[{"name": "config", "mountPath": "/etc/prometheus/prometheus.yml", "subPath": "prometheus.yml"},
                           {"name": "rules", "mountPath": "/etc/prometheus/rules"}],
                   env=[], probe="/-/ready", liveness="/-/healthy"),
            svc("prometheus", 9090, 30090),
            deploy("grafana", "grafana/grafana:12.1.1", [], 3000,
                   volumes=[{"name": "ds", "configMap": {"name": "grafana-datasources"}},
                            {"name": "dbp", "configMap": {"name": "grafana-dashboard-provider"}},
                            {"name": "dash", "configMap": {"name": "grafana-dashboards"}}],
                   mounts=[{"name": "ds", "mountPath": "/etc/grafana/provisioning/datasources"},
                           {"name": "dbp", "mountPath": "/etc/grafana/provisioning/dashboards"},
                           {"name": "dash", "mountPath": "/var/lib/grafana/dashboards"}],
                   env=[{"name": "GF_AUTH_ANONYMOUS_ENABLED", "value": "true"},
                        {"name": "GF_AUTH_ANONYMOUS_ORG_ROLE", "value": "Viewer"},  # 匿名只读；管理员 admin/admin（首次登录后修改）
                        {"name": "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH", "value": "/var/lib/grafana/dashboards/cpc-overview.json"}],
                   probe="/api/health", liveness=None),
            svc("grafana", 3000, 30300),
        ],
    }
    site_ports = {s: 30101 + i for i, s in enumerate(sites)}  # 站点也暴露 NodePort，便于从宿主机检查
    for s in sites:
        docs["21-sites.yaml"] += [
            deploy(f"site-{s}", args.image, ["site", "-s", scn_in_image, "--site", s, "--host", "0.0.0.0", "--port", "9100"], 9100,
                   node_selector={"topology.kubernetes.io/region": s}),
            svc(f"site-{s}", 9100, site_ports[s]),
        ]
    for fn, objs in docs.items():
        (out / fn).write_text("---\n".join(yaml.safe_dump(o, sort_keys=False, allow_unicode=True) for o in objs), encoding="utf-8")
        print(f"written {out / fn}")

    # kind 集群配置：一个控制面 + 每个站点一个 worker（带 region 标签）；NodePort 映射到宿主机
    kind = {
        "kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4", "name": "cpc",
        "nodes": [{"role": "control-plane", "extraPortMappings": [
            {"containerPort": p, "hostPort": p, "protocol": "TCP"}
            for p in (30080, 30090, 30300, 30900, 30930, *site_ports.values())]}]
        + [{"role": "worker", "labels": {"topology.kubernetes.io/region": s, "cpc.io/site": s}} for s in sites],
    }
    (HERE / "kind-cluster.yaml").write_text("# 由 generate.py 生成\n" + yaml.safe_dump(kind, sort_keys=False), encoding="utf-8")
    print(f"written {HERE / 'kind-cluster.yaml'}")

    # 从宿主机访问 kind 中各服务的地址文件（供冒烟测试、负载生成器使用）
    host_ep = {"grid": "http://127.0.0.1:30900", "gateway": "http://127.0.0.1:30080", "controller": "http://127.0.0.1:30930",
               "sites": {s: f"http://127.0.0.1:{p}" for s, p in site_ports.items()}}
    ep_file = ROOT / "configs" / "endpoints.kind-host.yaml"
    ep_file.write_text("# 从宿主机访问 kind 集群中的服务（NodePort）；由 deploy/k8s/generate.py 生成\n"
                       + yaml.safe_dump(host_ep, sort_keys=False), encoding="utf-8")
    print(f"written {ep_file}")


if __name__ == "__main__":
    main()
