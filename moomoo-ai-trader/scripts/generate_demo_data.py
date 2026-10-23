"""重新生成演示数据（虚构证券，确定性）。用法：python scripts/generate_demo_data.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.market_data.fixtures import generate  # noqa: E402

if __name__ == "__main__":
    generate(ROOT / "data" / "fixtures")
    print("演示数据已生成：data/fixtures/")
