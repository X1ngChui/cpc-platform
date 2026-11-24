import sys
from datetime import datetime
from pathlib import Path

import pytest

from cpc.config import load_scenario

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data"
TEST_SCENARIO = DATA / "scenario.yaml"
WORKLOAD = DATA / "datasets" / "workload-synthetic"

sys.path.insert(0, str(ROOT / "examples" / "datasets"))


def ensure_test_workload() -> Path:
    """测试场景的合成负载数据集（首次使用时生成，不入库）。"""
    if not (WORKLOAD / "datapackage.yaml").is_file():
        from synthetic_workload import generate

        generate(WORKLOAD, origin=datetime.fromisoformat("2026-07-15T00:00:00+08:00"), hours=48, rps=12, amplitude=0.5,
                 peak_hour=20, input_tokens=(800, 0.8, 8192), output_tokens=(200, 0.7, 1024), seed=1, name="workload-synthetic")
    return WORKLOAD


@pytest.fixture(scope="session", autouse=True)
def _test_workload():
    ensure_test_workload()


@pytest.fixture()
def scn():
    return load_scenario(TEST_SCENARIO)
