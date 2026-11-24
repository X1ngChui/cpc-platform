from pathlib import Path

import pytest

from cpc.config import load_scenario

ROOT = Path(__file__).resolve().parents[1]


TEST_SCENARIO = ROOT / "tests" / "data" / "scenario.yaml"  # 合成负载 + 随包电网数据，离线可用


@pytest.fixture()
def scn():
    return load_scenario(TEST_SCENARIO)
