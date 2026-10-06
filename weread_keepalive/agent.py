"""宿主机微信读书 Cookie 刷新代理（常驻 HTTP 服务）。

为什么需要它
------------
- 长命登录态是**宿主机 Chrome profile 的网页会话**，容器内 Linux Chromium 解密不了
  （profile 由操作系统凭据加密），因此浏览器刷新动作必须在宿主机完成。
- we-mp-rss 容器（或任何外部系统）希望在采集失败/ Cookie 过期时**自动触发刷新**，
  而不是手动跑到宿主机敲命令。
- 解法：宿主机常驻一个轻量 HTTP 代理。调用方请求 ``/refresh``，代理用本机浏览器
  做无头刷新（登录态失效时再弹窗扫码），结果写回数据卷 wx.lic；容器随后读到最新
  Cookie 继续同步文章。

网络暴露面（重要）
------------------
**默认只绑 127.0.0.1，这不是限制，而是正确配置。** 实测（Docker Desktop on Windows）
容器内 ``host.docker.internal`` 解析到 192.168.65.x 并转发到宿主机回环，容器**能直接
访问只绑回环的本代理**（``curl http://host.docker.internal:9876/health`` → 200）。
因此**没有理由绑 0.0.0.0**——那会把「能让宿主机弹浏览器窗口」的能力暴露给局域网。
若确实要绑非回环地址，必须同时设置 ``WEREAD_AGENT_TOKEN``，否则本代理**拒绝启动**。

刷新策略（verify-first，尽量不启动浏览器）
----------------------------------------
1. 先验证当前 wx.lic 里的 Cookie 是否仍能拉到数据；有效则直接返回（秒级，不启动浏览器）。
2. 无效则做**无头刷新**（复用持久化 profile，通常秒级~数十秒）。
3. 无头仍失败（登录态过期）→ 后台开一个可见浏览器窗口让用户扫码，HTTP 立即返回
   ``needs_scan``。

并发控制：同一时刻只能有一个刷新在跑，也只能有一个扫码窗口在等。否则容器侧
每轮采集都来一次，会连续弹出多个浏览器窗口。正在进行时返回 ``busy``，调用方
据此跳过本轮而不是当成失败告警。
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import config
from . import qr_push
from .refresher import refresh_cookie
from .state import record_attempt
from .verify import verify_cookie_detail
from .wxlic import load_weread_data

# 串行化刷新，避免并发（如 15 分钟定时任务 + 容器探活失败同时触发）争抢同一 profile
_refresh_lock = threading.Lock()
# 扫码窗口同样只允许一个：容器侧每轮采集都会来问一次，不去重就会连续弹窗
_scan_pending = threading.Event()


def is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _allowed_cidrs() -> list:
    networks = []
    for item in (config.AGENT_ALLOW_CIDRS or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            print(f"[agent] 忽略非法的 WEREAD_AGENT_ALLOW_CIDRS 项: {item}", flush=True)
    return networks


def _client_allowed(peer: str) -> tuple[bool, str]:
    networks = _allowed_cidrs()
    if not networks:
        return True, ""
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False, f"无法解析来源地址 {peer}"
    if any(address in network for network in networks):
        return True, ""
    return False, f"来源 {peer} 不在 WEREAD_AGENT_ALLOW_CIDRS 白名单内"


def _token_ok(token: str) -> bool:
    return not config.AGENT_TOKEN or token.strip() == config.AGENT_TOKEN


def _result(
    *, ok: bool, refreshed: bool = False, needs_scan: bool = False, busy: bool = False, message: str = ""
) -> dict:
    return {
        "ok": ok,
        "refreshed": refreshed,
        "needs_scan": needs_scan,
        "busy": busy,
        "message": message,
    }


def do_refresh() -> dict:
    """执行一次「确保 Cookie 新鲜」的逻辑（verify-first）。"""
    # 0) 已有一个扫码会话在等（可见窗口或二维码推送）：不要再跑无头刷新，
    #    更不要再开一个会话
    if _scan_pending.is_set() or qr_push.session_running():
        return _result(
            ok=False,
            needs_scan=True,
            busy=True,
            message="已有扫码会话在进行，请先完成扫码（二维码已推送飞书或窗口已弹出）",
        )

    # 1) 当前 Cookie 仍有效 → 无需启动浏览器，直接返回
    _, data = load_weread_data()
    cookie = (data.get("cookie") or "").strip()
    if cookie and verify_cookie_detail(cookie)[0]:
        return _result(ok=True, message="Cookie 仍有效，无需刷新")

    # 2) 需要刷新：无头优先（cooldown_hours=0 强制真刷新，绕过冷却误判）
    outcome: dict = {}
    try:
        ok = refresh_cookie(
            verbose=True, headless_only=True, cooldown_hours=0, outcome=outcome
        )
    except Exception as exc:
        ok = False
        outcome = {"attempted_profile": True, "reason": f"无头刷新异常: {exc}"}
        print(f"[agent] 无头刷新异常: {exc}", flush=True)

    # 只有真访问过 profile 的尝试才写入健康状态（冷却跳过不能证明 profile 存活）
    if outcome.get("attempted_profile") is not False:
        try:
            record_attempt(
                ok, source="agent", message=str(outcome.get("reason") or ("ok" if ok else "failed"))
            )
        except Exception as exc:
            print(f"[agent] 写入刷新状态失败: {exc}", flush=True)

    if ok:
        return _result(ok=True, refreshed=True, message="Cookie 已自动刷新")

    # 未配置刷新 URL / 缺依赖等"没碰浏览器"的失败：不是登录态问题，
    # 不要进入扫码分支（GUI 刷新也会因同样原因失败），直接返回配置类错误。
    reason = str(outcome.get("reason") or "")
    if outcome.get("attempted_profile") is False:
        return _result(ok=False, needs_scan=False, message=f"刷新未执行: {reason}")

    # 3) 无头失败：登录态可能已过期。
    # 3a) 容器/无人值守场景：不弹窗（也没有显示器），把登录二维码推送到飞书，
    #     用户远程扫码重建（WEREAD_QR_PUSH_ON_EXPIRE=1 + 飞书应用凭据）。
    if config.QR_PUSH_ON_EXPIRE and qr_push.image_delivery_ready():
        trigger = qr_push.start_session(f"无头刷新失败：{reason}")
        if trigger.get("triggered"):
            return _result(
                ok=False,
                needs_scan=True,
                message="登录态已过期，登录二维码已推送飞书，请用微信扫码重建登录态",
            )

    # 3b) 桌面宿主机：后台开可见浏览器窗口让用户扫码（不阻塞 HTTP 请求）；
    #     窗口内的二维码也会实时推飞书（配了飞书出口时），人不在电脑前同样能扫。
    def _bg_scan():
        scan_outcome: dict = {}
        try:
            try:
                # 不带 headless_only → 无头失败会弹窗等待扫码（最长 SCAN_WAIT_SECONDS 秒）
                scan_ok = refresh_cookie(verbose=True, cooldown_hours=0, outcome=scan_outcome)
            except Exception as exc:
                scan_ok = False
                scan_outcome = {"attempted_profile": True, "reason": f"后台扫码刷新异常: {exc}"}
                print(f"[agent] 后台扫码刷新异常: {exc}", flush=True)
            if scan_outcome.get("attempted_profile") is not False:
                try:
                    record_attempt(
                        scan_ok,
                        source="agent-gui",
                        message=str(scan_outcome.get("reason") or ("ok" if scan_ok else "failed")),
                    )
                except Exception as exc:
                    print(f"[agent] 写入刷新状态失败: {exc}", flush=True)
        finally:
            # 无论成功、失败还是抛异常都要清位，否则代理会永久认为「扫码窗口开着」，
            # 后续所有刷新请求都被 busy 短路掉。
            _scan_pending.clear()

    # 先置位再起线程：否则下一轮请求可能在后台线程置位前挤进来，又开一个窗口
    _scan_pending.set()
    threading.Thread(target=_bg_scan, daemon=True).start()
    return _result(
        ok=False,
        needs_scan=True,
        message="Cookie 已过期，请在弹出的浏览器窗口中扫码登录微信读书",
    )


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # 只记录路径不带 query：token 允许走 ?token=，绝不能写进日志
        path = self.path.partition("?")[0]
        message = str(payload.get("message") or "")[:140]
        print(
            f"[agent] {self.client_address[0]} {self.command} {path} -> {code} {message}",
            flush=True,
        )

    def _authorized(self, query: str) -> tuple[bool, str]:
        allowed, reason = _client_allowed(self.client_address[0])
        if not allowed:
            return False, reason
        if not config.AGENT_TOKEN:
            return True, ""
        query_token = (parse_qs(urlsplit(f"?{query}").query).get("token") or [""])[0]
        header_token = self.headers.get("X-Agent-Token", "")
        if _token_ok(header_token) or _token_ok(query_token):
            return True, ""
        return False, "invalid agent token"

    def _refresh(self, query: str = ""):
        allowed, reason = self._authorized(query)
        if not allowed:
            self._send(401, {"ok": False, "busy": False, "needs_scan": False, "message": reason})
            return
        # 非阻塞抢锁：正在刷新时立刻返回 busy，让容器侧跳过本轮，
        # 而不是把请求线程挂在这里等（等久了容器侧只会超时，拿不到有效信息）。
        if not _refresh_lock.acquire(blocking=False):
            self._send(
                200,
                {
                    "ok": False,
                    "refreshed": False,
                    "needs_scan": False,
                    "busy": True,
                    "message": "宿主机已有刷新在进行，本次跳过",
                },
            )
            return
        try:
            result = do_refresh()
        finally:
            _refresh_lock.release()
        self._send(200, result)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        path = path.rstrip("/")
        if path in ("/health", ""):
            self._send(
                200,
                {
                    "ok": True,
                    "message": "agent alive",
                    "busy": _refresh_lock.locked(),
                    "scan_pending": _scan_pending.is_set(),
                    "qr_session_running": qr_push.session_running(),
                    "token_required": bool(config.AGENT_TOKEN),
                },
            )
        elif path == "/refresh":
            self._refresh(query)
        elif path == "/login":
            # 扫码会话状态查询：running + 最近一次结果（容器侧轮询用）
            self._send(200, {"ok": True, **qr_push.session_status()})
        else:
            self._send(404, {"ok": False, "message": "not found"})

    def do_POST(self):
        # /refresh 同时支持 POST（容器 urllib 默认 POST）
        path, _, query = self.path.partition("?")
        if path.rstrip("/") == "/refresh":
            self._refresh(query)
        elif path.rstrip("/") == "/login":
            self._login(query)
        else:
            self._send(404, {"ok": False, "message": "not found"})

    def _login(self, query: str = ""):
        """手动触发一次二维码推送扫码会话（容器/无桌面场景的扫码重建入口）。"""
        allowed, reason = self._authorized(query)
        if not allowed:
            self._send(401, {"ok": False, "triggered": False, "message": reason})
            return
        trigger = qr_push.start_session("HTTP /login 手动触发")
        # busy / 未配置也是 200：调用方按 triggered/busy 字段判断，与 /refresh 风格一致
        self._send(200, {"ok": bool(trigger.get("triggered")), **trigger})

    def log_message(self, *args):
        # 默认实现会把每个请求打到 stderr（含 query 里的 token），已在 _send 内打印精简日志
        pass


def redirect_logs(path) -> None:
    """pythonw 启动时没有 stdout，print 会静默丢弃——日志落到文件才能排查。

    本代理是容器侧刷新链路的唯一入口，出问题时「没日志」等于没法定位。
    超过 2MB 时轮转一次，常驻进程不该让日志无限增长。
    """
    try:
        os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 2 * 1024 * 1024:
            os.replace(path, str(path) + ".1")
        handle = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = handle
    sys.stderr = handle


def run_server() -> int:
    """启动 HTTP 服务（含安全检查）。agent 本体与容器守护 daemon 共用。"""
    if not is_loopback(config.AGENT_HOST) and not config.AGENT_TOKEN:
        print(
            f"[agent] 拒绝启动：WEREAD_AGENT_HOST 绑定了非回环地址 ({config.AGENT_HOST})"
            " 但未设置 WEREAD_AGENT_TOKEN。本代理能触发宿主机浏览器弹窗，"
            "绝不可以在无鉴权的情况下暴露到局域网。"
            "\n[agent] 提示：容器默认也能访问只绑 127.0.0.1 的本代理"
            "（host.docker.internal 会转发到宿主机回环），通常无需改成 0.0.0.0。",
            flush=True,
        )
        return 2
    os.makedirs(str(config.LIC_PATH.parent), exist_ok=True)
    server = ThreadingHTTPServer((config.AGENT_HOST, config.AGENT_PORT), _Handler)
    token_note = "已启用 token 鉴权" if config.AGENT_TOKEN else "未启用 token 鉴权（仅回环可访问）"
    print(
        f"[agent] 微信读书 Cookie 刷新代理已启动: "
        f"http://{config.AGENT_HOST}:{config.AGENT_PORT}/refresh（{token_note}）",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[agent] 代理已停止", flush=True)
    return 0


def main() -> int:
    if sys.stdout is None:
        redirect_logs(config.AGENT_LOG_FILE)
    elif hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass

    return run_server()


if __name__ == "__main__":
    raise SystemExit(main())
