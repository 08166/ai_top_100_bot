import importlib.util
import sys
from pathlib import Path

import pytest

BOT_PATH = Path(__file__).resolve().parents[1] / "bot-test.py"


@pytest.fixture(scope="session")
def bot():
    # 파일명에 하이픈이 있어 import 문으로는 못 불러오므로 경로로 모듈을 올림
    spec = importlib.util.spec_from_file_location("bot", BOT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bot"] = module
    spec.loader.exec_module(module)
    return module
