"""单独启动/停止 10 个本地 OAI-PMH 模拟仓储。

用法：
  py run_servers.py            # 前台启动，Ctrl+C 停止
  py run_servers.py --status   # 查看 servers.json 注册信息
"""
from __future__ import annotations

import json
import sys
import time

from scholar_hub.config import SERVER_REGISTRY
from scholar_hub.repositories.server import ServerManager


def main() -> int:
    if "--status" in sys.argv:
        servers = json.loads(SERVER_REGISTRY.read_text(encoding="utf-8"))
        for s in servers:
            print(f"{s['code']:14} {s['base_url']:32} {s['metadata_format']} "
                  f"quirks={s['quirks']}")
        return 0

    manager = ServerManager()
    servers = manager.start()
    print(f"已启动 {len(servers)} 个本地 OAI-PMH 仓储（仅绑定 127.0.0.1）：")
    for s in servers:
        print(f"  {s['code']:14} http://127.0.0.1:{s['port']}/oai")
    print("\n按 Ctrl+C 停止 ...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止仓储 ...")
        manager.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
