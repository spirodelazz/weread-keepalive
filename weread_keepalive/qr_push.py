"""登录二维码推送飞书 + 无显示环境的无头扫码会话。

为什么需要这个模块
------------------
profile 登录态失效后，唯一根治办法是打开微信读书页面扫码重建长期会话。桌面
宿主机可以弹可见窗口，但容器 / NAS / 远程服务器没有显示器，弹了也没人看得见。
登录二维码本质是一张图片：扫码会话里实时把页面上的二维码截下来推到飞书，
用户用微信「扫一扫 → 相册」识别图片完成登录，cookie 自动写回 wx.lic，
全程无需在设备前。选择器、去重、限频等参数均为生产环境实测沉淀。

飞书渠道的关键事实（决定了下面的实现分层）：

- **自定义机器人 webhook 发不了图片**：image 消息和卡片里的 img 元素都要
  ``image_key``，而 image_key 只能经自建应用上传（tenant_access_token +
  ``im:resource`` 权限）获得——所以图片上传固定走应用身份；
- **卡片投递自适应**：配了 ``FEISHU_QR_RECEIVE_ID`` / ``FEISHU_CHAT_ID`` 走
  应用消息发到指定群/个人（``im:message`` 权限）；否则走群机器人 webhook
  （``FEISHU_WEBHOOK``，可配 ``FEISHU_SECRET`` 签名）。webhook 卡片引用应用
  上传的 image_key 实测可用。

设计要点（沉淀自生产环境实战教训）：

- 二维码会过期自动刷新：按截图内容 sha256 去重 + 最小重推间隔（渲染抖动会
  造成假变化），变了才重推；单会话推送上限防刷屏；
- 首页可能不自动弹登录层：连续抓不到码时主动跳 ``#login`` 触发弹层（整轮一次）；
- 推送失败绝不影响扫码重建本身：所有推送调用点都吞异常只打日志；
- 扫码成功 / 超时都补结果卡（绿/橙），守着飞书的人不用猜会话那边发生了什么。

环境变量见 ``config.py`` 的「登录二维码推送」段。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config
from .verify import verify_cookie_detail
from .wxlic import dedupe_cookie, file_lock, load_weread_data, refresh_url_of, save_cookie

WEBHOOK_TIMEOUT_SECONDS = 20
APP_TIMEOUT_SECONDS = 20

# qrconnect iframe 整块：生产实战口径的兜底——自带白底静区 +「请使用微信扫一扫」
# 提示、尺寸充足（实测 ~300px），解码器实测可扫；透明区域可能透出底层页面内容，
# 仅在原生码图与纯元素截图都拿不到时使用。
MAIN_PANEL_SELECTORS = (
    "iframe[src*='qrconnect']",
)
# 子 frame 里的二维码本体（qrconnect iframe 内的 img.qrcode）。
# 注意：元素截图只有 ~172px 显示尺寸，而这是高密度码（模块数多），压到这个尺寸
# 后抗锯齿模糊，解码器与手机均实测扫不出（2026-10-06 用户反馈）——所以首选
# 走 _qr_bytes_from_src 直接取 src 里的原生 470px 码图，截图只作改版兜底。
CHILD_FRAME_SELECTORS = (
    "img.qrcode",
    "img[class*='qrcode']",
)
# 主 frame 里二维码直接以 img 形式存在时的候选（网页改版兜底，同为纯二维码图）。
MAIN_ELEMENT_SELECTORS = (
    "#img_login_qrcode",
    "img.img_login_qrcode",
    "img[class*='login_qrcode']",
    "img[class*='qrcode']",
    "canvas[class*='qrcode']",
)
LOGIN_PAGE_URL = "https://weread.qq.com/#login"
# 同一二维码内容的最小重推间隔：二维码元素截图可能因渲染抖动逐帧不同，
# 间隔太短会刷屏；太长则扫码人手里那帧已过期。
MIN_PUSH_INTERVAL_SECONDS = 30
# 单次扫码会话最多推几张（10 分钟窗口 × 30s 间隔理论上限 20 张，留余量）。
MAX_PUSHES = 12
# 连续抓不到二维码几次后，主动跳 #login 触发登录弹层（只做一次）。
ENSURE_LOGIN_AFTER_MISSES = 2


# ---------------------------------------------------------------------------
# 飞书 webhook 基座（签名 / 卡片组装 / 发送）
# ---------------------------------------------------------------------------

def feishu_signature(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(string_to_sign, b"", digestmod="sha256").digest()
    return base64.b64encode(digest).decode("utf-8")


def sign_feishu_payload(payload: dict, secret: str | None) -> dict:
    """按群机器人「签名校验」口径给 payload 补 timestamp/sign；secret 为空原样返回。"""
    if not (secret or "").strip():
        return payload
    timestamp = int(time.time())
    payload["timestamp"] = str(timestamp)
    payload["sign"] = feishu_signature(secret.strip(), timestamp)
    return payload


def build_feishu_card(template: str, title: str, elements: list[dict], *, wide: bool = True) -> dict:
    """组装飞书交互卡片 payload（msg_type=interactive）。

    template 是飞书色彩语义：red=要动手的故障 / orange=瞬时或观察 / green=恢复。
    """
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": wide},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "elements": elements,
        },
    }


def send_feishu_webhook(webhook: str, payload: dict) -> None:
    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8", errors="replace")
    result = json.loads(body)
    if result.get("code") not in (0, None) or result.get("StatusCode") not in (0, None):
        raise RuntimeError(f"Feishu webhook failed: {body}")


def _http_error_body(exc: urllib.error.HTTPError, max_length: int = 1000) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except OSError:
        body = ""
    if len(body) > max_length:
        return body[: max_length - 1].rstrip() + "..."
    return body


# ---------------------------------------------------------------------------
# 飞书应用身份链路（tenant token / 图片上传 / 应用消息）
# ---------------------------------------------------------------------------

_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, float]] = {}


def feishu_tenant_access_token() -> str:
    """用 FEISHU_APP_ID / FEISHU_APP_SECRET 换 tenant_access_token（带缓存）。"""
    app_id = config.FEISHU_APP_ID
    app_secret = config.FEISHU_APP_SECRET
    if not app_id or not app_secret:
        return ""
    with _token_lock:
        cached = _token_cache.get(app_id)
        if cached and cached[1] > time.time() + 300:
            return cached[0]
    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=APP_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Feishu tenant token failed: HTTP {exc.code}: {_http_error_body(exc)}") from exc
    if result.get("code") not in (0, None):
        raise RuntimeError(f"Feishu tenant token failed: {result.get('msg') or result.get('message') or result}")
    token = str(result.get("tenant_access_token") or "")
    if not token:
        raise RuntimeError("Feishu tenant token response did not include tenant_access_token.")
    expire = int(result.get("expire") or 7200)
    with _token_lock:
        _token_cache[app_id] = (token, time.time() + expire)
    return token


def _encode_multipart_form_data(
    fields: dict[str, str], files: list[tuple[str, str, str, bytes]]
) -> tuple[bytes, str]:
    """手搓 multipart/form-data（标准库没有该编码器）。"""
    boundary = f"----WereadKeepaliveBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")
    for name, filename, content_type, content in files:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def feishu_upload_image(image_bytes: bytes, filename: str = "weread-login-qrcode.png") -> str:
    """上传图片换 image_key；新旧两个上传端点逐个尝试。"""
    token = feishu_tenant_access_token()
    if not token:
        return ""
    body, boundary = _encode_multipart_form_data(
        {"image_type": "message"},
        [("image", filename, "image/png", image_bytes)],
    )
    endpoints = [
        "https://open.feishu.cn/open-apis/image/v4/put/",
        "https://open.feishu.cn/open-apis/im/v1/images",
    ]
    last_error = ""
    for endpoint in endpoints:
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=APP_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {_http_error_body(exc)}"
            continue
        if result.get("code") != 0:
            last_error = str(result.get("msg") or result.get("message") or result)
            continue
        data = result.get("data") if isinstance(result, dict) else {}
        image_key = str(data.get("image_key") or "") if isinstance(data, dict) else ""
        if image_key:
            return image_key
        last_error = "response did not include image_key"
    raise RuntimeError(f"Feishu image upload failed: {last_error}")


def feishu_receive_target() -> tuple[str, str]:
    return config.FEISHU_QR_RECEIVE_ID, (config.FEISHU_QR_RECEIVE_ID_TYPE or "chat_id")


def feishu_send_app_message(payload: dict) -> str:
    """用应用身份把卡片消息发到 FEISHU_QR_RECEIVE_ID 指向的群/个人，返回 message_id。"""
    receive_id, receive_id_type = feishu_receive_target()
    if not receive_id:
        raise RuntimeError("FEISHU_QR_RECEIVE_ID / FEISHU_CHAT_ID is required for Feishu app messages.")
    token = feishu_tenant_access_token()
    body = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(payload.get("card") or {}, ensure_ascii=False),
    }
    request = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={urllib.parse.quote(receive_id_type)}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=APP_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Feishu app message failed: HTTP {exc.code}: {_http_error_body(exc)}") from exc
    if result.get("code") != 0:
        raise RuntimeError(f"Feishu app message failed: {result.get('msg') or result.get('message') or result}")
    data = result.get("data") if isinstance(result, dict) else {}
    return str(data.get("message_id") or "")


def send_feishu_card(webhook: str | None, payload: dict) -> str | None:
    """卡片投递自适应：app 模式（或配了 receive_id）走应用消息，否则走群机器人 webhook。"""
    mode = (config.FEISHU_QR_SEND_MODE or "").strip().lower()
    receive_id, _ = feishu_receive_target()
    if mode == "app" or (mode == "" and receive_id):
        return feishu_send_app_message(payload)
    if not webhook:
        raise RuntimeError("FEISHU_WEBHOOK is required when Feishu app receive id is not configured.")
    send_feishu_webhook(webhook, payload)
    return None


def feishu_list_chats() -> list[dict]:
    """列出应用机器人所在的群（配置 FEISHU_QR_RECEIVE_ID 用）。需要 im:chat 只读权限。"""
    token = feishu_tenant_access_token()
    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/chats?page_size=50",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=APP_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Feishu list chats failed: HTTP {exc.code}: {_http_error_body(exc)}") from exc
    if result.get("code") != 0:
        raise RuntimeError(f"Feishu list chats failed: {result.get('msg') or result.get('message') or result}")
    data = result.get("data") if isinstance(result, dict) else {}
    items = data.get("items") if isinstance(data, dict) else None
    return [
        {"chat_id": str(item.get("chat_id") or ""), "name": str(item.get("name") or "")}
        for item in (items or [])
        if isinstance(item, dict)
    ]


# ---------------------------------------------------------------------------
# 二维码捕获（选择器与逐级兜底均为实测沉淀）
# ---------------------------------------------------------------------------

def _shot_first(page_like, selectors) -> bytes | None:
    for selector in selectors:
        try:
            element = page_like.query_selector(selector)
        except Exception:
            element = None
        if element is None:
            continue
        try:
            data = element.screenshot(timeout=5000)
        except Exception:
            continue
        if data:
            return data
    return None


# 从二维码 <img> 的 src 直接取原生码图（不截图）：微信 qrconnect 的码图是
# ~470x470 的原生 PNG，天然不含页面内容、无显示尺寸压缩失真。fetch 在图片
# 所在文档内发起（同源，带 cookie），结果统一转成 data URL 返回。
_QR_SRC_DATA_URL_JS = """
(selector) => {
  const img = document.querySelector(selector);
  if (!img) return null;
  const src = img.currentSrc || img.src;
  if (!src) return null;
  if (src.startsWith('data:')) return src;
  return fetch(src, { credentials: 'include' })
    .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
    .then((blob) => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    }))
    .catch((e) => 'ERR:' + e.message);
}
"""


def _qr_bytes_from_src(page_like, selector) -> bytes | None:
    """提取二维码 <img> src 指向的原生图片字节；任何失败返回 None。"""
    try:
        out = page_like.evaluate(_QR_SRC_DATA_URL_JS, selector)
    except Exception:
        return None
    if not isinstance(out, str) or not out.startswith("data:"):
        return None
    try:
        raw = base64.b64decode(out.partition(",")[2])
    except Exception:
        return None
    return raw or None


def capture_qr_bytes(page) -> bytes | None:
    """截取当前页面上的登录二维码，逐级兜底。

    优先级（2026-10-06 三次调整，依据解码器 + 真机实测）：

    1. 从 ``img.qrcode`` 的 src 直接取原生码图（~470px、无失真、无页面内容）
       ——首选。元素截图只有 ~172px 显示尺寸，高密度码被压糊，手机实测扫不出；
    2. 子 frame 的 ``img.qrcode`` 等纯码元素截图（改版兜底，尺寸小）；
    3. 主 frame 的二维码元素候选截图（改版兜底）；
    4. qrconnect iframe 整块截图（自带静区与提示，实测可扫；
       透明区域可能带出底层页面内容，仅在前三级都拿不到时使用）；
    5. 整页截图（最后兜底）。
    """
    try:
        child_frames = [frame for frame in page.frames if frame is not page.main_frame]
    except Exception:
        child_frames = []
    for frame in child_frames:
        for selector in CHILD_FRAME_SELECTORS:
            data = _qr_bytes_from_src(frame, selector)
            if data:
                return data
    for selector in CHILD_FRAME_SELECTORS:
        data = _qr_bytes_from_src(page, selector)
        if data:
            return data
    for frame in child_frames:
        data = _shot_first(frame, CHILD_FRAME_SELECTORS)
        if data:
            return data
    data = _shot_first(page, MAIN_ELEMENT_SELECTORS)
    if data:
        return data
    data = _shot_first(page, MAIN_PANEL_SELECTORS)
    if data:
        return data
    try:
        return page.screenshot(timeout=8000)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 推送器：去重 / 限频 / 上限 / 结果卡
# ---------------------------------------------------------------------------

def _send_card(title: str, body: str, image_bytes: bytes | None = None, template: str = "") -> None:
    """发一张飞书卡片。没有配置任何飞书出口时静默跳过（扫码窗口行为不受影响）。"""
    webhook = (config.FEISHU_WEBHOOK or "").strip()
    receive_id, _ = feishu_receive_target()
    if not webhook and not receive_id:
        return
    image_key = ""
    if image_bytes:
        # 图片必须经应用上传换 image_key；没配应用凭据时降级为无图卡片
        # （对登录二维码来说无图等于没用，会话入口处会用 image_delivery_ready 拦住）。
        try:
            image_key = feishu_upload_image(image_bytes)
        except Exception:
            image_key = ""
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]
    if image_key:
        elements.append(
            {
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": title},
                "mode": "fit_horizontal",
                "compact_width": True,
                "preview": True,
            }
        )
    payload = build_feishu_card(
        template or ("red" if image_key else "orange"), title, elements, wide=False
    )
    sign_feishu_payload(payload, config.FEISHU_SECRET)
    send_feishu_card(webhook, payload)


class QRFeishuPusher:
    """一个扫码会话一个实例：负责去重、限频、上限与结果卡。"""

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.last_hash = ""
        self.last_push_at = 0.0
        self.push_count = 0
        self.closed = False
        self._capture_misses = 0
        self._login_ensured = False

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[qr-push] {message}", flush=True)

    def _ensure_login_page(self, page) -> None:
        """首页可能不自动弹登录层：二维码连续抓不到时主动跳 #login 触发弹层（一次）。

        只在确认抓不到码时才动页面，且整轮只做一次——扫码等待循环里唯一的导航，
        失败也只损失提醒，不影响 cookie 轮询本身。
        """
        self._login_ensured = True
        try:
            current_url = str(getattr(page, "url", "") or "")
            if "#login" in current_url:
                return
            self._log("未捕获到二维码，跳转 #login 触发登录弹层")
            page.goto(LOGIN_PAGE_URL, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)
        except Exception as exc:
            self._log(f"跳转登录页失败（不影响扫码等待）: {exc}")

    def push_if_changed(self, page) -> bool:
        """截当前页面二维码，内容有变化且不限频时推飞书。返回是否实际推送。"""
        if self.closed or self.push_count >= MAX_PUSHES:
            return False
        data = capture_qr_bytes(page)
        if not data:
            self._capture_misses += 1
            if not self._login_ensured and self._capture_misses >= ENSURE_LOGIN_AFTER_MISSES:
                self._ensure_login_page(page)
            return False
        digest = hashlib.sha256(data).hexdigest()
        if digest == self.last_hash:
            return False
        now = time.time()
        if self.last_push_at and now - self.last_push_at < MIN_PUSH_INTERVAL_SECONDS:
            return False  # 内容变了但太频繁：多为渲染抖动，等下一轮再看
        _send_card(
            "微信读书扫码登录",
            "登录二维码已推送，请用微信扫描。\n"
            "**手机单设备**：长按保存图片到相册，微信「扫一扫 → 相册」选取；"
            "**双设备**：电脑端看码，手机直接扫。\n"
            "**此码登录的是长期会话**，扫一次即根治；二维码会自动刷新，以最新一张为准。",
            data,
        )
        self.last_hash = digest
        self.last_push_at = now
        self.push_count += 1
        self._log(f"二维码已推送飞书（第 {self.push_count}/{MAX_PUSHES} 张）")
        return True

    def push_success(self, vid: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        detail = f"（vid={vid}）" if vid else ""
        try:
            _send_card(
                "微信读书登录成功",
                f"扫码成功，profile 登录态已重建{detail}，采集将自动恢复。",
                None,
                template="green",
            )
            self._log("登录成功卡片已推送飞书")
        except Exception as exc:
            self._log(f"登录成功卡片推送失败（不影响刷新结果）: {exc}")

    def push_timeout(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            _send_card(
                "微信读书扫码超时",
                "扫码会话等待超时已关闭。下一轮刷新失败后会重新推送新二维码；"
                "也可手动触发：`python qr_login.py`（容器内 `docker compose exec weread-keepalive python qr_login.py`）。",
                None,
                template="orange",
            )
            self._log("扫码超时卡片已推送飞书")
        except Exception as exc:
            self._log(f"扫码超时卡片推送失败（不影响刷新结果）: {exc}")


def enabled() -> bool:
    return bool(config.QR_PUSH_ENABLED)


def image_delivery_ready() -> bool:
    """二维码图片能否真正送到人手里：必须配了应用凭据（上传换 image_key），
    且至少有一个投递出口（webhook 或应用消息目标）。"""
    if not (config.FEISHU_APP_ID and config.FEISHU_APP_SECRET):
        return False
    return bool((config.FEISHU_WEBHOOK or "").strip() or feishu_receive_target()[0])


def make_pusher(verbose: bool = True) -> QRFeishuPusher | None:
    """扫码会话入口用：未启用返回 None（绝不影响扫码重建本身）。"""
    if not enabled():
        return None
    return QRFeishuPusher(verbose=verbose)


# ---------------------------------------------------------------------------
# 无显示环境的无头扫码会话（容器 / NAS / 远程服务器）
# ---------------------------------------------------------------------------

_session_event = threading.Event()
_status_lock = threading.Lock()
_last_status: dict = {"running": False, "triggered_at": 0, "ok": None, "message": "尚未运行"}


def session_running() -> bool:
    return _session_event.is_set()


def session_status() -> dict:
    with _status_lock:
        status = dict(_last_status)
    status["running"] = _session_event.is_set()
    return status


def _record(ok: bool | None, message: str) -> None:
    with _status_lock:
        _last_status.update(
            running=_session_event.is_set(),
            triggered_at=_last_status.get("triggered_at") or int(time.time()),
            ok=ok,
            message=message,
        )


def start_session(reason: str = "", verbose: bool = True) -> dict:
    """后台起一个无头扫码会话（agent / daemon 用，立即返回不阻塞 HTTP）。

    返回 ``{"triggered": bool, "busy": bool, "message": str}``。单例：已有会话
    在跑时返回 busy；未配置图片投递时直接拒绝并说明缺什么。
    """
    if not config.QR_PUSH_ENABLED:
        return {"triggered": False, "busy": False, "message": "二维码推送未启用（WEREAD_QR_PUSH_ENABLED=0）"}
    if not image_delivery_ready():
        return {
            "triggered": False,
            "busy": False,
            "message": (
                "二维码推送不可用：需要飞书自建应用凭据（FEISHU_APP_ID / FEISHU_APP_SECRET，"
                "用于上传二维码图片）且配置投递出口（FEISHU_QR_RECEIVE_ID 或 FEISHU_WEBHOOK）"
            ),
        }
    if _session_event.is_set():
        return {"triggered": False, "busy": True, "message": "已有扫码会话在进行，请先完成扫码"}
    _session_event.set()
    with _status_lock:
        _last_status.update(running=True, triggered_at=int(time.time()), ok=None, message=reason or "已触发")

    def _bg():
        outcome: dict = {}
        try:
            run_login_session(verbose=verbose, outcome=outcome)
        except Exception as exc:
            outcome["ok"] = False
            outcome["reason"] = f"扫码会话异常: {exc}"
            print(f"[qr-push] 扫码会话异常: {exc}", flush=True)
        finally:
            _session_event.clear()
            _record(
                bool(outcome.get("ok")),
                str(outcome.get("reason") or ("ok" if outcome.get("ok") else "failed")),
            )

    threading.Thread(target=_bg, daemon=True).start()
    return {"triggered": True, "busy": False, "message": reason or "扫码会话已启动，二维码稍后推送飞书"}


def _launch_context(p, headless: bool):
    """与 refresher 同参的持久化上下文启动（--no-sandbox 等容器必需参数）。"""
    _, data = load_weread_data()
    browser_path = (config.BROWSER_PATH or (data.get("browser_path") or "")).strip()
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    if browser_path:
        return p.chromium.launch_persistent_context(
            str(config.PROFILE_DIR), headless=headless, executable_path=browser_path, args=args
        )
    return p.chromium.launch_persistent_context(str(config.PROFILE_DIR), headless=headless, args=args)


def _context_cookie(context) -> str:
    try:
        cookies = context.cookies("https://weread.qq.com")
        return dedupe_cookie("; ".join(f"{c['name']}={c['value']}" for c in cookies))
    except Exception:
        return ""


def run_login_session(*, verbose: bool = True, outcome: dict | None = None, timeout_s: int | None = None) -> bool:
    """无头扫码会话：截二维码推飞书 → 轮询登录 → 写回 wx.lic（同步阻塞版）。

    与 ``refresh_cookie`` 共用同一把刷新锁（profile 同一时间只允许一个浏览器），
    自带单例保护。容器场景手动触发（``python qr_login.py``）与 agent/daemon
    自动触发（``start_session``）都走这里。
    """
    if outcome is None:
        outcome = {}

    def _done(ok: bool, reason: str) -> bool:
        outcome.update(ok=bool(ok), reason=reason)
        _record(bool(ok), reason)
        return bool(ok)

    timeout_s = timeout_s or config.SCAN_WAIT_SECONDS
    if _session_event.is_set():
        return _done(False, "已有扫码会话在进行")
    if not image_delivery_ready():
        return _done(
            False,
            "二维码推送不可用：需配置 FEISHU_APP_ID / FEISHU_APP_SECRET（上传图片）"
            "及投递出口（FEISHU_QR_RECEIVE_ID 或 FEISHU_WEBHOOK）",
        )

    try:
        lock = file_lock(str(config.LIC_PATH) + ".refresh.lock", block=False)
        lock.__enter__()
    except TimeoutError:
        return _done(False, "刷新锁被占用（可能正在刷新），稍后再试")

    _session_event.set()
    try:
        return _session_impl(verbose=verbose, timeout_s=timeout_s, outcome=outcome, _done=_done)
    finally:
        _session_event.clear()
        _record(outcome.get("ok"), str(outcome.get("reason") or ""))
        lock.__exit__(None, None, None)


def _session_impl(*, verbose: bool, timeout_s: int, outcome: dict, _done) -> bool:
    import os

    from playwright.sync_api import sync_playwright

    _, data = load_weread_data()
    url = refresh_url_of(data)
    os.makedirs(str(config.PROFILE_DIR), exist_ok=True)
    pusher = QRFeishuPusher(verbose=verbose)

    with sync_playwright() as p:
        context = _launch_context(p, headless=True)
        try:
            page = context.new_page()
            # 先不动现有 cookie 探一次：profile 会话其实还活着就不用扫码（清了就毁了）
            try:
                page.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                if verbose:
                    print(f"[qr-push] 打开微信读书首页异常: {exc}")
            ck = _context_cookie(context)
            if "wr_vid=" in ck and verify_cookie_detail(ck)[0]:
                vid = save_cookie(ck, name=data.get("name", ""), refresh_url=url)
                if verbose:
                    print(f"[qr-push] 登录态仍有效，无需扫码 (vid={vid})")
                try:
                    pusher.push_success(vid)
                except Exception:
                    pass
                return _done(True, "登录态仍有效，无需扫码")

            # 会话已死：清掉残cookie → 直达 #login 触发登录弹层 → 推送二维码
            try:
                context.clear_cookies()
            except Exception:
                pass
            try:
                page.goto(LOGIN_PAGE_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
            except Exception as exc:
                if verbose:
                    print(f"[qr-push] 打开登录页异常（推送兜底会继续尝试）: {exc}")

            try:
                pusher.push_if_changed(page)
            except Exception as exc:
                if verbose:
                    print(f"[qr-push] 二维码首次推送失败（会话继续重试）: {exc}")

            if verbose:
                print(f"[qr-push] 二维码已推送飞书，等待扫码（最长 {timeout_s} 秒）…")
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if not pusher.closed:
                    try:
                        pusher.push_if_changed(page)
                    except Exception as exc:
                        if verbose:
                            print(f"[qr-push] 二维码推送失败（不影响扫码等待）: {exc}")
                ck = _context_cookie(context)
                if "wr_vid=" in ck and verify_cookie_detail(ck)[0]:
                    vid = save_cookie(ck, name=data.get("name", ""), refresh_url=url)
                    if verbose:
                        print(f"[qr-push] 扫码登录成功，Cookie 已写回 (vid={vid})")
                    try:
                        pusher.push_success(vid)
                    except Exception:
                        pass
                    return _done(True, "扫码登录成功")
                time.sleep(3)

            try:
                pusher.push_timeout()
            except Exception:
                pass
            return _done(False, f"等待扫码超时（{timeout_s}s）")
        finally:
            try:
                context.close()
            except Exception:
                pass
