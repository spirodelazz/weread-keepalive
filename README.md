<div align="center">

# 📚 weread-keepalive

**微信读书登录态自动续期 · 为 we-mp-rss 采集模式护航**

[![License: MIT](https://img.shields.io/badge/License-MIT-8B5CF6?style=flat&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat&logo=python&logoColor=white)]()
[![Playwright](https://img.shields.io/badge/Playwright-Headless%20Keepalive-2EAD33?style=flat&logo=playwright&logoColor=white)]()
[![we-mp-rss](https://img.shields.io/badge/Serves-we--mp--rss-F59E0B?style=flat)]()

</div>

专为 [we-mp-rss](https://github.com/rachelos/we-mp-rss) 的 `weread_mp`（微信读书）采集模式服务：自动续期登录态。方案经过长期生产环境实战验证，工程细节中沉淀了不少踩坑教训（见下文）。

## 解决什么问题

we-mp-rss 用微信读书接口抓取公众号文章时依赖 `wx.lic` 里的登录 Cookie，但：

- **`wr_skey` 约 90 分钟就过期**，过期后采集全挂（issues #467、#465 等反馈的"无法同步"多数源于此）；
- we-mp-rss 的 `login_status` 只是**本地缓存时间戳，从不向微信读书真实校验**（issue #419）——服务端提前吊销会话（风控、异地登录）时状态仍是"已登录"，直到某轮采集撞上错误码才被动发现；
- 官方给出的解法是"过期发通知 + 用户扫短命二维码"（issue #417），扫完只能撑 90 分钟，陷入「一直要扫码」的跑步机。

weread-keepalive 的思路：**用本机浏览器的持久化 profile 持有真实的网页登录会话（长命），定时用 Playwright 无头访问微信读书页面保活，收割并验证 Cookie 后原子写回 `wx.lic`**。只要 profile 会话活着，`wx.lic` 里的 Cookie 就始终是新鲜的，we-mp-rss 无感续期。

## 两套会话，别搞混（核心设计）

| | profile 网页会话 | API 扫码会话 |
|---|---|---|
| 载体 | 本机 Chrome/Chromium 持久化 profile | we-mp-rss 扫码接口返回的 Cookie |
| 寿命 | 长（服务端长期有效，定期访问即保活） | `wr_skey` 约 90 分钟 |
| 角色 | **保活根基**，采集链路真正依赖的登录态 | 只能临时救急 |
| 失效后 | 无头刷新连续失败 → 人工扫码重建（桌面机回本机弹窗扫码；NAS / 无桌面服务器走飞书远程扫码，见 Docker 部署） | 扫码即恢复，但很快又过期 |

因此本工具**绝不**把"发二维码"当保活手段；profile 彻底失效时需要人工扫码重建一次（此后又是长期自动续期）：桌面机回本机跑 `python refresh_cookie.py`，NAS / 无桌面服务器由飞书推送二维码远程扫码（见 Docker 部署）。

## 架构

```
Windows / macOS 宿主机
┌────────────────────────────────────────────────────────┐
│  方式 A：计划任务（每 15 分钟）                            │
│    python refresh_cookie.py --headless --cooldown-hours 0│
│  方式 B：常驻代理（we-mp-rss 容器可远程触发）               │
│    python start_agent.py  →  127.0.0.1:9876              │
│      GET  /health   存活探针                              │
│      POST /refresh   verify-first 刷新（token 可选）      │
│      POST /login     触发扫码推送会话 / GET /login 查状态  │
│  方式 C：探活守护（可选，主动校验 + 告警）                   │
│    python start_monitor.py                               │
│         │                                                │
│         ▼                                                │
│  Playwright 持久化 profile（长命网页会话 = 保活根基）        │
│         │ 收割 Cookie → 真实接口验证                       │
│         ▼                                                │
│  wx.lic（原子写回，与 we-mp-rss 数据卷同一物理文件）          │
└─────────────────────┬──────────────────────────────────┘
                      ▼
        we-mp-rss 容器（weread_mp 模式读 wx.lic，无需任何改动）
```

- **为什么浏览器操作必须在宿主机**：桌面方案下，容器内是 Linux Chromium，解不开宿主操作系统凭据（Windows DPAPI / macOS 钥匙串）加密的 profile cookie，容器内刷新必然失败。容器侧只应通过 agent 的 `/refresh` 端点请宿主机代劳。**如果整台机器都没有桌面（NAS/服务器），请直接用下文的 Docker 模式**——浏览器与 profile 都在容器内自建自用，不存在跨系统解密问题；
- **容器怎么触发**：Docker Desktop（Windows/macOS）下容器内 `host.docker.internal` 会转发到宿主机回环，所以代理默认绑 `127.0.0.1` 就能被容器访问——**不要绑 0.0.0.0**；绑非回环地址必须设 `WEREAD_AGENT_TOKEN`，否则代理拒绝启动。

## 快速开始

### 1. 安装

```bash
cd weread-keepalive
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，**必配** `WEREAD_LIC_PATH` 指向 we-mp-rss 数据卷的 `wx.lic`（与容器 `/app/data` 挂载同一物理目录）。建议同时配置：

- `WEREAD_VERIFY_BOOK_IDS`：你在微信读书里关注的公众号 bookId（逗号分隔），兜底校验用。**不要用别人的 bookId**——校验会恒返 -2041，把有效登录态误判成失效；
- `WEREAD_BROWSER_PATH`：本机 Chrome 路径（留空用 Playwright 自带 Chromium 也可以）。

### 3. 首次扫码建立登录态

```bash
python refresh_cookie.py          # 弹出可见浏览器窗口，微信扫码，等待自动保存
python refresh_cookie.py --status # 查看登录态验证结果
```

### 4. 启用定时续期（Windows）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

每 15 分钟无头刷新一次（`--cooldown-hours 0` 强制每次真实访问页面，保证 90 分钟会话窗口内始终有访问）。

### 5.（可选）容器场景装常驻代理

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1   # 开机自启
wscript.exe "$([Environment]::GetFolderPath('Startup'))\weread-keepalive-agent.vbs"  # 立即启动
```

we-mp-rss 侧无需改动。如需让容器主动触发刷新，可在容器内调用：

```bash
curl -X POST http://host.docker.internal:9876/refresh   # 配了 token 则加 -H "X-Agent-Token: ..."
```

### 6.（可选）探活守护 + 告警

```bash
python start_monitor.py          # 常驻：每 15 分钟向微信读书真实接口探活
python start_monitor.py --once   # 诊断模式：只探活一轮
```

失败时先自动刷新自愈（本机无头，或经 `WEREAD_REFRESH_AGENT_URL` 请宿主机代理代劳）；连续失败达到阈值发告警（飞书/企业微信/通用 webhook，见 `.env.example`），告警默认 6 小时冷却防刷屏。

## Docker 部署（NAS / 无桌面服务器）

we-mp-rss 的主流部署形态是 NAS / 无桌面 Linux 服务器——这类机器弹不了扫码窗口，上面的桌面方案跑不了。项目提供**容器模式**：浏览器与 profile 都在容器内（Linux 自建自用，不存在跨系统 cookie 解密问题），登录态重建靠**飞书推送登录二维码**远程扫码完成，全程不需要在设备前。

> **场景选择**：Windows/macOS 桌面机 → 用上面的宿主机方案（弹窗扫码体验最好）；NAS / 无桌面服务器 → 用本节容器方案。
> **两种模式别共用同一份 profile**：宿主机 Chrome 建的 profile 容器解不开（DPAPI/钥匙串），反之亦然。各自用各自的 data 目录。

### 1. 准备文件

```bash
cd weread-keepalive
cp .env.example .env
```

`.env`（与 docker-compose.yml 同目录，compose 会读取它做变量替换）至少包含：

```bash
# 代理鉴权：容器互联需绑 0.0.0.0，无鉴权暴露会被拒绝启动（硬性安全要求）
WEREAD_AGENT_TOKEN=换成你自己的随机长串
# ---- 飞书扫码推送 ----
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_QR_RECEIVE_ID=oc_xxx      # python qr_login.py --list-chats 查询
# 可选：群机器人 webhook 作为第二投递出口 + 签名
#FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
#FEISHU_SECRET=
```

### 2. 启动

```bash
docker compose up -d --build
```

容器内跑的是三合一守护（`start_daemon.py`）：每 15 分钟无头刷新保活 + 探活监控告警 + 刷新代理（`/health` `/refresh` `/login`）。`we-mp-rss` 服务已在 compose 里一并编排（已有部署的可删掉该服务，把两边 data 卷指到同一目录）。

### 3. 首次扫码（远程完成）

触发方式任选其一：

- **自动**：compose 已设 `WEREAD_QR_PUSH_ON_EXPIRE=1`，无头刷新失败（登录态过期）时自动推送；
- **手动**：`docker compose exec weread-keepalive python qr_login.py`；
- **HTTP**：`curl -X POST http://127.0.0.1:9876/login -H "X-Agent-Token: 你的token"`。

飞书收到二维码卡片后：**手机单设备**——长按图片保存到相册，微信「扫一扫 → 相册」选取识别；**双设备**——电脑端飞书看码，手机直接扫。二维码会过期自动刷新，**以最新一张为准**；扫码成功/超时都会补一张结果卡。会话最长等 10 分钟，期间每 30 秒最多重推一张、整场最多 12 张（渲染抖动不会刷屏）。

### 4. we-mp-rss 侧触发刷新（可选）

```bash
# compose 内网直连（无需 host.docker.internal）
docker compose exec we-mp-rss curl -s -X POST http://weread-keepalive:9876/refresh -H "X-Agent-Token: 你的token"
```

### 5. 飞书应用怎么建（一次性）

1. [飞书开放平台](https://open.feishu.cn/) 创建**自建应用**，开启「机器人」能力；
2. 开通权限：`im:resource`（上传图片）、`im:message`（以应用身份发消息）、`im:chat:readonly`（查群列表）；发布版本使权限生效；
3. 把机器人加入接收二维码的群，`python qr_login.py --list-chats` 查 `chat_id` 填入 `FEISHU_QR_RECEIVE_ID`。

> 为什么必须建应用：飞书**群机器人 webhook 发不了图片**——image 消息和卡片 img 元素都要 `image_key`，而它只能经应用上传获得。webhook 可作为第二投递出口（配 `FEISHU_WEBHOOK`，开了签名校验再加 `FEISHU_SECRET`）。

### 6. 桌面宿主机也能受益

宿主机方案（`python refresh_cookie.py` 弹窗扫码）默认同时开启推送（`WEREAD_QR_PUSH_ENABLED=1`）：窗口弹出的同时二维码也推到飞书，**人不在电脑前用手机扫飞书里的码即可远程重建**；扫码成功/超时都有结果卡通知。不想推送就设 `WEREAD_QR_PUSH_ENABLED=0`。

## 配置项总览

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WEREAD_LIC_PATH` | `./data/wx.lic` | **必配**。we-mp-rss 数据卷的 wx.lic |
| `WEREAD_PROFILE_DIR` | `./data/weread-chrome-profile` | 持久化 profile 目录 |
| `WEREAD_REFRESH_URL` | 读 wx.lic 的 `cookie_refresh_url` | 刷新页面 URL（reader 页） |
| `WEREAD_BROWSER_PATH` | Playwright Chromium | 本机 Chrome 路径 |
| `WEREAD_VERIFY_BOOK_IDS` | 空 | 兜底校验 bookId（逗号分隔） |
| `WEREAD_FEEDS_FILE` | 空 | 可选的外部订阅文件（提取 wechat bookId） |
| `WEREAD_SEED_COOKIE_ENABLED` | false | 种子注入，**保持关闭**（见下） |
| `WEREAD_COOLDOWN_HOURS` | 6 | 计划任务冷却；agent/monitor 触发时强制 0 |
| `WEREAD_AGENT_HOST/PORT` | 127.0.0.1:9876 | 代理监听 |
| `WEREAD_AGENT_TOKEN` | 空 | /refresh 鉴权 token |
| `WEREAD_AGENT_ALLOW_CIDRS` | 空 | 来源 IP/CIDR 白名单 |
| `WEREAD_MONITOR_INTERVAL_SECONDS` | 900 | 探活间隔 |
| `WEREAD_NOTIFY_FEISHU_WEBHOOK` | 空 | 飞书群机器人 |
| `WEREAD_NOTIFY_WECOM_WEBHOOK` | 空 | 企业微信群机器人 |
| `WEREAD_NOTIFY_WEBHOOK` | 空 | 通用 webhook（POST `{"title","text"}`） |
| `WEREAD_QR_PUSH_ENABLED` | true | 扫码会话内把登录二维码实时推飞书 |
| `WEREAD_QR_PUSH_ON_EXPIRE` | false | 刷新失败需扫码时自动推送（容器场景开启） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 空 | 飞书自建应用（上传二维码图片必需） |
| `FEISHU_QR_RECEIVE_ID` | 空 | 应用消息投递目标（chat_id，`qr_login.py --list-chats` 查询） |
| `FEISHU_QR_RECEIVE_ID_TYPE` | `chat_id` | 投递目标类型（`chat_id` / `open_id` / `user_id`） |
| `FEISHU_QR_SEND_MODE` | 空（自适应） | 卡片投递出口：`app`=应用消息，`webhook`=群机器人，空=配了 receive_id 走 app |
| `FEISHU_WEBHOOK` / `FEISHU_SECRET` | 空 | 群机器人 webhook 投递出口 + 签名密钥 |
| `WEREAD_QR_SESSION_COOLDOWN_SECONDS` | 1800 | 两次扫码推送会话的最小间隔 |
| `WEREAD_DAEMON_REFRESH_SECONDS` | 900 | 容器守护的定时刷新间隔 |

## 与 we-mp-rss 的对接要点

1. we-mp-rss 需处于微信读书采集模式：`WE_MP_RSS_GATHER_MODEL=weread_mp`，`WE_MP_RSS_GATHER_CONTENT=True`；
2. **`WEREAD_COOKIE` 环境变量必须留空**——否则环境变量会覆盖扫码后写入的 `wx.lic`；
3. 本工具只写 `wx.lic` 的 `weread_data`（cookie/vid/name/时间戳），文档其余部分原样保留；原子写入（唯一临时文件 + `os.replace` + 跨进程文件锁），容器读到的永远是一份完整 YAML；
4. we-mp-rss 的过期通知（`WECHAT_WEBHOOK` 等）可以照常留着当兜底，但配置好本工具后几乎不应再触发。

## 数据与日志

- `data/weread_host_refresh_state.json`：刷新结果契约（`last_ok` / `consecutive_failures` / `source` / `message`），监控据此区分「API 会话过期」与「profile 已死需人工重建」；
- `data/logs/`：代理与刷新日志；
- `data/weread-chrome-profile/`：登录态 profile，**请勿清理**，丢了就要重新扫码。

## 沉淀在本项目的实战教训

- **验证必须真实**：只查 Cookie 里有没有 `wr_vid` 不够（过期的也带），必须真请求接口；主判据 `/web/shelf/sync` 的 `userVid` 必须传**空字符串**（非空反而 -2012）；
- **兜底校验必须用自己的 bookId**（别人的恒 -2041 → 假阴性误判）；
- **冷却跳过不算 profile 存活证据**：没碰过浏览器的调用（冷却跳过/未配置 URL）不写入刷新状态，否则会用"API Cookie 还活着"掩盖已死的 profile；
- **原子写 + 双锁**：唯一临时文件名防并发覆写；wx.lic 读-改-写持独占锁；刷新本身持非阻塞锁防双浏览器；
- **`/web/login/renewal` 续期接口受风控**（同一 Cookie 时而成功、之后持续 -2013），只作自愈尝试，不作主链路；
- **扫码窗口单例**：并发触发不去重会连环弹窗。

## 免责声明

本工具使用微信读书（weread.qq.com）的非公开内部接口做登录态保活，该接口未经腾讯官方文档化或授权。请仅用于个人订阅场景，控制访问频率，风险自负。

## License

[MIT](LICENSE)。
