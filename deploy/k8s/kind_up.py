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
import tempfile
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CLUSTER = "cpc"
IMAGES = ["cpc-platform:latest", "prom/prometheus:v3.5.0", "grafana/grafana:12.1.1"]


def sh(*cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check)


def load_image(img: str) -> None:
    """kind load docker-image 对 Docker Desktop（containerd 镜像库）中的多架构镜像会失败，
    失败时改为导出单一平台的镜像包再用 kind load image-archive 加载。"""
    if subprocess.run(["kind", "load", "docker-image", img, "--name", CLUSTER], cwd=ROOT).returncode == 0:
        return
    arch = subprocess.run(["docker", "info", "--format", "{{.Architecture}}"], capture_output=True, text=True).stdout.strip()
    plat = "linux/arm64" if arch in ("aarch64", "arm64") else "linux/amd64"
    sh("docker", "pull", "--platform", plat, img)
    with tempfile.TemporaryDirectory() as d:
        tar = str(Path(d) / "image.tar")
        sh("docker", "save", "--platform", plat, "-o", tar, img)
        sh("kind", "load", "image-archive", tar, "--name", CLUSTER)


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
    # 所有镜像都在宿主机准备好后加载进 kind 节点，节点本身不需要访问镜像仓库
    # （宿主机使用 127.0.0.1 上的代理时，kind 节点继承的代理地址不可达，直接拉取会失败）。
    for img in IMAGES:
        load_image(img)
    sh("kubectl", "--context", f"kind-{CLUSTER}", "apply", "-f", str(HERE / "generated"))
    sh("kubectl", "--context", f"kind-{CLUSTER}", "-n", "cpc", "rollout", "restart", "deployment")
    sh("kubectl", "--context", f"kind-{CLUSTER}", "-n", "cpc", "wait", "--for=condition=available", "deployment", "--all", "--timeout=180s")
    sh("kubectl", "--context", f"kind-{CLUSTER}", "-n", "cpc", "get", "pods", "-o", "wide")
    print("\n就绪：网关 http://localhost:30080  Grafana http://localhost:30300  Prometheus http://localhost:30090")
    print("检查：python scripts/smoke_test.py --external -e configs/endpoints.kind-host.yaml")


if __name__ == "__main__":
    main()
