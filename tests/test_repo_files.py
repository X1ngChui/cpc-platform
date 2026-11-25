"""仓库中的配置文件：全部 YAML 文件可解析（部署文件、场景、数据集描述与 CI 配置）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _yaml_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "*.yml", "*.yaml"], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("不在 git 工作区中")
    return out.stdout.split()


def test_all_yaml_files_parse():
    files = _yaml_files()
    assert files
    bad = {}
    for f in files:
        try:
            list(yaml.safe_load_all((ROOT / f).read_text(encoding="utf-8")))
        except yaml.YAMLError as e:
            bad[f] = str(e).splitlines()[0]
    assert not bad, bad
