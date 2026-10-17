# 架构与设计

本文说明 weread-keepalive 的设计取舍与实现约束。日常使用看 [README](../README.md) 即可。

## 要解决的问题

we-mp-rss 用微信读书接口抓取公众号文章时依赖 `wx.lic` 中的登录 Cookie，这条链路有三个结构性缺陷：

- **`wr_skey` 约 90 分钟过期**，过期后采集全部失败；
- we-mp-rss 的 `login_status` 只是**本地缓存时间戳，从不向微信读书真实校验**——服务端提前吊销会话（风控、异地登录）时状态仍显示「已登录」，直到某轮采集撞上错误码才被动发现；
- 官方方案是「过期发通知 + 用户扫短命二维码」，扫完只能撑 90 分钟。

本项目的思路：**用本机浏览器的持久化 profile 持有真实的网页登录会话（长命），定时无头访问保活，收割并验证 Cookie 后原子写回 `wx.lic`**。只要 profile 会话活着，`wx.lic` 里的 Cookie 就始终新鲜。

## 两套会话

必须区分清楚，否则会把「扫码」误当成保活手段：

| | profile 网页会话 | API 扫码会话 |
| --- | --- | --- |
| 载体 | 本机 Chrome/Chromium 持久化 profile | we-mp-rss 扫码接口返回的 Cookie |
| 寿命 | 长（服务端长期有效，定期访问即保活） | `wr_skey` 约 90 分钟 |
| 角色 | **保活根基**，采集链路真正依赖的登录态 | 临时救急 |
| 失效后 | 无头刷新连续失败 → 人工扫码重建 | 扫码即恢复，但很快再次过期 |

profile 彻底失效时需要人工扫码重建一次，此后又是长期自动续期。桌面机回本机跑 `python refresh_cookie.py` 弹窗扫码；NAS / 无桌面服务器由飞书推送二维码远程扫码。

## 数据流

```
Windows / macOS 宿主机（或 Docker 容器）
┌────────────────────────────────────────────────────────┐
│  方式 A：计划任务（每 15 分钟）                            │
│    python refresh_cookie.py --headless --cooldown-hours 0│
│  方式 B：常驻代理（we-mp-rss 容器可远程触发）               │
│    python start_agent.py  →  127.0.0.1:9876              │
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

三种方式可以叠加：桌面机通常用 A + B，容器内由 `start_daemon.py` 同时提供 B 与定时的 A 等价逻辑；C 用于主动发现「状态显示已登录但实际已吊销」的情况。

## 为什么浏览器位置很关键

- **桌面方案必须在宿主机跑浏览器**：容器内是 Linux Chromium，解不开宿主操作系统凭据（Windows DPAPI / macOS 钥匙串）加密的 profile cookie，容器内刷新必然失败。容器侧只应通过 agent 的 `/refresh` 端点请宿主机代劳。
- **整机没有桌面时用容器模式**：浏览器与 profile 都在容器内自建自用，不存在跨系统解密问题，登录态重建靠飞书推送二维码。
- **两种模式不能共用同一份 profile**：宿主机 Chrome 创建的 profile 容器解不开，反之亦然，各自使用各自的 data 目录。
- **代理绑定地址**：Docker Desktop 下 `host.docker.internal` 会转发到宿主机回环，因此默认绑 `127.0.0.1` 即可被容器访问——不要绑 `0.0.0.0`；绑非回环地址必须设置 `WEREAD_AGENT_TOKEN`，否则拒绝启动。

## 写回与一致性

- 只改 `wx.lic` 的 `weread_data`（cookie / vid / name / 时间戳），文档其余部分原样保留。
- 原子写入：唯一临时文件名 + `os.replace`，容器任何时刻读到的都是一份完整 YAML。
- 双锁：`wx.lic` 读-改-写持跨进程独占锁；刷新本身持非阻塞锁，防止两个浏览器实例同时操作同一 profile。
- 扫码窗口单例：并发触发会去重，避免连环弹窗。
- 状态契约：`data/weread_host_refresh_state.json` 记录 `last_ok` / `consecutive_failures` / `source` / `message`，监控据此区分「API 会话过期」与「profile 已死需人工重建」。

## 飞书应用（远程扫码）

1. 在[飞书开放平台](https://open.feishu.cn/)创建**自建应用**并开启「机器人」能力；
2. 开通权限：`im:resource`（上传图片）、`im:message`（以应用身份发消息）、`im:chat:readonly`（查群列表），发布版本使权限生效；
3. 把机器人加入接收二维码的群，`python qr_login.py --list-chats` 查 `chat_id` 填入 `FEISHU_QR_RECEIVE_ID`。

## 实战教训

这些结论来自长期生产环境运行，改动前建议先读：

- **验证必须真实**：只查 Cookie 里有没有 `wr_vid` 不够（过期 Cookie 也带），必须真请求接口；主判据 `/web/shelf/sync` 的 `userVid` 必须传**空字符串**（非空反而报 `-2012`）。
- **兜底校验必须用自己的 bookId**：用别人的恒返 `-2041`，把有效登录态误判成失效。
- **冷却跳过不算 profile 存活证据**：没碰过浏览器的调用（冷却跳过 / 未配置 URL）不写入刷新状态，否则会用「API Cookie 还活着」掩盖已死的 profile。
- **`/web/login/renewal` 续期接口受风控**（同一 Cookie 时而成功、之后持续 `-2013`），只作自愈尝试，不作主链路。
- **种子注入默认关闭**：注入的 Cookie 会掩盖 profile 已死的事实，`WEREAD_SEED_COOKIE_ENABLED` 保持 `false`。
- **扫码推送限流**：会话最长等 10 分钟，期间每 30 秒最多重推一张、整场最多 12 张，避免渲染抖动刷屏。
- **飞书群机器人发不了图片**：image 消息与卡片 `img` 都需要 `image_key`，只能经自建应用上传获得；webhook 仅作为第二投递出口。
