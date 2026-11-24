"""一键在本机创建 kind 集群并部署平台（Windows / Linux / macOS 通用）。

前置：Docker、kind、kubectl 已安装并在 PATH 中。
用法：
    python deploy/k8s/kind_up.py            # 创建集群、构建并加载镜像、部署、等待就绪
    python deploy/k8s/kind_up.py --down     # 删除集群
访问：网关 http://localhost:30080，Grafana http://localhost:30300，Prometheus http://localhost:30090
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CLUSTER = "cpc"
IMAGES = ["cpc-platform:latest", "prom/prometheus:v3.5.0", "grafana/grafana:12.1.1"]


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--down", action="store_true")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()
    if args.down:
        sh("kind", "delete", "cluster", "--name", CLUSTER)
        return
    sh(sys.executable, str(HERE / "generate.py"))
    clusters = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True).stdout.split()
    if CLUSTER not in clusters:
        sh("kind", "create", "cluster", "--config", str(HERE / "kind-cluster.yaml"), "--wait", "120s")
    if not args.skip_build:
        sh("docker", "build", "-t", "cpc-platform:latest", ".")
    for img in IMAGES[1:]:
        if subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode != 0:
            sh("docker", "pull", img)
    for img in IMAGES:
        sh("kind", "load", "docker-image", img, "--name", CLUSTER)
    sh("kubectl", "--context", f"kind-{CLUSTER}", "apply", "-f", str(HERE / "generated"))
    sh("kubectl", "--context", f"kind-{CLUSTER}", "-n", "cpc", "rollout", "restart", "deployment")
    sh("kubectl", "--context", f"kind-{CLUSTER}", "-n", "cpc", "wait", "--for=condition=available", "deployment", "--all", "--timeout=180s")
    sh("kubectl", "--context", f"kind-{CLUSTER}", "-n", "cpc", "get", "pods", "-o", "wide")
    print("\n就绪：网关 http://localhost:30080  Grafana http://localhost:30300  Prometheus http://localhost:30090")
    print("检查：python scripts/smoke_test.py --external -e configs/endpoints.kind-host.yaml")


if __name__ == "__main__":
    main()
