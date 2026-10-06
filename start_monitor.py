#!/usr/bin/env python3
"""启动主动探活守护（定期验证登录态 + 自动自愈 + 告警）。

用法::

    python start_monitor.py          # 常驻守护
    python start_monitor.py --once   # 只探活一轮并输出结果（诊断用）

对齐 we-mp-rss issue #419：不信任本地 login_status 缓存，直接向微信读书
真实接口验证。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weread_keepalive import config  # noqa: E402

config.load_env_file()

from weread_keepalive.monitor import check_once  # noqa: E402


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="微信读书登录态主动探活")
    parser.add_argument("--once", action="store_true", help="只执行一轮探活（诊断模式）")
    args = parser.parse_args()

    if args.once:
        result = check_once()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result.get("probe_ok") or result.get("recovered") else 1)

    from weread_keepalive.monitor import main

    raise SystemExit(main())
