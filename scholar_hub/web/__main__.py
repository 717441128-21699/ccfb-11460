"""支持 `py -m scholar_hub.web` 启动界面。"""
from __future__ import annotations

from .app import create_app


def main() -> None:
    app = create_app()
    app.run(host="127.0.0.1", port=8000, debug=False)


if __name__ == "__main__":
    main()
