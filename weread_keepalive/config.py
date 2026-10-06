"""集中管理全部环境变量配置。

约定：所有路径默认相对本仓库根；生产环境通过 .env / 系统环境变量覆盖，
指向 we-mp-rss 数据卷中的真实文件（wx.lic 必须与容器 /app/data 指向同一物理目录）。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(key: str, default: str = "") -> str:
    value = (os.environ.get(key) or "").strip()
    return value if value else default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(_env(key, str(default))), minimum)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except (TypeError, ValueError):
        return default


def load_env_file(path: Path | str | None = None) -> None:
    """把 .env 中尚未存在的变量注入环境（只读不覆盖：显式设置的环境变量优先）。

    宿主机代理与计划任务共用同一份 .env，避免两边各配一次导致不一致。
    """
    path = Path(path) if path else ROOT / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

# wx.lic：we-mp-rss 数据卷中的微信读书凭据文件（YAML）。必配项——独立部署时
# 必须显式指向 we-mp-rss 容器挂载的 data/wx.lic 同一物理文件。
LIC_PATH = Path(_env("WEREAD_LIC_PATH", str(ROOT / "data" / "wx.lic")))

# 持久化 Chrome profile：持有真实网页登录会话（长命），是保活的根基。
PROFILE_DIR = Path(_env("WEREAD_PROFILE_DIR", str(ROOT / "data" / "weread-chrome-profile")))

# 刷新状态文件：宿主机写入，监控/外部系统读取（契约见 state.py）。
STATE_FILE = Path(_env("WEREAD_HOST_REFRESH_STATE_FILE", str(ROOT / "data" / "weread_host_refresh_state.json")))

# 刷新日志目录：按日落轮转。
LOG_DIR = Path(_env("WEREAD_LOG_DIR", str(ROOT / "data" / "logs")))

# ---------------------------------------------------------------------------
# 刷新行为
# ---------------------------------------------------------------------------

# 刷新页面 URL（微信读书 reader 页）。默认读 wx.lic 的 weread_data.cookie_refresh_url
# （we-mp-rss 配置页写入）；本变量优先级更高，可覆盖。
REFRESH_URL_OVERRIDE = _env("WEREAD_REFRESH_URL")

# 本机 Chrome 可执行文件路径。留空时使用 Playwright 自带 Chromium。
BROWSER_PATH = _env("WEREAD_BROWSER_PATH")

# 兜底校验用的公众号 bookId（逗号分隔，形如 MP_WXS_xxxx）。
# /web/mp/articles 校验必须用自己账号订阅的 bookId：用别人的账号会恒返 -2041，
# 把有效登录态误判成失效。解析时按首次出现顺序去重。
VERIFY_BOOK_IDS = list(dict.fromkeys(
    p.strip() for p in _env("WEREAD_VERIFY_BOOK_IDS").split(",") if p.strip()
))

# 可选：外部订阅文件（JSON，{"feeds":[{"platform":"wechat","id":"wechat-MP_WXS_xxx"}]}），
# 与常见 RSS 订阅管理工具的导出格式兼容。留空则只用 VERIFY_BOOK_IDS。
FEEDS_FILE = Path(_env("WEREAD_FEEDS_FILE"))

# 种子注入（默认关）：向浏览器注入 wx.lic 的 API Cookie 会覆盖 profile 自己的
# 真实网页会话，把长命会话降级成 90 分钟短命会话——除非明确知道自己在做什么，
# 否则不要打开。
SEED_COOKIE_ENABLED = _env_bool("WEREAD_SEED_COOKIE_ENABLED", False)

# 计划任务默认冷却小时数（agent 触发时传 0 强制真刷新）。
COOLDOWN_HOURS = _env_float("WEREAD_COOLDOWN_HOURS", 6.0)

# 无头刷新单次超时（秒）。
HEADLESS_TIMEOUT_SECONDS = _env_int("WEREAD_HEADLESS_TIMEOUT_SECONDS", 300, minimum=30)
# 扫码重建等待时长（秒）。
SCAN_WAIT_SECONDS = _env_int("WEREAD_SCAN_WAIT_SECONDS", 600, minimum=60)

# ---------------------------------------------------------------------------
# 常驻代理（agent）
# ---------------------------------------------------------------------------

# 默认只绑 127.0.0.1 是正确配置：Docker Desktop 下容器内 host.docker.internal
# 会转发到宿主机回环，容器可以直接访问。绑非回环地址必须设置 token，否则拒绝启动。
AGENT_HOST = _env("WEREAD_AGENT_HOST", "127.0.0.1")
AGENT_PORT = _env_int("WEREAD_AGENT_PORT", 9876, minimum=1)
# token 鉴权：设置后 /refresh 必须携带 X-Agent-Token 头或 ?token= 参数。
AGENT_TOKEN = _env("WEREAD_AGENT_TOKEN") or _env("WEREAD_HOST_AGENT_TOKEN")
# 来源 IP/CIDR 白名单（逗号分隔），空表示不限制。
AGENT_ALLOW_CIDRS = _env("WEREAD_AGENT_ALLOW_CIDRS")
AGENT_LOG_FILE = Path(_env("WEREAD_AGENT_LOG", str(LOG_DIR / "weread-keepalive-agent.log")))

# ---------------------------------------------------------------------------
# 探活监控（monitor）
# ---------------------------------------------------------------------------

MONITOR_INTERVAL_SECONDS = _env_int("WEREAD_MONITOR_INTERVAL_SECONDS", 900, minimum=60)
# 连续失败多少次才发告警（容忍网络抖动与刷新间隙）。
MONITOR_ALERT_FAILURES = _env_int("WEREAD_MONITOR_ALERT_FAILURES", 2, minimum=1)
# 同一问题告警的重发冷却（秒），默认 6 小时，避免刷屏。
MONITOR_ALERT_COOLDOWN_SECONDS = _env_int("WEREAD_MONITOR_ALERT_COOLDOWN_SECONDS", 21600, minimum=60)
MONITOR_STATE_FILE = Path(_env("WEREAD_MONITOR_STATE_FILE", str(ROOT / "data" / "weread_keepalive_monitor.json")))

# 通用 webhook 告警：POST JSON {"title": ..., "text": ...}
NOTIFY_WEBHOOK_URL = _env("WEREAD_NOTIFY_WEBHOOK")
# 飞书群机器人 webhook（https://open.feishu.cn/open-apis/bot/v2/hook/xxx）
NOTIFY_FEISHU_WEBHOOK = _env("WEREAD_NOTIFY_FEISHU_WEBHOOK")
# 企业微信群机器人 webhook（https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx）
NOTIFY_WECOM_WEBHOOK = _env("WEREAD_NOTIFY_WECOM_WEBHOOK")

# ---------------------------------------------------------------------------
# 登录二维码推送（飞书卡片，方案与 robin 项目生产实现同源）
# ---------------------------------------------------------------------------
# 背景：无桌面环境（容器 / NAS / 远程服务器）弹不了扫码窗口，profile 登录态
# 失效后无人能扫。登录二维码本质是一张图片——扫码会话里实时把二维码截下来
# 推到飞书，用户用微信「扫一扫 → 相册」识别图片即可远程重建长期会话。
#
# 渠道能力差异（决定了配置项）：
# - 飞书**自定义机器人 webhook 发不了图片**（image_key 必须经自建应用上传获得），
#   所以图片上传固定走自建应用（FEISHU_APP_ID / FEISHU_APP_SECRET）；
# - 卡片投递自适应：配了 FEISHU_QR_RECEIVE_ID / FEISHU_CHAT_ID 走应用消息发到
#   指定群/个人，否则走群机器人 webhook（FEISHU_WEBHOOK，可配 FEISHU_SECRET 签名）。

# 扫码会话内推送二维码（默认开；没配任何飞书出口时自动静默跳过，不影响扫码本身）
QR_PUSH_ENABLED = _env_bool("WEREAD_QR_PUSH_ENABLED", True)
# 无头刷新失败需要扫码时自动推送（容器/NAS 无人值守场景开启；桌面宿主机默认仍弹窗）
QR_PUSH_ON_EXPIRE = _env_bool("WEREAD_QR_PUSH_ON_EXPIRE", False)

# 飞书群机器人 webhook（卡片投递出口之一；留空则回退读 WEREAD_NOTIFY_FEISHU_WEBHOOK）
FEISHU_WEBHOOK = _env("FEISHU_WEBHOOK") or NOTIFY_FEISHU_WEBHOOK
# 群机器人签名校验密钥（webhook 开了签名校验才需要）
FEISHU_SECRET = _env("FEISHU_SECRET")
# 飞书自建应用凭据：上传二维码图片换 image_key 必需（应用需开机器人能力 + im:resource 权限）
FEISHU_APP_ID = _env("FEISHU_APP_ID")
FEISHU_APP_SECRET = _env("FEISHU_APP_SECRET")
# 应用消息投递目标（群 chat_id 或 open_id；用 `python qr_login.py --list-chats` 查询）
FEISHU_QR_RECEIVE_ID = _env("FEISHU_QR_RECEIVE_ID") or _env("FEISHU_CHAT_ID")
FEISHU_QR_RECEIVE_ID_TYPE = _env("FEISHU_QR_RECEIVE_ID_TYPE", "chat_id")
# 投递模式：app=强制走应用消息；webhook=强制走群机器人；空=自适应（配了 receive_id 走 app）
FEISHU_QR_SEND_MODE = _env("FEISHU_QR_SEND_MODE")

# 同一 profile 连两次扫码推送会话的最小间隔（秒）：profile 已死时每个刷新周期
# 都触发会徒增打扰，冷却期内静默跳过。
QR_SESSION_COOLDOWN_SECONDS = _env_int("WEREAD_QR_SESSION_COOLDOWN_SECONDS", 1800, minimum=60)

# ---------------------------------------------------------------------------
# 容器守护（Docker 入口 start_daemon.py）
# ---------------------------------------------------------------------------

# 容器内定时无头刷新间隔（秒），等价宿主机计划任务的 15 分钟。
DAEMON_REFRESH_SECONDS = _env_int("WEREAD_DAEMON_REFRESH_SECONDS", 900, minimum=300)
