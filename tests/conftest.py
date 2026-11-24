from pathlib import Path

import pytest

from cpc.config import load_scenario

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def scn():
    return load_scenario(ROOT / "configs" / "scenario.yaml")
