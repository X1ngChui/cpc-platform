"""在一个进程中启动实验服务（cpc up、cpc live 使用）：电网信号服务、各站点代理、网关与控制器。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .config import Endpoints, Scenario, _host_port


def build_apps(scn: Scenario, ep: Endpoints, log_dir: Path, *, routing: str | None = None, capacity: str | None = None,
               interval: float | None = None, initial_active: int | None = None, controller: bool = True) -> list[tuple]:
    """构造各服务的应用；返回 [(应用, 地址)]。网关与控制器的日志写入 log_dir。"""
    from .controller.service import create_app as ctl_app
    from .gateway.service import create_app as gw_app
    from .grid.service import create_app as grid_app
    from .site.service import create_app as site_app

    log_dir.mkdir(parents=True, exist_ok=True)
    apps = [(grid_app(scn), ep.grid)]
    apps += [(site_app(scn, s.id, initial_active=initial_active), ep.sites[s.id]) for s in scn.sites]
    apps.append((gw_app(scn, ep, routing, None, str(log_dir / "gateway.jsonl")), ep.gateway))
    if controller:
        apps.append((ctl_app(scn, ep, capacity, None, interval, str(log_dir / "controller.jsonl")), ep.controller))
    return apps


class Running:
    """已启动的一组服务。"""

    def __init__(self, apps: list[tuple], host: str | None = None):
        import uvicorn

        self.apps = apps
        self.servers = []
        for app, url in apps:
            h, p = _host_port(url)
            self.servers.append(uvicorn.Server(uvicorn.Config(app, host=host or h, port=p, log_level="warning", access_log=False)))
        self.tasks: dict[asyncio.Task, str] = {}

    async def start(self, timeout_s: float = 60.0) -> None:
        """启动全部服务并等待其开始监听；任一服务启动失败时停止其余服务并抛出 RuntimeError。"""
        self.tasks = {asyncio.create_task(s.serve()): app.title for s, (app, _) in zip(self.servers, self.apps)}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not all(s.started for s in self.servers):
            failed = self.failed()
            if failed or loop.time() > deadline:
                await self.stop()
                raise RuntimeError(f"服务启动失败：{failed or '启动超时'}")
            await asyncio.sleep(0.1)

    def failed(self) -> list[str]:
        return [name for t, name in self.tasks.items() if t.done()]

    async def wait(self) -> list[str]:
        """等待任一服务退出；返回已退出的服务。"""
        done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        return [self.tasks[t] for t in done]

    async def stop(self) -> None:
        for s in self.servers:
            s.should_exit = True
        await asyncio.gather(*self.tasks, return_exceptions=True)
