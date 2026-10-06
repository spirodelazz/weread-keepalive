#!/usr/bin/env python3
"""微信读书 Cookie 刷新 CLI（全部在本机执行，we-mp-rss 容器只消费结果）。

用法::

    # 首次建立登录态 / 过期后重建：弹可见窗口，手机扫码（默认 GUI 模式）
    python refresh_cookie.py

    # 定时自动刷新（供 Windows 计划任务 / cron 调用，无头、不弹窗、不等待扫码）：
    python refresh_cookie.py --headless

    # 只验证当前登录态，不刷新：
    python refresh_cookie.py --status

每次运行的结果都会写入 data/weread_host_refresh_state.json（契约见
weread_keepalive/state.py）。外部监控据此区分「profile 还真活着，只是 API 会话
过期」与「profile 已连续刷新失败、必须回本机重建」，避免让用户反复扫只能撑
90 分钟的 API 二维码。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weread_keepalive import config  # noqa: E402

# .env 只读不覆盖：显式设置的进程环境变量优先
config.load_env_file()

from weread_keepalive.refresher import refresh_cookie  # noqa: E402
from weread_keepalive.state import record_attempt  # noqa: E402
from weread_keepalive.verify import verify_cookie_detail  # noqa: E402
from weread_keepalive.wxlic import load_weread_data  # noqa: E402


class _TeeLines:
    """透传输出到原流，同时保留最近若干行，用于落盘一条可排查的结果摘要。"""

    def __init__(self, stream, keep: int = 40) -> None:
        self._stream = stream
        self._keep = keep
        self.lines: list[str] = []

    def write(self, data) -> int:
        try:
            self._stream.write(data)
        except Exception:
            pass
        for part in str(data).splitlines():
            if part.strip():
                self.lines.append(part.strip())
        if len(self.lines) > self._keep:
            del self.lines[: -self._keep]
        return len(data)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass

    def last_line(self, fallback: str) -> str:
        return self.lines[-1] if self.lines else fallback


def _show_status() -> int:
    """打印当前 wx.lic 的登录态概况（不打码完整 Cookie，只显示键名与关键键值状态）。"""
    _, data = load_weread_data()
    cookie = (data.get("cookie") or "").strip()
    print(f"wx.lic 路径: {config.LIC_PATH}")
    print(f"刷新页面 URL: {data.get('cookie_refresh_url') or '(未配置)'}")
    print(f"vid: {data.get('vid') or '(空)'}  name: {data.get('name') or '(空)'}")
    last_ts = data.get("cookie_refresh_last_ts") or 0
    if last_ts:
        print(f"上次刷新时间: {datetime.fromtimestamp(float(last_ts))}")
    if not cookie:
        print("Cookie: (空) —— 请运行 python refresh_cookie.py 扫码建立登录态")
        return 1
    keys = [item.partition("=")[0].strip() for item in cookie.split(";") if "=" in item]
    print(f"Cookie 键({len(keys)}): {', '.join(keys)}")
    ok, reason = verify_cookie_detail(cookie)
    print(f"登录态验证: {'有效 ✓' if ok else '无效 ✗'}（{reason}）")
    return 0 if ok else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="微信读书 Cookie 刷新（本机）")
    parser.add_argument(
        "--headless", action="store_true",
        help="无头模式：定时自动刷新用，不弹窗、不等待扫码；登录态过期时直接返回失败",
    )
    parser.add_argument(
        "--cooldown-hours", type=float, default=None,
        help="距上次成功刷新不足该小时数且 Cookie 仍有效时跳过浏览器刷新；"
             "定时任务建议传 0 表示每次都真实访问页面，保证 90 分钟会话窗口内始终有页面访问",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="只查看当前登录态验证结果，不刷新",
    )
    args = parser.parse_args()

    if args.status:
        raise SystemExit(_show_status())

    source = "task" if args.headless else "gui"
    tee = _TeeLines(sys.stdout)
    outcome: dict = {}
    ok = False
    try:
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
            ok = refresh_cookie(
                verbose=True,
                headless_only=args.headless,
                cooldown_hours=args.cooldown_hours,
                outcome=outcome,
            )
    except Exception as exc:  # 记录现场后按失败退出，避免异常让状态文件停在旧值
        ok = False
        outcome = {"ok": False, "attempted_profile": True, "reason": f"未捕获异常: {exc}"}
    finally:
        # 只有真访问过 profile 的调用才算「profile 健康证据」：冷却跳过只能证明
        # wx.lic 的 API Cookie 活着，拿它清零连续失败计数会掩盖已死的 profile。
        if outcome.get("attempted_profile") is False:
            tee.write(f"[refresh] 未访问 profile（{outcome.get('reason')}），不更新宿主机刷新状态\n")
        else:
            reason = str(outcome.get("reason") or ("ok" if ok else "failed"))
            try:
                record_attempt(ok, source=source, message=f"{reason} | {tee.last_line(reason)}")
            except Exception as exc:
                # 状态文件写不进去不能影响刷新本身的退出码判断，只提示。
                tee.write(f"[refresh] 写入刷新状态失败: {exc}\n")
    sys.exit(0 if ok else 1)
