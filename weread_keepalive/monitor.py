"""主动探活守护：定期用真实接口验证登录态，失效时自愈并告警。

背景（we-mp-rss issue #419）：we-mp-rss 的 ``login_status`` 只是本地缓存的时间戳，
从不向微信读书发起真实校验——服务端提前吊销会话（风控、异地登录、强制下线）时，
状态接口仍然报告「已登录」，直到某轮采集任务撞上错误码才被动发现。本守护进程
补上这一环：**直接用 wx.lic 里的 Cookie 向微信读书接口探活**，不依赖 we-mp-rss
的任何状态接口。

恢复梯度（来自生产环境验证过的经验）：

1. 探活失败 → 先尝试自动刷新（本机直接调 refresh_cookie，或经 WEREAD_REFRESH_AGENT_URL
   请宿主机代理代劳）——成功则静默自愈，不打扰用户；
2. 连续失败 < 阈值：容忍刷新间隙与网络抖动，不发告警；
3. 宿主机刷新状态文件显示 profile 连续失败 ≥ 阈值（或状态过期）→ 发
   「需在本机重建登录态」告警（扫码重建入口见 README）；
4. 告警按冷却时间（默认 6 小时）重发，避免刷屏，也避免一次错过就永久静默。

通知渠道（可同时启用多个，全部未配置时只写日志）：

- ``WEREAD_NOTIFY_FEISHU_WEBHOOK`` 飞书群机器人
- ``WEREAD_NOTIFY_WECOM_WEBHOOK``  企业微信群机器人
- ``WEREAD_NOTIFY_WEBHOOK``        通用端点（POST JSON {"title","text"}）
"""

from __future__ import annotations

import json
import time
import urllib.request

from . import config
from . import state as refresh_state
from .refresher import refresh_cookie
from .verify import verify_cookie_detail
from .wxlic import load_weread_data


def _post_json(url: str, payload: dict, timeout: int = 15) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def notify(title: str, text: str) -> list[str]:
    """把告警发到所有已配置的渠道，返回各渠道发送结果描述。"""
    results = []
    full_text = f"{title}\n{text}"
    if config.NOTIFY_FEISHU_WEBHOOK:
        ok, detail = _post_json(
            config.NOTIFY_FEISHU_WEBHOOK,
            {"msg_type": "text", "content": {"text": full_text}},
        )
        results.append(f"feishu: {'ok' if ok else detail}")
    if config.NOTIFY_WECOM_WEBHOOK:
        ok, detail = _post_json(
            config.NOTIFY_WECOM_WEBHOOK,
            {"msgtype": "text", "text": {"content": full_text}},
        )
        results.append(f"wecom: {'ok' if ok else detail}")
    if config.NOTIFY_WEBHOOK:
        ok, detail = _post_json(
            config.NOTIFY_WEBHOOK,
            {"title": title, "text": text},
        )
        results.append(f"webhook: {'ok' if ok else detail}")
    if not results:
        results.append("未配置任何通知渠道（WEREAD_NOTIFY_*），仅记录日志")
    return results


