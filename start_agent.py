#!/usr/bin/env python3
"""启动宿主机常驻刷新代理（HTTP /health /refresh）。

用法::

    python start_agent.py

常驻进程建议用开机启动项拉起（隐藏窗口），见 scripts/install_startup.ps1。
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

from weread_keepalive.agent import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
