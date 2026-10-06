#!/usr/bin/env python3
"""启动容器常驻守护（Docker 入口）：定时刷新 + 探活监控 + 刷新代理三合一。

用法::

    python start_daemon.py

Docker 镜像的默认 CMD 就是它；宿主机桌面场景请分别使用
start_agent.py（常驻代理）+ Windows 计划任务（refresh_cookie.py --headless）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weread_keepalive import config  # noqa: E402

config.load_env_file()

from weread_keepalive.daemon import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
