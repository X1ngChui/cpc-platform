"""数据集抽象：清单（出处、许可、校验和、引用）、缓存目录、下载与校验、注册表。

数据集分三类（kind）：
    workload       推理负载：请求到达时间、输入/输出 token 数，可选模型名、会话标识（cpc.datasets.workload）
    grid_signal    电网信号：按区域与时间的电价或碳强度（cpc.datasets.grid）
    power_profile  功率特性：设备型号、负载（批大小 / 吞吐）与功率的对应关系（cpc.datasets.power）

文件来源：
    远程文件（RemoteFile）下载到缓存目录 $CPC_DATA_DIR（缺省 ~/.cache/cpc/datasets）下的 <名称>/<版本>/，
    不提交入库；随包文件（BundledFile）位于 cpc/datasets/bundled/，是从公开文件整理的小型数据（如政策文件中的电价时段）。
    每个文件在清单中记录 SHA-256，加载前校验；文件缺失或校验失败时抛出 DatasetError，并给出获取方式。
    平台不自动下载，也不以合成数据替代：下载由 cpc data fetch 显式执行。

需要认证的文件（auth="hf"）从环境变量 HF_TOKEN 读取 Hugging Face 访问令牌。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal

log = logging.getLogger("cpc.datasets")

Kind = Literal["workload", "grid_signal", "power_profile"]
BUNDLED = resources.files("cpc.datasets") / "bundled"


class DatasetError(Exception):
    """数据集不可用：未注册、文件缺失、校验失败、下载失败或缺少认证。"""


def data_dir() -> Path:
    """数据集缓存目录：环境变量 CPC_DATA_DIR，缺省 ~/.cache/cpc/datasets。"""
    return Path(os.environ.get("CPC_DATA_DIR") or Path.home() / ".cache" / "cpc" / "datasets")


@dataclass(frozen=True)
class RemoteFile:
    name: str  # 缓存目录中的文件名
    url: str  # 固定到版本（提交、发布标签或修订号）的下载地址
    sha256: str
    size: int
    auth: Literal["hf"] | None = None


@dataclass(frozen=True)
class BundledFile:
    name: str  # cpc/datasets/bundled/ 下的相对路径
    sha256: str
    origin: str  # 整理所依据的原始文件或文献


@dataclass(frozen=True)
class Manifest:
    name: str
    version: str
    kind: Kind
    title: str
    source: str  # 主页或发布页
    license: str
    citation: str
    files: tuple[RemoteFile, ...] = ()
    bundled: tuple[BundledFile, ...] = ()
    notes: str = ""
    synthetic: bool = False  # 合成数据集：只用于单元测试与示例，不作为实验数据

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


class Dataset:
    """数据集基类。子类给出类属性 manifest，并实现所属类别的加载接口。"""

    manifest: ClassVar[Manifest]

    def __init__(self, root: Path | None = None, **params: Any):
        self.root = root or data_dir()
        self.params = params
        self._verified: set[str] = set()

    # ------------------------------------------------------------ 文件位置

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def dir(self) -> Path:
        return self.root / self.manifest.name / self.manifest.version

    def path(self, name: str) -> Path:
        """文件路径（随包文件位于包内，远程文件位于缓存目录）。使用前须经 verify() 校验。"""
        for b in self.manifest.bundled:
            if b.name == name:
                return Path(str(BUNDLED / b.name))
        for f in self.manifest.files:
            if f.name == name:
                return self.dir / f.name
        raise DatasetError(f"数据集 {self.name} 没有文件 {name}")

    @property
    def ref(self) -> str:
        return f"{self.manifest.name}@{self.manifest.version}"

    # ------------------------------------------------------------ 校验

    def _stamp(self, p: Path) -> Path:
        return p.with_name(p.name + ".sha256")

    def _check(self, p: Path, sha: str) -> None:
        """校验文件的 SHA-256。大文件的校验结果按（大小, 修改时间）缓存在旁边的 .sha256 文件中。"""
        if not p.is_file():
            raise DatasetError(f"数据集 {self.ref} 缺少文件 {p}；请运行 cpc data fetch {self.name}")
        st = p.stat()
        stamp = self._stamp(p)
        key = f"{sha} {st.st_size} {st.st_mtime_ns}"
        if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == key:
            return
        got = sha256_file(p)
        if got != sha:
            raise DatasetError(f"数据集 {self.ref} 的文件 {p} 校验失败：SHA-256 {got}，清单为 {sha}")
        if p.is_relative_to(self.root):
            stamp.write_text(key + "\n", encoding="utf-8")

    def verify(self) -> None:
        for b in self.manifest.bundled:
            if b.name not in self._verified:
                self._check(self.path(b.name), b.sha256)
                self._verified.add(b.name)
        for f in self.manifest.files:
            if f.name not in self._verified:
                self._check(self.path(f.name), f.sha256)
                self._verified.add(f.name)

    def status(self) -> dict[str, str]:
        out = {}
        for name, sha in [(b.name, b.sha256) for b in self.manifest.bundled] + [(f.name, f.sha256) for f in self.manifest.files]:
            try:
                self._check(self.path(name), sha)
                out[name] = "ok"
            except DatasetError as e:
                out[name] = "missing" if not self.path(name).exists() else f"invalid: {e}"
        return out

    # ------------------------------------------------------------ 下载

    def fetch(self, force: bool = False, progress: Callable[[str, int, int], None] | None = None) -> list[Path]:
        """下载缺失（或 force 时全部）的远程文件并校验。外部下载使用系统代理设置（trust_env=True）。"""
        import httpx

        out = []
        self.dir.mkdir(parents=True, exist_ok=True)
        for f in self.manifest.files:
            p = self.dir / f.name
            if p.is_file() and not force:
                self._check(p, f.sha256)
                out.append(p)
                continue
            headers = {}
            if f.auth == "hf":
                tok = os.environ.get("HF_TOKEN")
                if not tok:
                    raise DatasetError(f"数据集 {self.ref} 需要 Hugging Face 访问令牌：请在 {self.manifest.source} 申请访问后设置环境变量 HF_TOKEN")
                headers["Authorization"] = f"Bearer {tok}"
            part = p.with_name(p.name + ".part")
            t0 = time.time()
            try:
                with httpx.stream("GET", f.url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as r:
                    if r.status_code in (401, 403):
                        raise DatasetError(f"下载 {f.url} 被拒绝（HTTP {r.status_code}）：请检查访问权限与 HF_TOKEN")
                    r.raise_for_status()
                    done = 0
                    with open(part, "wb") as w:
                        for chunk in r.iter_bytes(1 << 20):
                            w.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(f.name, done, f.size)
            except httpx.HTTPError as e:
                raise DatasetError(f"下载 {f.url} 失败：{e}；可手动下载到 {p} 后运行 cpc data verify {self.name}") from e
            part.replace(p)
            self._check(p, f.sha256)
            log.info("已下载 %s（%.1f MB，%.0f s）", p, p.stat().st_size / 1e6, time.time() - t0,
                     extra={"event": "dataset_fetched", "dataset": self.ref})
            out.append(p)
        return out

    # ------------------------------------------------------------ 派生缓存

    def derived(self, key: str, build: Callable[[], dict[str, Any]], sources: list[str]) -> dict[str, Any]:
        """把原始文件解析后的数组缓存为 .npz（位于数据集目录的 derived/ 下），记录所依据原始文件的校验和。"""
        import numpy as np

        self.verify()
        shas = {s: next((f.sha256 for f in self.manifest.files if f.name == s), None) or
                next(b.sha256 for b in self.manifest.bundled if b.name == s) for s in sources}
        d = self.dir / "derived"
        npz, meta = d / f"{key}.npz", d / f"{key}.json"
        if npz.is_file() and meta.is_file() and json.loads(meta.read_text(encoding="utf-8")).get("sources") == shas:
            with np.load(npz, allow_pickle=False) as z:
                return {k: z[k] for k in z.files}
        arrays = build()
        d.mkdir(parents=True, exist_ok=True)
        np.savez(npz, **arrays)
        meta.write_text(json.dumps({"sources": shas, "dataset": self.ref}, ensure_ascii=False), encoding="utf-8")
        return arrays


DATASETS: dict[str, type[Dataset]] = {}


def register_dataset(cls: type[Dataset]) -> type[Dataset]:
    name = cls.manifest.name
    if name in DATASETS and DATASETS[name] is not cls:
        raise ValueError(f"数据集名称重复：{name}")
    DATASETS[name] = cls
    return cls


def make_dataset(name: str, root: Path | None = None, **params: Any) -> Dataset:
    from . import grid, power, workload  # noqa: F401  注册内置数据集

    if name not in DATASETS:
        raise DatasetError(f"未知数据集 {name}，可选：{sorted(DATASETS)}")
    try:
        return DATASETS[name](root=root, **params)
    except TypeError as e:
        raise DatasetError(f"数据集 {name} 的参数无效：{e}") from e


def all_datasets() -> dict[str, type[Dataset]]:
    from . import grid, power, workload  # noqa: F401

    return dict(DATASETS)


class FileView(Dataset):
    """只按清单管理文件（查看、下载、校验），不构造数据集本身（构造可能需要参数）。"""

    def __init__(self, manifest: Manifest, root: Path | None = None):
        self.manifest = manifest
        super().__init__(root)


def file_view(name: str, root: Path | None = None) -> FileView:
    reg = all_datasets()
    if name not in reg:
        raise DatasetError(f"未知数据集 {name}，可选：{sorted(reg)}")
    return FileView(reg[name].manifest, root)