def _load_monitor_state() -> dict:
    try:
        payload = json.loads(config.MONITOR_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_monitor_state(state: dict) -> None:
    import os
    import tempfile
    from pathlib import Path

    path = Path(config.MONITOR_STATE_FILE)
    directory = path.parent if str(path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _should_alert(state: dict, now: int) -> bool:
    """连续失败达到阈值且距上次告警超过冷却期。"""
    failures = int(state.get("consecutive_failures") or 0)
    if failures < config.MONITOR_ALERT_FAILURES:
        return False
    last_alert = int(state.get("last_alert_at") or 0)
    return now - last_alert >= config.MONITOR_ALERT_COOLDOWN_SECONDS


def _try_auto_refresh() -> tuple[bool, str]:
    """恢复梯度第一级：尝试自动刷新。

    配置了 WEREAD_REFRESH_AGENT_URL（容器/远程场景）走 HTTP 代理；
    否则在本机直接调 refresh_cookie（无头，不弹窗）。
    """
    import os

    agent_url = os.environ.get("WEREAD_REFRESH_AGENT_URL", "").strip()
    if agent_url:
        from .refresher import request_host_refresh

        result = request_host_refresh(timeout_s=180)
        if result.get("triggered") and result.get("ok") and not result.get("needs_scan"):
            return True, result.get("message") or "宿主机刷新成功"
        return False, result.get("message") or "宿主机刷新未成功"
    outcome: dict = {}
    try:
        ok = refresh_cookie(verbose=False, headless_only=True, cooldown_hours=0, outcome=outcome)
    except Exception as exc:
        return False, f"本机自动刷新异常: {exc}"
    reason = str(outcome.get("reason") or ("ok" if ok else "failed"))
    return ok, reason


def check_once() -> dict:
    """执行一轮探活 + 自愈 + 告警判定，返回本轮结果摘要（也用于单次诊断模式）。"""
    now = int(time.time())
    _, data = load_weread_data()
    cookie = (data.get("cookie") or "").strip()
    ok, reason = verify_cookie_detail(cookie)
    result: dict = {"probe_ok": ok, "probe_reason": reason, "actions": []}

    mon_state = _load_monitor_state()
    if ok:
        if int(mon_state.get("consecutive_failures") or 0) > 0:
            result["actions"].append("登录态已恢复（此前存在失败记录）")
            _save_monitor_state({"consecutive_failures": 0, "last_alert_at": mon_state.get("last_alert_at", 0)})
        return result

    # 探活失败：先自愈
    refreshed, refresh_msg = _try_auto_refresh()
    result["actions"].append(f"自动刷新: {'成功' if refreshed else '失败'}（{refresh_msg}）")
    if refreshed:
        # 刷新成功后复检一次，避免「Cookie 文件好了、会话没好」的假恢复
        _, data = load_weread_data()
        ok2, reason2 = verify_cookie_detail((data.get("cookie") or "").strip())
        result["actions"].append(f"刷新后复检: {'通过' if ok2 else '仍失败（' + reason2 + '）'}")
        if ok2:
            _save_monitor_state({"consecutive_failures": 0, "last_alert_at": mon_state.get("last_alert_at", 0)})
            result["recovered"] = True
            return result

    # 自愈失败：累计失败计数并判定是否告警
    failures = int(mon_state.get("consecutive_failures") or 0) + 1
    mon_state["consecutive_failures"] = failures
    mon_state["last_fail_at"] = now

    # 结合宿主机刷新状态判断 profile 是否已死（死 → 必须人工扫码重建）
    dead, dead_detail = refresh_state.profile_dead(
        None,
        threshold=3,
        max_age_seconds=3600,
    )
    if _should_alert(mon_state, now):
        if dead:
            title = "【微信读书登录态需人工重建】"
            # 按环境给出正确的重建入口：配置了二维码推送（容器/无人值守）→ 远程扫码；
            # 桌面宿主机 → 本机弹窗扫码。
            from . import qr_push

            if config.QR_PUSH_ON_EXPIRE and qr_push.image_delivery_ready():
                rebuild_hint = (
                    "已配置二维码推送：下一轮刷新失败时会自动推送登录二维码到飞书，"
                    "用微信扫码即可；也可立即触发 POST /login 或运行 python qr_login.py"
                )
            else:
                rebuild_hint = "请在本机运行: python refresh_cookie.py （弹窗扫码重建登录态）"
            text = (
                f"探活连续失败 {failures} 次，自动刷新无效，且宿主机 profile 会话已连续刷新失败。\n"
                f"{dead_detail}\n"
                f"{rebuild_hint}"
            )
        else:
            title = "【微信读书登录态异常】"
            text = (
                f"探活连续失败 {failures} 次，自动刷新无效。\n"
                f"最近原因: {reason}\n"
                "将持续自动重试；若持续收到本告警，请在本机运行 python refresh_cookie.py 扫码重建。"
            )
        for line in notify(title, text):
            result["actions"].append(f"告警 {line}")
        mon_state["last_alert_at"] = now
        mon_state["last_alert_title"] = title
    else:
        result["actions"].append(
            f"失败累计 {failures}/{config.MONITOR_ALERT_FAILURES}，未达告警条件或处于告警冷却期"
        )
    _save_monitor_state(mon_state)
    return result


def main() -> int:
    """常驻守护：按 MONITOR_INTERVAL_SECONDS 周期探活。"""
    print(
        f"[monitor] 微信读书登录态探活守护已启动（间隔 {config.MONITOR_INTERVAL_SECONDS}s，"
        f"告警阈值 {config.MONITOR_ALERT_FAILURES} 次连续失败，告警冷却 "
        f"{config.MONITOR_ALERT_COOLDOWN_SECONDS // 3600}h）",
        flush=True,
    )
    while True:
        try:
            result = check_once()
            actions = "; ".join(result.get("actions") or []) or "正常"
            print(
                f"[monitor] 探活 {'通过' if result.get('probe_ok') else '失败'}"
                f"（{result.get('probe_reason')}）→ {actions}",
                flush=True,
            )
        except Exception as exc:
            print(f"[monitor] 本轮探活异常: {type(exc).__name__}: {exc}", flush=True)
        try:
            time.sleep(config.MONITOR_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("[monitor] 已停止", flush=True)
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
