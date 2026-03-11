"""
独立代理模块 - kookeey SOCKS5 代理池管理

★★★ 完全自包含 ★★★
复制本目录(proxy/) + .env 文件到任何项目即可直接使用。
无需额外的 utils.py、config.py 等外部依赖。

★ 依赖安装：
  pip install requests PySocks

★ 最小 .env 配置（放在 proxy/ 的上级目录）：
  PROXY_ENABLED=true
  PROXY_API_URL=https://www.kookeey.net/pickdynamicips?...
  LOCAL_PROXY=http://127.0.0.1:7897
  PROXY_POOL_FILE=kookeey-1000.txt
  PROXY_MAX_USES=4

★ 快速使用（复制即用）：

  # 方式1: 单例模式（推荐，自动从 .env 加载配置）
  from proxy import get_proxy_pool

  pool = get_proxy_pool(task_count=10)
  proxy = pool.acquire()
  print(f"使用代理: {proxy.display_addr}")
  # requests.get(url, proxies=proxy.requests_proxies)
  pool.report_success(proxy)

  # 方式2: 手动创建（不依赖 .env，适合程序化配置）
  from proxy import ProxyPool, ProxyConfig

  config = ProxyConfig(
      api_url="https://www.kookeey.net/...",
      local_proxy="http://127.0.0.1:7897",
      pool_file="kookeey-1000.txt",
      max_uses=4,
  )
  pool = ProxyPool(config)
  pool.initialize(task_count=10)
  proxy = pool.acquire()

  # 方式3: 浏览器代理（解决 Chromium 不支持认证 SOCKS5 的问题）
  from proxy import get_proxy_pool, LocalSocksRelay

  pool = get_proxy_pool(task_count=5)
  proxy = pool.acquire()
  relay = LocalSocksRelay(proxy.host, proxy.port, proxy.username, proxy.password)
  relay.start()
  # browser.new_context(proxy=relay.playwright_proxy)  # 无认证的本地端口
  relay.stop()

详细文档见 proxy/README.md
"""
from proxy.pool import ProxyPool, ProxyEntry, ProxyConfig, get_proxy_pool, parse_proxy_line
from proxy.relay import LocalSocksRelay

__all__ = [
    # 核心类
    "ProxyPool",       # 代理池管理器
    "ProxyEntry",      # 单个代理条目
    "ProxyConfig",     # 代理配置
    "LocalSocksRelay", # 本地 SOCKS5 中继（浏览器用）
    # 入口函数
    "get_proxy_pool",  # 获取全局代理池单例（推荐入口）
    "parse_proxy_line", # 解析单行代理文本
]
