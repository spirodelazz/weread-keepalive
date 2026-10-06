"""宿主机刷新健康状态：宿主机写入，监控与外部系统只读。

为什么需要这个文件
------------------
微信读书有**两套互不相干的会话**：

1. **profile 网页会话**（本机 Chrome 持久化 profile，长命）——采集链路真正依赖的
   登录态，由计划任务每 15 分钟无头刷新维护；
2. **API 扫码会话**（we-mp-rss 的 /weread/qr/code，wr_skey 约 90 分钟）——只能临时救急。

profile 会话彻底失效后，无头刷新会**连续失败**。外部监控据此判断 profile 是不是
真的死了：连续失败达到阈值时提示用户回宿主机重建 profile，而不是再发一个只能撑
90 分钟的短命二维码——那是「一直要扫码」的跑步机。

契约（JSON）
-----------
::

    {
      "last_attempt_at": int,       # 最近一次刷新尝试（unix 秒）
      "last_ok": bool,              # 最近一次是否成功
      "last_success_at": int,       # 最近一次成功（unix 秒，0 表示从未成功）
      "consecutive_failures": int,  # 连续失败次数（成功即清零）
      "source": str,                # 写入方：task / gui / agent
      "message": str                # 最近一次结果摘要，便于排查
    }

只读方约定：字段缺失或文件不存在一律按「状态未知」处理，不要把缺文件误判成
profile 已死。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import config
from .wxlic import file_lock

_STATE_THREAD_LOCK = __import__("threading").Lock()


def state_path() -> Path:
    return Path(config.STATE_FILE)


def load() -> dict:
    try:
        payload = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_json(path: Path, payload: dict) -> None:
    """mkstemp 唯一临时名 + os.replace：固定 .tmp 名在并发/崩溃残留时会互相覆盖。"""
    directory = path.parent if str(path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def record_attempt(ok: bool, *, source: str = "task", message: str = "") -> dict:
    """记录一次宿主机刷新结果并原子落盘，返回写入后的状态。

    成功清零连续失败计数；失败累加。last_success_at 只在成功时推进。
    写入方有多个进程（计划任务 / GUI / 常驻代理），读-改-写必须串行，
    否则 consecutive_failures 会丢更新。锁长期争不到视为异常环境，直接抛出
    （宁可丢一次记录，也不要串行化错乱）。
    """
    path = state_path()
    with _STATE_THREAD_LOCK, file_lock(str(path) + ".lock", block=True):
        state = load()
        now = int(time.time())
        failures = int(state.get("consecutive_failures") or 0)
        state.update(
            {
                "last_attempt_at": now,
                "last_ok": bool(ok),
                "consecutive_failures": 0 if ok else failures + 1,
                "source": str(source or ""),
                "message": str(message or "")[:300],
            }
        )
        if ok:
            state["last_success_at"] = now
        else:
            state.setdefault("last_success_at", int(state.get("last_success_at") or 0))
        _atomic_write_json(path, state)
        return state


def consecutive_failures(state: dict | None = None) -> int:
    payload = load() if state is None else state
    if not isinstance(payload, dict):
        return 0
    try:
        return max(int(payload.get("consecutive_failures") or 0), 0)
    except (TypeError, ValueError):
        return 0


def profile_dead(state: dict | None, *, threshold: int, max_age_seconds: int) -> tuple[bool, str]:
    """判断宿主机 profile 会话是否已连续失败到「必须人工重建」的程度。

    返回 (是否已死, 原因描述)。状态文件缺失、计数未达阈值、或最后一次尝试过旧
    （说明定时任务本身没在跑，不能据此下结论）时一律返回 False。
    """
    payload = load() if state is None else state
    if not isinstance(payload, dict) or not payload:
        return False, "宿主机刷新状态未知（无状态文件）"

    failures = consecutive_failures(payload)
    if failures < max(int(threshold), 1):
        return False, f"宿主机无头刷新连续失败 {failures}/{max(int(threshold), 1)}"

    last_attempt = int(payload.get("last_attempt_at") or 0)
    if not last_attempt:
        return False, "宿主机刷新状态缺少 last_attempt_at"
    age = int(time.time()) - last_attempt
    if age > max(int(max_age_seconds), 60):
        # 定时任务停摆时计数会停在旧值，此时不能断定 profile 已死。
        return False, f"宿主机刷新状态过期（{age}s 未更新）"

    detail = (
        f"宿主机无头刷新连续失败 {failures} 次"
        f"（最近 {age}s 前，source={payload.get('source') or '-'}，"
        f"msg={payload.get('message') or '-'}）"
    )
    return True, detail
