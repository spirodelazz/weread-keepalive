"""wx.lic 读写：we-mp-rss 的微信读书凭据文件（YAML 格式）。

文件结构（we-mp-rss 上游约定，本工具只动 weread_data，其余原样保留）::

    weread_data:
      cookie: "wr_vid=...; wr_skey=...; ..."
      vid: "123456"
      name: "微信读书昵称"
      cookie_refresh_url: "https://weread.qq.com/web/mp/reader/xxxx"
      browser_path: ""
      cookie_refresh_last_ts: 1760000000.0

关键工程决策（均来自长期生产环境实战的教训）：

- **原子写**：临时文件必须用 mkstemp 唯一命名。固定 ``{lic}.tmp`` 名在两个写者
  （计划任务进程 / 常驻代理线程）并发时会互相覆写同一临时文件，把交错内容 replace
  进线上 wx.lic，容器下一轮读到的就是一坨坏 YAML。
- **读-改-写整段持独占锁**：否则两个写者各自读到旧文档，后写的会把先写的另一部分
  更新盖掉。
- **同步 vid**：只更新 cookie 不更新 vid，换账号扫码后会残留上一个账号的 vid。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path

import yaml

from . import config


def extract_vid(cookie: str) -> str:
    """从 Cookie 字符串中提取 wr_vid。"""
    for item in (cookie or "").split(";"):
        item = item.strip()
        if item.startswith("wr_vid="):
            return item[len("wr_vid="):].strip()
    return ""


def dedupe_cookie(cookie: str) -> str:
    """去掉重复键（浏览器注入种子后再回抓可能产生重复 wr_vid/wr_skey 等），保留首次出现。

    重复键会导致服务端取到第一个（可能是过期）值，故保存与注入前都需去重。
    """
    kept: dict[str, str] = {}
    for item in (cookie or "").split(";"):
        item = item.strip()
        if "=" in item:
            key, _, value = item.partition("=")
            key = key.strip()
            if key and key not in kept:
                kept[key] = f"{key}={value.strip()}"
    return "; ".join(kept.values())


def file_lock(path: str | Path, *, block: bool):
    """跨进程独占文件锁（Windows msvcrt / posix fcntl），返回已加锁的 context manager。

    block=False 时争用直接抛 TimeoutError；block=True 最多等 10 秒。
    调用方负责 __exit__。
    """
    path = str(path)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle = open(path, "a+")

    def _try_lock() -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock() -> None:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    deadline = time.time() + (10.0 if block else 0.0)
    while not _try_lock():
        if time.time() >= deadline:
            handle.close()
            raise TimeoutError(f"另一个微信读书刷新占着锁: {path}")
        time.sleep(0.2)

    @contextlib.contextmanager
    def _managed():
        try:
            yield handle
        finally:
            _unlock()
            handle.close()

    return _managed()


def read_lic(lic_path: Path | str | None = None) -> dict:
    """读取 wx.lic（YAML），文件不存在返回空 dict。"""
    lic_path = Path(lic_path) if lic_path else config.LIC_PATH
    if not lic_path.exists():
        return {}
    try:
        with open(lic_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return {}


def load_weread_data(lic_path: Path | str | None = None) -> tuple[dict, dict]:
    """返回 (doc, data)：doc 为整个 YAML 文档，data 为 weread_data 字典。

    兼容 weread_data 存成 JSON 字符串的旧写法。
    """
    doc = read_lic(lic_path)
    data = doc.get("weread_data", {})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    return doc, data


def write_lic(lic_path: Path | str, doc: dict) -> None:
    """原子写入：mkstemp 唯一临时文件 + os.replace。

    Windows 下 wx.lic 正被 we-mp-rss 容器以非 share-delete 方式打开时
    os.replace 会抛错，此时清掉自己的临时文件再上抛，不留垃圾。
    """
    lic_path = Path(lic_path)
    directory = lic_path.parent if str(lic_path.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".wx.lic.", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, lic_path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_cookie(
    cookie: str,
    name: str = "",
    lic_path: Path | str | None = None,
    refresh_url: str = "",
) -> str:
    """把最新 cookie 写回 wx.lic 的 weread_data（保留文档其他部分），返回 vid。

    读-改-写整段持独占锁。同时同步 vid 字段与 cookie_refresh_last_ts（冷却用）。
    若文件里还没有 cookie_refresh_url 且传入了 refresh_url，则一并写入。

    写入前先去重：重复键会导致服务端取到第一个（可能是过期）值。
    """
    lic_path = Path(lic_path) if lic_path else config.LIC_PATH
    cookie = dedupe_cookie(cookie)
    lock = file_lock(str(lic_path) + ".lock", block=True)
    with lock:
        doc, data = load_weread_data(lic_path)
        data["cookie"] = cookie
        vid = extract_vid(cookie)
        if vid:
            data["vid"] = vid
        if name:
            data["name"] = name
        if refresh_url and not (data.get("cookie_refresh_url") or "").strip():
            data["cookie_refresh_url"] = refresh_url
        data["cookie_refresh_last_ts"] = time.time()
        doc["weread_data"] = data
        write_lic(lic_path, doc)
        return vid


def refresh_url_of(data: dict) -> str:
    """取本次刷新应打开的页面 URL：环境变量覆盖 > wx.lic 的 cookie_refresh_url。

    注意必须是 reader 页（https://weread.qq.com/web/mp/reader/xxxx），
    不要配成带 bookId 的 /web/mp/articles 接口地址——接口地址会被重定向且不是给人看的页面。
    """
    return (config.REFRESH_URL_OVERRIDE or (data.get("cookie_refresh_url") or "")).strip()
