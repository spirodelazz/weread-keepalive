"""容器常驻守护：定时刷新 + 探活监控 + 刷新代理三合一（Docker 入口）。

Docker / NAS 场景没有 Windows 计划任务与多个终端，一个容器里跑齐三件事：

- **定时无头刷新**（等价宿主机计划任务）：每 ``WEREAD_DAEMON_REFRESH_SECONDS``
  （默认 900s）真实访问一次微信读书页面保活；
- **探活监控**：复用 monitor.check_once，直接向微信读书接口验证登录态并告警；
- **常驻代理**：复用 agent 的 HTTP 服务（/health /refresh /login），we-mp-rss
  容器可经 compose 内网直连触发刷新，或手动触发扫码推送。

登录态重建：无头刷新失败且配置了二维码推送（``WEREAD_QR_PUSH_ON_EXPIRE=1`` +
飞书应用凭据）时，自动把登录二维码推送到飞书，用户微信扫码即可远程重建 profile；
会话之间有冷却（``WEREAD_QR_SESSION_COOLDOWN_SECONDS``，默认 30 分钟）防打扰。
"""

from __future__ import annotations

import threading
import time

from . import agent, config, monitor, qr_push
from .refresher import refresh_cookie
from .state import record_attempt

_last_auto_push = [0.0]


def _maybe_auto_push(reason: str) -> None:
    """定时刷新失败且判定需要人工扫码时，触发一次二维码推送会话（带冷却）。"""
    if qr_push.session_running():
        return
    now = time.time()
    if now - _last_auto_push[0] < config.QR_SESSION_COOLDOWN_SECONDS:
        return
    trigger = qr_push.start_session(f"定时刷新失败：{reason}")
    if trigger.get("triggered"):
        _last_auto_push[0] = now
        print(f"[daemon] 登录二维码推送会话已启动（{reason}），请在飞书中扫码", flush=True)
    else:
        print(f"[daemon] 二维码推送未触发: {trigger.get('message')}", flush=True)


def _refresh_loop() -> None:
    """定时无头刷新：等价宿主机计划任务 `refresh_cookie.py --headless --cooldown-hours 0`。"""
    interval = config.DAEMON_REFRESH_SECONDS
    print(f"[daemon] 定时刷新已启动（间隔 {interval}s）", flush=True)
    while True:
        outcome: dict = {}
        ok = False
        try:
            ok = refresh_cookie(
                verbose=True, headless_only=True, cooldown_hours=0, outcome=outcome
            )
        except Exception as exc:
            ok = False
            outcome = {"attempted_profile": True, "reason": f"定时刷新异常: {exc}"}
            print(f"[daemon] 定时刷新异常: {exc}", flush=True)
        # 与宿主机口径一致：只有真访问过 profile 的尝试才算健康证据
        if outcome.get("attempted_profile") is not False:
            try:
                record_attempt(
                    ok,
                    source="daemon",
                    message=str(outcome.get("reason") or ("ok" if ok else "failed")),
                )
            except Exception as exc:
                print(f"[daemon] 写入刷新状态失败: {exc}", flush=True)
        if (
            not ok
            and outcome.get("attempted_profile")
            and config.QR_PUSH_ON_EXPIRE
            and qr_push.image_delivery_ready()
        ):
            _maybe_auto_push(str(outcome.get("reason") or "刷新失败"))
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return


def _monitor_loop() -> None:
    """探活监控：复用 monitor 的探活 + 自愈 + 告警逻辑（告警走已配置的通知渠道）。"""
    print(
        f"[daemon] 探活监控已启动（间隔 {config.MONITOR_INTERVAL_SECONDS}s）",
        flush=True,
    )
    while True:
        try:
            result = monitor.check_once()
            actions = "; ".join(result.get("actions") or []) or "正常"
            print(
                f"[daemon] 探活 {'通过' if result.get('probe_ok') else '失败'}"
                f"（{result.get('probe_reason')}）→ {actions}",
                flush=True,
            )
        except Exception as exc:
            print(f"[daemon] 本轮探活异常: {type(exc).__name__}: {exc}", flush=True)
        try:
            time.sleep(config.MONITOR_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            return


def main() -> int:
    print(
        "[daemon] weread-keepalive 容器守护启动"
        f"（wx.lic: {config.LIC_PATH}，profile: {config.PROFILE_DIR}）",
        flush=True,
    )
    if config.QR_PUSH_ON_EXPIRE and qr_push.image_delivery_ready():
        print("[daemon] 二维码推送已启用：登录态失效时会自动推送飞书", flush=True)
    elif config.QR_PUSH_ON_EXPIRE:
        print(
            "[daemon] 警告：WEREAD_QR_PUSH_ON_EXPIRE 已开启，但缺少飞书应用凭据或投递出口，"
            "登录态失效时只能告警、无法远程扫码（需配置 FEISHU_APP_ID/FEISHU_APP_SECRET "
            "及 FEISHU_QR_RECEIVE_ID 或 FEISHU_WEBHOOK）",
            flush=True,
        )
    threading.Thread(target=_refresh_loop, daemon=True).start()
    threading.Thread(target=_monitor_loop, daemon=True).start()
    # 主线程跑 HTTP 服务（/refresh /login），复用 agent 的安全检查与处理器
    return agent.run_server()


if __name__ == "__main__":
    raise SystemExit(main())
