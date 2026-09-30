"""启动互访编排 HTTP 服务（启动前自动执行恢复清理）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exchange_service.__main__ import main

if __name__ == "__main__":
    main()
