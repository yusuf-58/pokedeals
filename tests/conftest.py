import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pokedeals as pd  # noqa: E402


@pytest.fixture(autouse=True)
def reset_state():
    pd.PPT_STATE["old"] = False
    pd.VISION_RUN.update(calls=0, fails=0)
    yield
    pd.PPT_STATE["old"] = False
