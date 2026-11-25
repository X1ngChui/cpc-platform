"""Running the experiment services in one process (cpc up, cpc live): grid-signal service, site agents,
gateway and controller."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .config import Endpoints, Scenario, host_port


def build_apps(scn: Scenario, ep: Endpoints, log_dir: Path, *, routing: str | None = None, capacity: str | None = None,
               interval: float | None = None, initial_active: int | None = None, controller: bool = True) -> list[tuple]:
    """Build the service applications; returns [(app, url)]. Gateway and controller log to log_dir."""
    from .controller.service import create_app as ctl_app
    from .gateway.service import create_app as gw_app
    from .grid.service import create_app as grid_app
    from .site.service import create_app as site_app

    log_dir.mkdir(parents=True, exist_ok=True)
    apps = [(grid_app(scn), ep.grid),
            *((site_app(scn, s.id, initial_active=initial_active), ep.sites[s.id]) for s in scn.sites),
            (gw_app(scn, ep, routing, None, str(log_dir / "gateway.jsonl")), ep.gateway)]
    if controller:
        apps.append((ctl_app(scn, ep, capacity, None, interval, str(log_dir / "controller.jsonl")), ep.controller))
    return apps


class Running:
    """A group of started services."""

    def __init__(self, apps: list[tuple], host: str | None = None):
        import uvicorn

        self.apps = apps
        self.servers = [uvicorn.Server(uvicorn.Config(app, host=host or h, port=p, log_level="warning", access_log=False))
                        for app, url in apps for h, p in [host_port(url)]]
        self.tasks: dict[asyncio.Task, str] = {}

    async def start(self, timeout_s: float = 60.0) -> None:
        """Start all services and wait until they listen; if any fails, stop the others and raise RuntimeError."""
        self.tasks = {asyncio.create_task(s.serve()): app.title for s, (app, _) in zip(self.servers, self.apps)}
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not all(s.started for s in self.servers):
            if (failed := self.failed()) or asyncio.get_running_loop().time() > deadline:
                await self.stop()
                raise RuntimeError(f"services failed to start: {failed or 'timeout'}")
            await asyncio.sleep(0.1)

    def failed(self) -> list[str]:
        return [name for t, name in self.tasks.items() if t.done()]

    async def wait(self) -> list[str]:
        """Wait until any service exits; returns the services that exited."""
        done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        return [self.tasks[t] for t in done]

    async def stop(self) -> None:
        for s in self.servers:
            s.should_exit = True
        await asyncio.gather(*self.tasks, return_exceptions=True)
