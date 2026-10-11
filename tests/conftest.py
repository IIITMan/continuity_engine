from pathlib import Path
import pytest

from continuity_engine import ConfigManager

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def config_manager():
    return ConfigManager.from_yaml(ROOT / "config" / "default.yaml")
