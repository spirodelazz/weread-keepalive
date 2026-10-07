"""微信读书 Cookie 刷新核心：桌面方案在宿主机执行，Docker 方案在容器内执行。

架构与原理
----------
微信读书的登录态有**两套互不相干的会话**：

- **profile 网页会话**（本机 Chrome/Chromium 持久化 profile，**长命**）——采集链路
  真正依赖的登录态。保活的根基就是定期用真实浏览器打开微信读书页面，让服务端
  一直认为这是一个活跃的网页会话；
- **API 扫码会话**（we-mp-rss 的扫码接口，wr_skey 只有约 90 分钟）——只能临时救急。
  如果 profile 死了只靠扫 API 二维码，就会陷入「扫一次撑 90 分钟、再被叫去扫」的
  跑步机。本工具绝不走那条路。

流程（``refresh_cookie``）：

1. 抢非阻塞刷新锁（防并发对同一 profile 起第二个浏览器）；
2. 冷却检查：距上次成功刷新不足冷却期且 Cookie 仍经真实接口验证有效 → 跳过；
3. 无头打开配置的 reader 页，从页面发出的 /web/mp/articles 请求头收割最新
   Cookie（回退 context.cookies 拼接），去重后实打实验证；
4. 验证通过 → 原子写回 wx.lic，结束；
5. 验证失败 → headless 模式直接报告失败；GUI 模式弹可见窗口等待用户扫码重建
   （最长 SCAN_WAIT_SECONDS 秒，3 秒轮询验证，成功后写回 wx.lic）。

为什么浏览器操作必须在宿主机：**桌面方案**（we-mp-rss 跑在容器、本工具跑在
Windows/macOS 宿主机）下，容器内是 Linux Chromium，解不开宿主操作系统凭据
（Windows DPAPI / macOS 钥匙串）加密的 profile cookie——容器内做刷新必然失败。
容器侧只应通过 agent 的 /refresh 端点请宿主机代劳。**Docker 模式**（NAS/无桌面
服务器，见 README）则相反：浏览器与 profile 都在容器内自建自用，不存在跨系统
解密问题，登录态重建靠无头扫码会话 + 二维码推送飞书（见 qr_push.py）。

注意：profile 种子注入默认关闭（``WEREAD_SEED_COOKIE_ENABLED``）——注入 wx.lic
的 API Cookie 会覆盖 profile 自己的真实网页会话，把长命会话降级成 90 分钟短命会话。
"""

from __future__ import annotations

import os
import sys
import time

from . import config
from .verify import verify_cookie_detail
from .wxlic import (
    dedupe_cookie,
    extract_vid,
    file_lock,
    load_weread_data,
    refresh_url_of,
    save_cookie,
)


def _make_qr_pusher(verbose: bool = True):
    """扫码会话里把登录二维码实时推飞书；未启用/导入失败返回 None（绝不影响刷新）。

    桌面宿主机弹窗扫码的同时二维码也会推到飞书（人不在
    电脑前也能远程扫）；无显示器的容器/NAS 则直接走无头会话 + 推送。
    """
    try:
        from .qr_push import make_pusher

        return make_pusher(verbose)
    except Exception:
        return None


