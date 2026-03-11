# 代理模块使用说明

## 模块功能

独立的 kookeey SOCKS5 代理池管理模块，提供：
- **代理池管理**：从 API 或本地文件加载代理 → 并发测试 → 自动轮换分配
- **本地中继**：解决 Chromium 不支持 SOCKS5 认证代理的问题

## 快速开始

### 1. 环境准备

```bash
pip install requests PySocks
```

### 2. 配置 .env

```env
PROXY_ENABLED=true
PROXY_API_URL=                         # kookeey API（留空从本地文件加载）
LOCAL_PROXY=http://127.0.0.1:7897      # 本地 tun 代理
PROXY_POOL_FILE=kookeey_pool.txt       # 代理列表文件
PROXY_MAX_USES=4                       # 每个代理最多用几次
```

### 3. 准备代理列表

`kookeey_pool.txt` 格式（每行一条）：
```
gate.kookeey.info:1000:用户名:密码
gate.kookeey.info:1000:用户名:密码-US-123456-5m
```

### 4. 代码调用

```python
from proxy import get_proxy_pool, LocalSocksRelay

# ---- 初始化代理池 ----
pool = get_proxy_pool(task_count=10)  # 按任务量初始化，优化测试范围
if pool is None:
    print("代理未启用")
    exit()

print(f"可用代理: {pool.available_count}")

# ---- 获取代理 ----
proxy = pool.acquire()
if proxy is None:
    print("无可用代理")
    exit()

# ---- 方式1: 直接用 SOCKS5（适合 requests/curl_cffi）----
import requests
resp = requests.get("https://httpbin.org/ip", proxies=proxy.requests_proxies, timeout=10)

# ---- 方式2: 通过本地中继（适合 Chromium/Playwright）----
relay = LocalSocksRelay(proxy.host, proxy.port, proxy.username, proxy.password)
relay.start()
# browser.new_context(proxy=relay.playwright_proxy)
# 使用完毕后:
relay.stop()

# ---- 报告结果（影响代理轮换策略）----
pool.report_success(proxy)   # 成功：重置失败计数
pool.report_failure(proxy)   # 失败：连续失败2次后标记不可用

# ---- 查看统计 ----
stats = pool.get_stats()
print(f"总计={stats['total']}, 可用={stats['available']}, 已耗尽={stats['exhausted']}")
```

## 代理轮换机制

- 每个代理最多使用 `PROXY_MAX_USES` 次（默认4次，对应注册3-5个账号）
- 使用次数达到上限后，`acquire()` 自动返回下一个可用代理
- 连续失败 2 次的代理自动标记为不可用
- 所有代理用完时 `acquire()` 返回 `None`

## 代理数量验证

注册前应验证代理数量是否充足：

```python
pool = get_proxy_pool(task_count=注册数量)
max_per_proxy = 4  # PROXY_MAX_USES
needed_proxies = -(-注册数量 // max_per_proxy)  # 向上取整
if pool.available_count < needed_proxies:
    print(f"代理不足: 需要{needed_proxies}个，实际{pool.available_count}个")
```

## API 参考

### ProxyEntry（代理条目）

| 属性 | 类型 | 说明 |
|------|------|------|
| `host` | str | 代理地址 |
| `port` | int | 代理端口 |
| `username` | str | 认证用户名 |
| `password` | str | 认证密码 |
| `socks5_url` | str | 完整 SOCKS5 URL |
| `requests_proxies` | dict | requests 库 proxies 参数 |
| `playwright_proxy` | dict | Playwright 代理配置 |

### ProxyPool（代理池）

| 方法 | 返回 | 说明 |
|------|------|------|
| `initialize(task_count)` | None | 加载+测试代理 |
| `acquire()` | ProxyEntry/None | 获取下一个可用代理 |
| `report_success(proxy)` | None | 报告成功 |
| `report_failure(proxy)` | None | 报告失败 |
| `get_stats()` | dict | 统计信息 |
| `available_count` | int | 可用代理数 |
| `total_count` | int | 代理总数 |

### LocalSocksRelay（本地中继）

| 方法 | 说明 |
|------|------|
| `start()` | 启动中继（自动绑定随机端口） |
| `stop()` | 停止中继 |
| `port` | 本地端口号 |
| `playwright_proxy` | Playwright 代理配置 |
| `local_socks5_url` | 本地 SOCKS5 URL |

## 移植到其他项目

1. 复制 `proxy/` 目录 + `utils.py` + `config.py` + `.env`
2. 在 `.env` 中配置代理参数
3. 按上述示例代码调用即可
4. 代理模块无其他外部依赖（仅需 `requests` 和 `PySocks`）
