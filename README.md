<div align="center">

# weread-keepalive

**微信读书登录态自动续期 · 为 we-mp-rss 的 `weread_mp` 采集模式护航**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](#环境要求)
[![Playwright](https://img.shields.io/badge/playwright-chromium-2EAD33.svg)](#环境要求)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED.svg)](#docker-部署)
[![CI](https://github.com/spirodelazz/weread-keepalive/actions/workflows/ci.yml/badge.svg)](https://github.com/spirodelazz/weread-keepalive/actions/workflows/ci.yml)

</div>

定时用 Playwright 无头访问微信读书页面保活，收割并真实验证 Cookie 后原子写回 `wx.lic`，让 [we-mp-rss](https://github.com/rachelos/we-mp-rss) 的微信读书采集模式无感续期。

`wr_skey` 只有约 90 分钟寿命，而 we-mp-rss 的登录状态只是本地时间戳、从不真实校验。本项目用本机浏览器的持久化 profile 持有长命网页会话，把它变成 `wx.lic` 里始终新鲜的 Cookie——采集端不需要任何改动。

## 功能特性

- **无头保活**：每 15 分钟访问一次 reader 页，保证 90 分钟会话窗口内始终有访问。
- **真实验证**：不只看 Cookie 有没有 `wr_vid`，而是请求真实接口确认有效。
- **原子写回**：只改 `wx.lic` 的 `weread_data`，唯一临时文件 + `os.replace` + 跨进程锁，容器不会读到半份 YAML。
- **三种运行方式**：系统计划任务、常驻 HTTP 代理、容器三合一守护，按部署形态选。
- **探活告警**：失败先自愈刷新，连续失败推送到飞书 / 企业微信 / 通用 webhook。
- **远程扫码**：登录态失效时把二维码推到飞书，手机扫码即可重建，无需到设备前。

## 环境要求

- Python 3.10+
- Playwright + Chromium（`playwright install chromium`），或本机已安装的 Chrome
- 可选：Docker / Docker Compose（NAS 与无桌面服务器）
- 可选：飞书自建应用（远程扫码推送）

## 安装

```bash
git clone https://github.com/spirodelazz/weread-keepalive.git
cd weread-keepalive
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

也可以 `pip install .` 获得命令行入口 `weread-keepalive-refresh` / `-agent` / `-monitor` / `-qr-login` / `-daemon`。

## 快速开始（桌面宿主机）

**1. 配置 `.env`**

`WEREAD_LIC_PATH` 必配，指向 we-mp-rss 数据卷里的 `wx.lic`（与容器 `/app/data` 同一物理目录）。建议再配两项：

- `WEREAD_VERIFY_BOOK_IDS`：你自己在微信读书里关注的公众号 bookId（逗号分隔）。**不要填别人的**，校验会恒返 `-2041`。
- `WEREAD_BROWSER_PATH`：本机 Chrome 路径（留空用 Playwright 自带 Chromium）。

**2. 首次扫码**

```bash
python refresh_cookie.py          # 弹出浏览器，微信扫码，自动保存
python refresh_cookie.py --status # 查看登录态验证结果
```

**3. 启用定时续期（Windows）**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

每 15 分钟无头刷新一次（`--cooldown-hours 0` 强制每次真实访问页面）。

**4. 可选：常驻代理 / 探活告警**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1  # 开机自启代理
python start_monitor.py --once   # 探活一轮（去掉 --once 即常驻）
```

容器侧需要主动刷新时：`curl -X POST http://host.docker.internal:9876/refresh`（配了 token 加 `-H "X-Agent-Token: ..."`）。

## Docker 部署

适用于 NAS / 无桌面 Linux 服务器：浏览器与 profile 都在容器内，扫码靠飞书远程完成。

```bash
cp .env.example .env      # 至少填 WEREAD_AGENT_TOKEN + 飞书三项
docker compose up -d --build
```

容器入口 `start_daemon.py` 即三合一守护（定时刷新 + 探活告警 + 刷新代理）。首次扫码任选一种触发：

- **自动**：compose 已设 `WEREAD_QR_PUSH_ON_EXPIRE=1`，刷新失败时自动推送；
- **手动**：`docker compose exec weread-keepalive python qr_login.py`；
- **HTTP**：`curl -X POST http://127.0.0.1:9876/login -H "X-Agent-Token: 你的token"`。

飞书收到卡片后，单设备长按保存图片、用微信「扫一扫 → 相册」识别；双设备直接扫屏幕。**以最新一张为准**。自建应用的创建与权限配置见[飞书应用](docs/architecture.md#飞书应用远程扫码)。

> 宿主机与容器**不要共用同一份 profile**：宿主系统凭据（DPAPI / 钥匙串）加密的 cookie 容器解不开，反之亦然。

## 配置

全部变量与注释见 [`.env.example`](.env.example)。常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WEREAD_LIC_PATH` | `./data/wx.lic` | **必配**，we-mp-rss 的 wx.lic |
| `WEREAD_PROFILE_DIR` | `./data/weread-chrome-profile` | 持久化 profile，勿清理 |
| `WEREAD_VERIFY_BOOK_IDS` | 空 | 兜底校验 bookId，必须是自己关注的 |
| `WEREAD_COOLDOWN_HOURS` | 6 | 冷却小时数，agent / monitor 触发时强制 0 |
| `WEREAD_AGENT_HOST` / `PORT` | `127.0.0.1` / `9876` | 代理监听 |
| `WEREAD_AGENT_TOKEN` | 空 | `/refresh`、`/login` 鉴权 |
| `WEREAD_MONITOR_INTERVAL_SECONDS` | 900 | 探活间隔 |
| `WEREAD_QR_PUSH_ON_EXPIRE` | false | 刷新失败需扫码时自动推送（容器场景开启） |
| `FEISHU_APP_ID` / `SECRET` / `QR_RECEIVE_ID` | 空 | 飞书自建应用与投递目标 |

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 存活探针：`busy` / `scan_pending` / `qr_session_running` / `token_required` |
| `GET` / `POST` | `/refresh` | verify-first 刷新，可带 `?token=` 或 `X-Agent-Token` |
| `POST` | `/login` | 触发一次二维码推送会话 |
| `GET` | `/login` | 查询扫码会话状态 |

## 与 we-mp-rss 对接

1. 采集模式：`WE_MP_RSS_GATHER_MODEL=weread_mp`，`WE_MP_RSS_GATHER_CONTENT=True`。
2. **`WEREAD_COOKIE` 必须留空**，否则环境变量会覆盖扫码后写入的 `wx.lic`。
3. 本工具只写 `wx.lic` 的 `weread_data`，其余内容原样保留。
4. we-mp-rss 自带的过期通知可保留作兜底，配置好本工具后几乎不应再触发。

## 数据与日志

| 路径 | 内容 |
| --- | --- |
| `data/weread_host_refresh_state.json` | 刷新状态契约，供监控判断会话过期 / profile 死亡 |
| `data/logs/` | 代理与刷新日志 |
| `data/weread-chrome-profile/` | 登录态 profile，**请勿清理**，丢失需重新扫码 |

## 项目结构

```
weread-keepalive/
├── weread_keepalive/      refresher / verify / wxlic / state / agent / monitor / qr_push / daemon / config
├── refresh_cookie.py      CLI：刷新 / 扫码 / 状态
├── qr_login.py            CLI：扫码会话、--list-chats
├── start_agent.py         常驻代理
├── start_monitor.py       探活监控
├── start_daemon.py        容器入口（三合一）
├── scripts/               Windows 计划任务与开机自启
├── tests/                 单元测试
├── docs/architecture.md   原理、数据流与实战教训
├── Dockerfile / docker-compose.yml
└── .env.example
```

## 测试

```bash
python -m unittest discover -s tests -v
```

## 文档

- [docs/architecture.md](docs/architecture.md)：两套会话的设计取舍、数据流、为什么浏览器位置很关键、实战教训

## 免责声明

本工具使用微信读书（`weread.qq.com`）的非公开内部接口做登录态保活，该接口未经腾讯官方文档化或授权。请仅用于个人订阅场景，控制访问频率，风险自负。

## 许可证

[MIT](LICENSE) © 2026 weread-keepalive contributors
