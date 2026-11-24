"""功率来源（能耗感知的设备层）。

- ModelPower：按节点状态与占用率用功率模型计算（仿真后端）。
- NvmlPower：通过 NVIDIA 官方 NVML 绑定（nvidia-ml-py）读取真实 GPU 功率；
  主机（CPU、内存等）部分仍用模型估计，除非另行接入 IPMI/Redfish 等整机功率。
"""

from __future__ import annotations

import logging

from ..config import NodeProfile
from ..models import host_power_w, node_power_w
from .core import Node

log = logging.getLogger("cpc.site.power")


class ModelPower:
    name = "model"

    def __init__(self, profile: NodeProfile):
        self.p = profile

    def sample(self, nodes: list[Node]) -> None:
        for n in nodes:
            occ = n.busy / n.slots if n.slots else 0.0
            it, gpu = node_power_w(self.p, n.state, occ, n.cap_ratio)
            n.it_power_w, n.gpu_power_w = it, gpu
            per = gpu / n.gpus if n.gpus else 0.0
            n.gpu_powers_w = [per] * n.gpus
            util = 100.0 * occ if n.state in ("on", "draining") else 0.0
            n.gpu_utils = [util] * n.gpus
            n.power_source = "model"


class NvmlPower:
    """真实 GPU 功率。每个节点的 GPU 由 UpstreamConfig.gpu_indices 指定。

    注意：消费级显卡（GeForce）也支持 NVML 的功率读数；DCGM 则主要面向数据中心显卡。
    """

    name = "nvml"

    def __init__(self, profile: NodeProfile):
        import pynvml  # 来自 nvidia-ml-py

        self.nv = pynvml
        self.nv.nvmlInit()
        self.p = profile
        self._handles: dict[int, object] = {}

    def handle(self, idx: int):
        if idx not in self._handles:
            self._handles[idx] = self.nv.nvmlDeviceGetHandleByIndex(idx)
        return self._handles[idx]

    def read_gpu(self, idx: int) -> tuple[float, float]:
        h = self.handle(idx)
        p = self.nv.nvmlDeviceGetPowerUsage(h) / 1000.0  # mW → W
        u = float(self.nv.nvmlDeviceGetUtilizationRates(h).gpu)
        return p, u

    def sample(self, nodes: list[Node]) -> None:
        for n in nodes:
            idxs = n.upstream.gpu_indices if n.upstream else []
            occ = n.busy / n.slots if n.slots else 0.0
            if not idxs:  # 未绑定 GPU：退化为模型
                it, gpu = node_power_w(self.p, n.state, occ, n.cap_ratio)
                n.it_power_w, n.gpu_power_w, n.power_source = it, gpu, "model"
                n.gpu_powers_w, n.gpu_utils = [gpu], [100.0 * occ]
                continue
            powers, utils = [], []
            for i in idxs:
                try:
                    p, u = self.read_gpu(i)
                except Exception as e:  # noqa: BLE001 —— 采集失败不应中断服务
                    log.warning("NVML 读取 GPU %s 失败：%s", i, e)
                    p, u = 0.0, 0.0
                powers.append(p)
                utils.append(u)
            n.gpu_powers_w, n.gpu_utils = powers, utils
            n.gpu_power_w = sum(powers)
            # 主机部分：多个实例共用一台主机时，按实例数均摊由使用者在 profile 中设定
            host = 0.0 if n.state == "off" else host_power_w(self.p, occ)
            n.it_power_w = n.gpu_power_w + host
            n.power_source = "nvml+model"

    def set_power_limit(self, idx: int, ratio: float) -> None:
        """设置 GPU 功率上限（需要 root/管理员权限）。"""
        h = self.handle(idx)
        lo, hi = self.nv.nvmlDeviceGetPowerManagementLimitConstraints(h)
        target = int(max(lo, min(hi, ratio * hi)))
        self.nv.nvmlDeviceSetPowerManagementLimit(h, target)
