"""容量执行器：把控制器的决策落到具体基础设施上。

- SiteAgentActuator（默认）：调用站点代理的 POST /admin/capacity，由站点代理负责开关节点、设置功率封顶。
  仿真站点与真实站点（vLLM + NVML + Docker）都走这一条路径。
- KubernetesActuator：直接伸缩 Kubernetes Deployment 的副本数（例如每个站点一个 vLLM Deployment），
  适合"一个副本 = 一个推理节点"的 Kubernetes 原生部署。需要安装 kubernetes Python 客户端；
  认证方式由 auth 显式指定（incluster：Pod 内的服务账号；kubeconfig：本机 kubeconfig），初始化失败即启动失败。

apply() 返回 (是否成功, 失败原因)。
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import ConfigError
from ..health import describe
from ..schemas import CapacityTarget

log = logging.getLogger("cpc.controller.actuator")


class SiteAgentActuator:
    name = "site-agent"

    def __init__(self, site_urls: dict[str, str], client: httpx.AsyncClient):
        self.urls = site_urls
        self.client = client

    async def apply(self, site: str, target: CapacityTarget) -> tuple[bool, str | None]:
        try:
            r = await self.client.post(self.urls[site] + "/admin/capacity", json=target.model_dump(), timeout=5.0)
        except httpx.HTTPError as e:
            return False, describe(e)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        return True, None


class KubernetesActuator:
    """把 active_nodes 映射为 Deployment 副本数。deployment_template 例："vllm-{site}"。"""

    name = "kubernetes"

    def __init__(self, namespace: str = "cpc", deployment_template: str = "vllm-{site}", auth: str = "incluster"):
        try:
            from kubernetes import client, config  # type: ignore
        except ImportError as e:
            raise ConfigError('Kubernetes 执行器需要安装 kubernetes 客户端（pip install "cpc-platform[k8s]"）') from e
        if auth == "incluster":
            config.load_incluster_config()
        elif auth == "kubeconfig":
            config.load_kube_config()
        else:
            raise ConfigError(f"Kubernetes 认证方式只能为 incluster 或 kubeconfig，当前为 {auth}")
        self.api = client.AppsV1Api()
        self.ns = namespace
        self.tpl = deployment_template

    async def apply(self, site: str, target: CapacityTarget) -> tuple[bool, str | None]:
        if target.active_nodes is None:
            return True, None
        name = self.tpl.format(site=site)
        body = {"spec": {"replicas": int(target.active_nodes)}}
        try:
            await asyncio.to_thread(self.api.patch_namespaced_deployment_scale, name, self.ns, body)
            return True, None
        except Exception as e:  # noqa: BLE001 —— kubernetes 客户端的异常类型多样；原因返回给控制器记录
            return False, describe(e)


def make_actuator(kind: str, site_urls: dict[str, str], client: httpx.AsyncClient, k8s_namespace: str = "cpc",
                  k8s_template: str = "vllm-{site}", k8s_auth: str = "incluster"):
    if kind == "site-agent":
        return SiteAgentActuator(site_urls, client)
    if kind == "kubernetes":
        return KubernetesActuator(k8s_namespace, k8s_template, k8s_auth)
    raise ConfigError(f"未知执行器 {kind}，可选：site-agent、kubernetes")
