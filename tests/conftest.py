import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/kiosk/backend"))


def load_remote():
    spec = importlib.util.spec_from_file_location("frame_remote", ROOT / "apps/remote/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
