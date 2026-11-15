"""全局路径与运行参数。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SCHOLAR_HUB_DATA", ROOT_DIR / "data"))
DB_PATH = DATA_DIR / "scholar_hub.db"
GEN_DATA_DIR = Path(os.environ.get(
    "SCHOLAR_HUB_GEN_DATA", DATA_DIR / "generated"))  # 生成器产出的真值 + 仓储负载
SERVER_REGISTRY = GEN_DATA_DIR / "servers.json"

# 收割参数
HTTP_TIMEOUT = 15.0
PAGE_RETRIES = 2                 # 网络层重试
RESUME_GRACE_SECONDS = 0         # 演示用；真实场景可放宽时钟偏差
DEFAULT_PAGE_SIZE = 25

# 作者归并参数（经 data/generated/threshold_sweep.json 调参确定）
# 0.65 时成对 P=1.0000 / R=0.9597，双过九成验收线
PAIR_ACCEPT = 0.65               # 成对归并接受阈值
PAIR_REVIEW = 0.45               # 低于此值一定不归并

# 仓储端口段
REPO_PORT_START = 18010


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GEN_DATA_DIR.mkdir(parents=True, exist_ok=True)
