"""Repository files: every YAML file parses (deployment files, scenarios, dataset descriptors, CI)."""

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
        pytest.skip("not in a git work tree")
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