def _display_absent() -> bool:
    """无桌面环境判定：Linux 容器/服务器没有 DISPLAY → 无头扫码 + 二维码推送。

    Windows/macOS 永远认为有桌面（macOS 无需 DISPLAY 变量即可弹窗）。
    """
    if sys.platform in ("win32", "darwin"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

# 11 键完整 Cookie（含 wr_fp/wr_gid 等指纹）是 profile 会话健康的标志，
# 这里不做硬性键数检查，但 wr_vid 是最低门槛（见 verify_cookie_detail）。


def refresh_cookie(
    *,
    verbose: bool = True,
    headless_only: bool = False,
    cooldown_hours: float | None = None,
    outcome: dict | None = None,
) -> bool:
    """对外入口：先抢非阻塞刷新锁，防并发对同一 Chrome profile 起第二个浏览器。

    计划任务进程与常驻代理线程互不感知；两个进程同时 launch_persistent_context
    指向同一 profile，新浏览器会拿到空/损坏会话，还可能把 wx.lic 写花。
    锁被占用即返回 busy（attempted_profile=False：没碰 profile 就不能当作存活的证据）。

    headless_only: True 时只做无头刷新，不在登录失效时弹窗扫码（代理/容器场景）。
    cooldown_hours: None 时取配置默认值；传 0 表示强制真刷新。
    outcome: 可选出参，回填 ``{"ok", "attempted_profile", "reason"}``。
             冷却跳过 / 未配置 URL / 缺依赖时 attempted_profile=False——这些路径
             只能证明 wx.lic 里的 API Cookie 活着，**不能**证明 profile 会话还活着，
             调用方不应据此清零「profile 连续失败」计数。
    """
    if cooldown_hours is None:
        cooldown_hours = config.COOLDOWN_HOURS

    try:
        lock = file_lock(str(config.LIC_PATH) + ".refresh.lock", block=False)
        lock.__enter__()
    except TimeoutError:
        if verbose:
            print("[refresh] 已有刷新在进行（busy），本次跳过")
        if outcome is not None:
            outcome.update(ok=False, attempted_profile=False, reason="busy（刷新锁被占用，未动浏览器）")
        return False
    try:
        return _refresh_impl(
            verbose=verbose,
            headless_only=headless_only,
            cooldown_hours=cooldown_hours,
            outcome=outcome,
        )
    finally:
        lock.__exit__(None, None, None)


def _refresh_impl(
    *,
    verbose: bool,
    headless_only: bool,
    cooldown_hours: float,
    outcome: dict | None,
) -> bool:
    def _done(ok: bool, attempted_profile: bool, reason: str) -> bool:
        if outcome is not None:
            outcome["ok"] = bool(ok)
            outcome["attempted_profile"] = bool(attempted_profile)
            outcome["reason"] = reason
        return bool(ok)

    def _verified(candidate: str) -> tuple[bool, str]:
        if not candidate:
            return False, "未提取到 Cookie"
        if "wr_vid=" not in candidate:
            return False, "Cookie 缺少 wr_vid"
        return verify_cookie_detail(candidate)

    _, data = load_weread_data()
    url = refresh_url_of(data)

    if not url:
        if verbose:
            print(
                "[refresh] 未配置刷新页面 URL：请在 we-mp-rss 微信读书配置页填写"
                " cookie_refresh_url（形如 https://weread.qq.com/web/mp/reader/xxxx），"
                "或设置 WEREAD_REFRESH_URL"
            )
        return _done(False, False, "未配置 cookie_refresh_url")

    # 冷却：上次刷新成功且在冷却期内，且已有 Cookie，则视为仍有效，跳过
    if cooldown_hours and cooldown_hours > 0:
        last_ts = data.get("cookie_refresh_last_ts") or 0
        has_cookie = bool((data.get("cookie") or "").strip())
        if has_cookie and last_ts and (time.time() - float(last_ts)) < cooldown_hours * 3600:
            # 冷却期内：只有经真实接口验证 Cookie 仍有效才跳过刷新；
            # 否则（已过期 / 旧时间戳是假阳性）必须强制刷新，不能被旧时间戳卡住。
            if verify_cookie_detail((data.get("cookie") or "").strip())[0]:
                if verbose:
                    print(f"[refresh] Cookie 在冷却期内（{cooldown_hours:g}h）且仍有效，跳过刷新")
                # 冷却跳过没有访问页面，不能当作 profile 存活的证据。
                return _done(True, False, "冷却期内跳过（未访问 profile）")
            if verbose:
                print("[refresh] Cookie 在冷却期内但已失效，强制刷新")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if verbose:
            print("[refresh] 未安装 playwright，请执行: pip install playwright && playwright install chromium")
        return _done(False, False, "未安装 playwright")

    profile_dir = str(config.PROFILE_DIR)
    os.makedirs(profile_dir, exist_ok=True)
    browser_path = (config.BROWSER_PATH or (data.get("browser_path") or "")).strip()

    def _launch(p, headless: bool):
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        if browser_path:
            return p.chromium.launch_persistent_context(
                profile_dir, headless=headless, executable_path=browser_path, args=args
            )
        return p.chromium.launch_persistent_context(profile_dir, headless=headless, args=args)

    def _extract_cookie_from_page(page, context, page_url: str) -> str:
        """优先从 /web/mp/articles 请求头取 Cookie，回退 context.cookies 拼接。"""
        captured: dict[str, str] = {}

        def _on_request(request):
            if "web/mp/articles" in request.url:
                captured["cookie"] = request.headers.get("cookie", "")

        page.on("request", _on_request)
        try:
            page.goto(page_url, wait_until="networkidle", timeout=60000)
        except Exception as exc:  # playwright 异常类型多样，统一兜住
            if verbose:
                print(f"[refresh] 打开页面异常: {exc}")
        cookie = captured.get("cookie", "").strip()
        if cookie:
            return cookie
        try:
            cookies = context.cookies("https://weread.qq.com")
            return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception:
            return ""

    def _try(headless: bool, wait_login: bool = False, timeout_s: int | None = None, pusher=None) -> str:
        """打开页面并提取 Cookie；wait_login=True 时保持窗口/会话等待用户扫码。"""
        from playwright.sync_api import sync_playwright

        timeout_s = timeout_s or config.SCAN_WAIT_SECONDS
        with sync_playwright() as p:
            context = _launch(p, headless=headless)
            page = context.new_page()
            # 扫码模式下绝不注入种子 Cookie：过期 Cookie 会让页面停在"已登录但失效"
            # 的状态，既出不来二维码，也会让等待逻辑误判为"已拿到 Cookie"而直接退出。
            seed_cookie = (
                dedupe_cookie((data.get("cookie") or "").strip())
                if (config.SEED_COOKIE_ENABLED and not wait_login)
                else ""
            )
            if wait_login:
                try:
                    context.clear_cookies()
                except Exception:
                    pass
            if seed_cookie:
                try:
                    cookies = []
                    for item in seed_cookie.split(";"):
                        item = item.strip()
                        if "=" in item:
                            name, _, value = item.partition("=")
                            if name.strip() and value.strip():
                                cookies.append(
                                    {"name": name.strip(), "value": value.strip(), "url": "https://weread.qq.com"}
                                )
                    if cookies:
                        context.add_cookies(cookies)
                        if verbose:
                            print(f"[refresh] 已注入种子 Cookie（{len(cookies)} 项）")
                except Exception as exc:
                    if verbose:
                        print(f"[refresh] 注入种子 Cookie 失败（不影响）: {exc}")
            try:
                cookie = dedupe_cookie(_extract_cookie_from_page(page, context, url))
                if wait_login:
                    # 扫码模式：只有"验证真实有效"的 Cookie 才算数；否则保持可见窗口，
                    # 轮询等待用户扫码登录（登录态持久化到 profile 目录）。
                    if cookie and _verified(cookie)[0]:
                        return cookie
                    # 确保窗口停在可扫码的登录入口（reader 页未登录时可能是报错页）
                    try:
                        page.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    if verbose:
                        print("[refresh] 请在弹出的浏览器窗口中用微信扫码登录微信读书…")
                    deadline = time.time() + timeout_s
                    cookie = ""
                    while time.time() < deadline:
                        if pusher is not None:
                            # 扫码窗口开着的同时把二维码实时推飞书：人不在电脑前
                            # 也能用手机扫飞书里的码远程完成重建。
                            try:
                                pusher.push_if_changed(page)
                            except Exception as exc:
                                if verbose:
                                    print(f"[refresh] 二维码推送失败（不影响扫码等待）: {exc}")
                        try:
                            cookies = context.cookies("https://weread.qq.com")
                            ck = dedupe_cookie("; ".join(f"{c['name']}={c['value']}" for c in cookies))
                        except Exception:
                            ck = ""
                        if "wr_vid=" in ck and verify_cookie_detail(ck)[0]:
                            cookie = ck
                            break
                        time.sleep(3)
                    if pusher is not None:
                        # 结果卡形成闭环：扫成功推「已恢复」，超时推「窗口已关」，
                        # 让守着飞书的人不用猜窗口那边发生了什么。
                        try:
                            if cookie:
                                pusher.push_success(extract_vid(cookie))
                            else:
                                pusher.push_timeout()
                        except Exception:
                            pass
                    return cookie
                return cookie
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    # 1) 常规无头刷新：登录态持久化在 profile，通常直接拿到有效 Cookie
    cookie = _try(headless=True, timeout_s=config.HEADLESS_TIMEOUT_SECONDS)
    verified, verify_reason = _verified(cookie)
    if verified:
        vid = save_cookie(cookie, name=data.get("name", ""), refresh_url=url)
        if verbose:
            print(f"[refresh] Cookie 已自动更新 (vid={vid})")
        return _done(True, True, "无头刷新成功")

    if verbose:
        # 把校验原因打出来：只写「-2012」而不说哪个接口判负，会让下次排查从零开始。
        print(f"[refresh] 无头刷新未通过校验: {verify_reason}")

    # 2) 未拿到有效 Cookie（或拿到但验证失败＝过期）
    if headless_only:
        if verbose:
            print(
                "[refresh] Cookie 无效/已过期。无头模式不弹窗，"
                "请在本机运行 'python refresh_cookie.py' 扫码登录后重建 profile 会话"
            )
        return _done(False, True, f"无头刷新失败（{verify_reason}）")

    # 3) 打开扫码会话：桌面宿主机弹可见窗口；无显示器的容器/NAS 走无头会话，
    #    二维码实时推飞书（推送未启用/不可用时仍按原样弹窗）。
    pusher = _make_qr_pusher(verbose)
    scan_headless = pusher is not None and _display_absent()
    if verbose:
        if scan_headless:
            print("[refresh] 未获取到有效 Cookie，当前无显示环境，登录二维码将推送到飞书，请用微信扫码…")
            print(f"[refresh] 等待扫码（最长 {config.SCAN_WAIT_SECONDS} 秒），扫码成功后自动保存 Cookie…")
        else:
            print("[refresh] 未获取到有效 Cookie，已打开浏览器窗口，请扫码登录微信读书…")
            print(f"[refresh] 等待登录（最长 {config.SCAN_WAIT_SECONDS} 秒），登录成功后自动保存 Cookie…")
    cookie = _try(headless=scan_headless, wait_login=True, pusher=pusher)
    verified, verify_reason = _verified(cookie)
    if verified:
        vid = save_cookie(cookie, name=data.get("name", ""), refresh_url=url)
        if verbose:
            print(f"[refresh] 扫码登录后 Cookie 已更新 (vid={vid})")
        return _done(True, True, "扫码重建成功")

    if verbose:
        print(f"[refresh] 等待扫码超时或 Cookie 仍无效（{verify_reason}），请检查微信读书登录状态")
    return _done(False, True, f"扫码重建失败（{verify_reason}）")


def request_host_refresh(timeout_s: int = 180) -> dict:
    """容器内调用宿主机刷新代理（**不在容器内启动浏览器**）。

    可选工具函数：如果把本项目的 weread_keepalive 包拷进容器，或想在容器侧
    脚本里触发刷新，可用它调用宿主机常驻代理。浏览器刷新动作由代理完成，
    代理把最新明文 Cookie 写回数据卷 wx.lic，容器随后读取它同步文章。

    环境变量 ``WEREAD_REFRESH_AGENT_URL``（默认 http://host.docker.internal:9876/refresh）。

    返回::

        {"triggered": bool, "ok": bool, "needs_scan": bool, "busy": bool, "message": str}
    """
    import json
    import urllib.request

    agent_url = os.environ.get("WEREAD_REFRESH_AGENT_URL", "").strip()
    if not agent_url:
        return {
            "triggered": False,
            "ok": True,
            "needs_scan": False,
            "busy": False,
            "message": "未配置 WEREAD_REFRESH_AGENT_URL，跳过自动刷新（手动模式）",
        }
    if not agent_url.endswith("/refresh"):
        agent_url = agent_url.rstrip("/") + "/refresh"
    headers = {"Content-Type": "application/json"}
    if config.AGENT_TOKEN:
        headers["X-Agent-Token"] = config.AGENT_TOKEN
    try:
        req = urllib.request.Request(agent_url, data=b"{}", method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {
            "triggered": True,
            "ok": bool(payload.get("ok")),
            "needs_scan": bool(payload.get("needs_scan")),
            "busy": bool(payload.get("busy")),
            "message": payload.get("message", ""),
        }
    except Exception as exc:
        return {
            "triggered": True,
            "ok": False,
            "needs_scan": False,
            "busy": False,
            "message": f"调用宿主机刷新代理失败: {exc}",
        }
