"""容量执行器：把控制器的决策落到具体基础设施上。

- SiteAgentActuator（默认）：调用站点代理的 POST /admin/capacity，由站点代理负责开关节点、设置功率封顶。
  仿真站点与真实站点（vLLM + NVML + Docker）都走这一条路径。
- KubernetesActuator（可选）：直接伸缩 Kubernetes Deployment 的副本数（例如每个站点一个 vLLM Deployment），
  适合"一个副本 = 一个推理节点"的 Kubernetes 原生部署。需要安装 kubernetes Python 客户端。
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..schemas import CapacityTarget

log = logging.getLogger("cpc.controller.actuator")


class SiteAgentActuator:
    name = "site-agent"

    def __init__(self, site_urls: dict[str, str], client: httpx.AsyncClient):
        self.urls = site_urls
        self.client = client

    async def apply(self, site: str, target: CapacityTarget) -> bool:
        try:
            r = await self.client.post(self.urls[site] + "/admin/capacity", json=target.model_dump(), timeout=5.0)
            return r.status_code == 200
        except httpx.HTTPError as e:
            log.warning("apply to %s failed: %s", site, e)
            return False


class KubernetesActuator:
    """把 active_nodes 映射为 Deployment 副本数。deployment_template 例："vllm-{site}"。"""

    name = "kubernetes"

    def __init__(self, namespace: str = "cpc", deployment_template: str = "vllm-{site}"):
        from kubernetes import client, config  # type: ignore

        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001
            config.load_kube_config()
        self.api = client.AppsV1Api()
        self.ns = namespace
        self.tpl = deployment_template

    async def apply(self, site: str, target: CapacityTarget) -> bool:
        if target.active_nodes is None:
            return True
        name = self.tpl.format(site=site)
        body = {"spec": {"replicas": int(target.active_nodes)}}
        try:
            await asyncio.to_thread(self.api.patch_namespaced_deployment_scale, name, self.ns, body)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("scale %s/%s failed: %s", self.ns, name, e)
            return False
