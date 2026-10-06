"""weread-keepalive：微信读书登录态自动续期，专为 we-mp-rss 的 weread_mp 模式服务。

模块结构：
  - config.py    集中管理全部环境变量配置
  - wxlic.py     wx.lic（YAML）读写：原子写、跨进程锁、Cookie 去重
  - verify.py    Cookie 真实接口验证（/web/shelf/sync 主判据 + /web/mp/articles 兜底）
  - refresher.py 刷新核心：Playwright 无头保活 + 扫码重建
  - state.py     刷新健康状态文件契约（宿主机写入，监控只读）
  - agent.py     宿主机常驻 HTTP 代理（/health、/refresh，供容器远程触发）
  - monitor.py   主动探活守护：定期真实校验 + 自愈 + 告警
"""

__version__ = "0.1.0"
