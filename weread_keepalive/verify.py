"""Cookie 真实接口验证：确认 Cookie 真能从微信读书拉到数据。

只检查 Cookie 里有没有 ``wr_vid=`` 远远不够：过期 Cookie 同样带 wr_vid，
服务端会以 -2012/-2041 等拒绝。刷新前后都必须用真实接口验证，避免把过期
Cookie 误判为有效（假阳性），让保活链路在假阳性里空转。

判定顺序对齐 we-mp-rss 上游 ``driver/weread_qr.py::_verify_cookies``（实测可用）：

1. 主判据 ``/web/shelf/sync``：``userVid`` 必须传**空字符串**，非空反而会触发
   -2012「登录超时」；
2. 兜底 ``/web/mp/articles``：用**本账号实际订阅**的公众号 bookId。不要沿用
   教程/示例里硬编码的别人的 bookId——那会让接口恒返 -2041（「该公众号不属于
   当前用户」），把有效登录态误判成失效（假阴性），等于没有兜底。

两个接口都带浏览器同款 Origin/Referer，服务端才会按网页会话而不是游客处理。
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from . import config

SHELF_SYNC_URL = "https://weread.qq.com/web/shelf/sync"
MP_ARTICLES_URL = "https://weread.qq.com/web/mp/articles"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _book_ids_from_feeds_file() -> list[str]:
    """从外部订阅文件（可选）提取 wechat 平台的公众号 bookId。

    兼容的 JSON 结构（与常见 RSS 订阅管理工具的导出格式一致）：
    ``{"feeds": [{"platform": "wechat", "id": "wechat-MP_WXS_xxxx"}, ...]}``
    """
    if not config.FEEDS_FILE:
        return []
    try:
        payload = json.loads(Path(config.FEEDS_FILE).read_text(encoding="utf-8"))
        feeds = payload.get("feeds") if isinstance(payload, dict) else None
    except (OSError, ValueError, AttributeError):
        return []
    ids: list[str] = []
    for feed in feeds or []:
        if not isinstance(feed, dict) or feed.get("platform") != "wechat":
            continue
        feed_id = str(feed.get("id") or "")
        mp_id = feed_id[len("wechat-"):] if feed_id.startswith("wechat-") else ""
        if mp_id and mp_id not in ids:
            ids.append(mp_id)
    return ids


def mp_book_ids() -> list[str]:
    """兜底校验用的公众号 bookId 列表。

    来源优先级：``WEREAD_VERIFY_BOOK_IDS``（逗号分隔）→ 订阅文件里的 wechat 订阅。
    取不到就返回空列表：此时跳过兜底接口，只用主判据判定。
    """
    ids = list(config.VERIFY_BOOK_IDS)
    for mp_id in _book_ids_from_feeds_file():
        if mp_id not in ids:
            ids.append(mp_id)
    return ids


def verify_cookie_detail(cookie: str, timeout: int = 30) -> tuple[bool, str]:
    """实打实请求一次微信读书接口，确认 Cookie 真能拉到数据。

    返回 ``(是否有效, 原因)``；原因串用于日志排查，例如
    ``shelf/sync: errCode=-2012``。判定失败时会把是哪个接口、什么原因都列出来，
    避免下次排查从零开始。
    """
    if not cookie:
        return False, "Cookie 为空"
    if "wr_vid=" not in cookie:
        return False, "Cookie 缺少 wr_vid"

    headers = {
        "Cookie": cookie,
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://weread.qq.com",
        "Referer": "https://weread.qq.com/",
    }

    def _probe(url: str, params: dict, fields: set[str]) -> tuple[bool, str]:
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            payload = r.json()
        except requests.RequestException as exc:
            return False, f"请求异常 {type(exc).__name__}: {exc}"
        except ValueError:
            return False, f"响应非 JSON（HTTP {getattr(r, 'status_code', '?')}）"
        if not isinstance(payload, dict):
            return False, f"响应非 JSON 对象（HTTP {getattr(r, 'status_code', '?')}）"
        code = payload.get("errCode", payload.get("errcode", 0))
        if code:
            return False, f"errCode={code}"
        if any(key in payload for key in fields):
            return True, "ok"
        return False, f"HTTP {getattr(r, 'status_code', '?')} 响应缺少数据字段"

    # 主判据：shelf/sync，userVid 必须传空字符串
    ok, reason = _probe(
        SHELF_SYNC_URL,
        {"userVid": "", "synckey": 0},
        {"books", "bookCount", "pureBookCount", "synckey"},
    )
    if ok:
        return True, "shelf/sync ok"

    reasons = [f"shelf/sync: {reason}"]
    book_ids = mp_book_ids()
    if not book_ids:
        reasons.append("未配置 WEREAD_VERIFY_BOOK_IDS，跳过 mp/articles 兜底")
    for book_id in book_ids:
        ok, detail = _probe(
            MP_ARTICLES_URL,
            {"bookId": book_id, "offset": 0},
            {"reviews", "articles", "synckey", "bookId"},
        )
        if ok:
            return True, f"mp/articles({book_id}) ok"
        reasons.append(f"mp/articles({book_id}): {detail}")
    return False, "; ".join(reasons)


def verify_cookie(cookie: str, timeout: int = 30) -> bool:
    """``verify_cookie_detail`` 的布尔包装（轮询场景用它，不打印原因）。"""
    return verify_cookie_detail(cookie, timeout=timeout)[0]
