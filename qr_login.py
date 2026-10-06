#!/usr/bin/env python3
"""扫码推送登录 CLI（容器 / 无桌面服务器的扫码重建入口）。

用法::

    # 触发一轮扫码会话：无头打开登录页 → 截二维码推飞书 → 等待扫码 → 写回 wx.lic
    python qr_login.py

    # 查看最近一次会话状态（agent 场景也可 GET http://127.0.0.1:9876/login）
    python qr_login.py --status

    # 配置飞书应用时查询机器人所在的群（拿 chat_id 填 FEISHU_QR_RECEIVE_ID）
    python qr_login.py --list-chats

前置条件：已在 .env 配置 FEISHU_APP_ID / FEISHU_APP_SECRET（上传二维码图片）
以及投递出口（FEISHU_QR_RECEIVE_ID 或 FEISHU_WEBHOOK）。
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

from weread_keepalive import qr_push  # noqa: E402


def _show_status() -> int:
    status = qr_push.session_status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    agent_url = f"http://{config.AGENT_HOST}:{config.AGENT_PORT}/login"
    print(f"\n常驻代理场景也可查询: GET {agent_url}")
    return 0 if status.get("ok") is not False else 1


def _list_chats() -> int:
    try:
        chats = qr_push.feishu_list_chats()
    except Exception as exc:
        print(f"[qr-login] 查询群列表失败: {exc}", flush=True)
        print(
            "提示：需要配置 FEISHU_APP_ID / FEISHU_APP_SECRET，"
            "且应用已开通机器人能力并授予 im:chat 只读权限，机器人已加入目标群。",
            flush=True,
        )
        return 2
    if not chats:
        print("[qr-login] 应用机器人不在任何群里：请先把机器人加入目标群再查询")
        return 1
    print("机器人所在的群（把 chat_id 填到 FEISHU_QR_RECEIVE_ID）:")
    for chat in chats:
        print(f"  {chat['chat_id']}  {chat['name']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="微信读书扫码推送登录（飞书二维码）")
    parser.add_argument(
        "--status", action="store_true",
        help="只查看最近一次扫码会话状态，不触发新会话",
    )
    parser.add_argument(
        "--list-chats", action="store_true",
        help="列出飞书应用机器人所在的群（配置 FEISHU_QR_RECEIVE_ID 用）",
    )
    parser.add_argument(
        "--timeout", type=int, default=0,
        help="扫码等待时长（秒），默认取 SCAN_WAIT_SECONDS 配置",
    )
    args = parser.parse_args()

    if args.status:
        raise SystemExit(_show_status())
    if args.list_chats:
        raise SystemExit(_list_chats())

    outcome: dict = {}
    ok = qr_push.run_login_session(verbose=True, outcome=outcome, timeout_s=args.timeout or None)
    print(f"[qr-login] {'成功' if ok else '失败'}: {outcome.get('reason')}")
    sys.exit(0 if ok else 1)
