"""
代理池管理器 - 获取、测试、分配、轮换 kookeey SOCKS5 代理

★★★ 独立模块 ★★★
本文件是完全自包含的代理池管理器，无外部依赖（仅需 requests + PySocks）。
复制 proxy/ 文件夹 + .env 到任何项目即可直接使用。

★ 核心概念：
  - ProxyEntry: 单个代理条目（地址、端口、认证信息、使用计数）
  - ProxyPool: 线程安全的代理池管理器（初始化→测试→分配→轮换）
  - ProxyConfig: 代理配置（从 .env 自动读取或手动传入）

★ 代理生命周期：
  1. 每次启动时，通过本地 tun 代理(127.0.0.1:7897)请求 kookeey API 刷新代理列表
  2. API 返回的代理列表保存到本地文件(kookeey-1000.txt)作为备用缓存
  3. 并发测试代理可用性（按需测试，节省时间）
  4. 通过 pool.acquire() 获取可用代理（自动轮换）
  5. 每个代理使用 max_uses 次后自动切换到下一个
  6. 通过 report_success/report_failure 反馈使用结果

★ 快速上手（3行代码）：
  from proxy import get_proxy_pool

  pool = get_proxy_pool(task_count=10)  # 按任务量初始化
  proxy = pool.acquire()                 # 获取一个代理
  # 使用 proxy.socks5_url 发起请求
  pool.report_success(proxy)             # 成功反馈

★ 对接 requests/curl_cffi：
  proxy = pool.acquire()
  response = requests.get(url, proxies=proxy.requests_proxies)

★ 对接 Playwright（需搭配 LocalSocksRelay）：
  from proxy import get_proxy_pool, LocalSocksRelay

  proxy = pool.acquire()
  relay = LocalSocksRelay(proxy.host, proxy.port, proxy.username, proxy.password)
  relay.start()
  browser.new_context(proxy=relay.playwright_proxy)  # 无认证的本地端口
  relay.stop()

★ 配置项（.env 文件，放在 proxy/ 的上级目录）：
  PROXY_ENABLED=true                           # 是否启用代理池
  PROXY_API_URL=https://www.kookeey.net/...    # kookeey API 地址（必填，每次启动自动刷新）
  LOCAL_PROXY=http://127.0.0.1:7897            # 本地 tun 代理（请求 API 用）
  PROXY_POOL_FILE=kookeey-1000.txt             # 本地代理列表文件（API 获取后自动保存）
  PROXY_MAX_USES=4                             # 每个代理最多使用次数
  PROXY_TEST_CONCURRENCY=30                    # 代理测试并发数
  PROXY_TEST_TIMEOUT=10                        # 代理测试超时秒数

★ 依赖：
  pip install requests PySocks
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

# ============================================================
# 常量
# ============================================================

# 代理连续失败此次数后标记为不可用
PROXY_FAIL_THRESHOLD = 2
# 代理测试目标 URL（验证代理能访问目标站点）
PROXY_TEST_URL = "https://chatgpt.com"
# API 请求超时（秒）
API_FETCH_TIMEOUT = 30
# 模块目录 → 上级目录（项目根目录，.env 和代理列表文件存放处）
_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _MODULE_DIR.parent


# ============================================================
# 内置工具（无外部依赖）
# ============================================================

def _build_logger(name: str = "proxy") -> logging.Logger:
    """创建标准化日志记录器"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def _parse_env_file(env_path: Path) -> dict:
    """
    解析 .env 文件，返回键值对字典
    支持 KEY=VALUE 格式，忽略注释行和空行
    """
    env_map = {}
    if not env_path.exists():
        return env_map
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                env_map[key] = value
    except Exception:
        pass
    return env_map


def _env_val(env_map: dict, key: str, default: str = "") -> str:
    """获取环境变量值，优先级：系统环境变量 > .env 文件 > 默认值"""
    return os.environ.get(key, env_map.get(key, default))


# ============================================================
# 代理配置
# ============================================================

@dataclass
class ProxyConfig:
    """
    代理池配置

    ★ 两种创建方式：
      1. 自动从 .env 加载: config = ProxyConfig.from_env()
      2. 手动指定:          config = ProxyConfig(api_url="...", max_uses=5)
    """
    enabled: bool = True
    api_url: str = ""
    local_proxy: str = "http://127.0.0.1:7897"
    pool_file: str = "kookeey-1000.txt"
    max_uses: int = 4
    test_concurrency: int = 30
    test_timeout: int = 10

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> ProxyConfig:
        """
        从 .env 文件加载代理配置

        参数:
          env_path: .env 文件路径（默认为 proxy/ 上级目录的 .env）

        ★ 对接说明：
          将 .env 文件放在 proxy/ 文件夹的上级目录（项目根目录）
          配置项见模块顶部文档字符串
        """
        if env_path is None:
            env_path = _PROJECT_DIR / ".env"
        env = _parse_env_file(env_path)
        enabled_str = _env_val(env, "PROXY_ENABLED", "true")
        return cls(
            enabled=enabled_str.lower() in ("true", "1", "yes"),
            api_url=_env_val(env, "PROXY_API_URL", ""),
            local_proxy=_env_val(env, "LOCAL_PROXY", "http://127.0.0.1:7897"),
            pool_file=_env_val(env, "PROXY_POOL_FILE", "kookeey-1000.txt"),
            max_uses=int(_env_val(env, "PROXY_MAX_USES", "4")),
            test_concurrency=int(_env_val(env, "PROXY_TEST_CONCURRENCY", "30")),
            test_timeout=int(_env_val(env, "PROXY_TEST_TIMEOUT", "10")),
        )


# ============================================================
# 代理条目
# ============================================================

@dataclass
class ProxyEntry:
    """
    单个代理条目

    ★ 属性说明：
      host/port: 代理服务器地址和端口
      username/password: SOCKS5 认证信息
      use_count: 已使用次数
      fail_count: 连续失败次数
      is_available: 是否可用

    ★ 常用属性（直接传给各种 HTTP 客户端）：
      proxy.socks5_url       → "socks5h://user:pass@host:port"
                                用于 requests / curl_cffi 的代理参数

      proxy.requests_proxies → {"http": "socks5h://...", "https": "socks5h://..."}
                                直接传给 requests.get(proxies=proxy.requests_proxies)

      proxy.playwright_proxy → {"server": "socks5://host:port", "username": "...", "password": "..."}
                                用于 Playwright browser.new_context(proxy=proxy.playwright_proxy)
                                注意: Chromium 不支持带认证的 SOCKS5，需搭配 LocalSocksRelay 使用

      proxy.display_addr     → "host:port"（日志显示用，不含认证信息）
    """
    host: str
    port: int
    username: str
    password: str
    use_count: int = 0
    fail_count: int = 0
    is_available: bool = True
    last_used_at: float = 0.0

    @property
    def socks5_url(self) -> str:
        """SOCKS5 代理 URL（socks5h 表示由代理端解析 DNS）"""
        if self.username and self.password:
            return f"socks5h://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"socks5h://{self.host}:{self.port}"

    @property
    def requests_proxies(self) -> dict:
        """直接用于 requests 库的 proxies 参数"""
        url = self.socks5_url
        return {"http": url, "https": url}

    @property
    def playwright_proxy(self) -> dict:
        """用于 Playwright browser.new_context() 的代理配置"""
        proxy = {"server": f"socks5://{self.host}:{self.port}"}
        if self.username:
            proxy["username"] = self.username
        if self.password:
            proxy["password"] = self.password
        return proxy

    @property
    def display_addr(self) -> str:
        """显示用地址（不含认证信息）"""
        return f"{self.host}:{self.port}"


def parse_proxy_line(line: str) -> Optional[ProxyEntry]:
    """
    解析单行代理文本

    支持格式:
      host:port:username:password  (kookeey 标准格式，如 gate.kookeey.info:1000:user:pass)
      host:port                     (无认证代理)
    """
    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.split(":")
    if len(parts) >= 4:
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            return None
        username = parts[2]
        # 密码中可能含冒号，取后面所有部分
        password = ":".join(parts[3:])
        return ProxyEntry(host=host, port=port, username=username, password=password)
    if len(parts) == 2:
        try:
            port = int(parts[1])
        except ValueError:
            return None
        return ProxyEntry(host=parts[0], port=port, username="", password="")
    return None


# ============================================================
# 代理池管理器
# ============================================================

class ProxyPool:
    """
    线程安全的代理池管理器

    ★ 使用流程：
      1. pool = ProxyPool(config)
      2. pool.initialize(task_count=10)  # 加载 + 测试
      3. proxy = pool.acquire()           # 获取代理
      4. pool.report_success(proxy)       # 或 report_failure(proxy)
      5. 重复 3-4 直到任务完成

    ★ 代理轮换逻辑：
      - 每个代理最多使用 max_uses 次（默认4次，对应3-5个账号）
      - 达到上限后 acquire() 自动返回下一个可用代理
      - 连续失败 PROXY_FAIL_THRESHOLD 次后标记不可用

    ★ 也可以直接创建（不走单例）：
      config = ProxyConfig(api_url="...", max_uses=5)
      pool = ProxyPool(config)
      pool.initialize(task_count=20)
    """

    def __init__(self, config: ProxyConfig, logger=None) -> None:
        self._config = config
        self._logger = logger or _build_logger("proxy")
        self._lock = threading.Lock()
        self._proxies: list[ProxyEntry] = []
        self._index = 0
        self._tested_count = 0

    @property
    def total_count(self) -> int:
        """代理总数"""
        return len(self._proxies)

    @property
    def available_count(self) -> int:
        """当前可用代理数"""
        with self._lock:
            return sum(
                1 for p in self._proxies
                if p.is_available and p.use_count < self._config.max_uses
            )

    def initialize(self, task_count: int = 0) -> None:
        """
        初始化代理池：获取 → 解析 → 保存 → 测试

        参数:
          task_count: 预计任务数量，用于计算需要测试多少代理
                      传 0 则测试全部代理
        """
        self._logger.info("开始初始化代理池...")
        raw_lines = self._fetch_proxies()
        self._proxies = self._parse_lines(raw_lines)
        self._logger.info("解析得到 %d 条代理", len(self._proxies))
        if not self._proxies:
            raise RuntimeError("代理池为空，无法继续")
        self._save_to_file(raw_lines)
        self._test_proxies(task_count)
        available = self.available_count
        self._logger.info(
            "代理池初始化完成: 已测试=%d, 可用=%d, 不可用=%d, 预计可注册=%d-%d 个账号",
            self._tested_count, available, self._tested_count - available,
            available * 3, available * self._config.max_uses,
        )
        if available == 0:
            raise RuntimeError("没有可用的代理，请检查网络或代理配置")

    def _fetch_proxies(self) -> list[str]:
        """
        获取代理列表：每次启动强制从 kookeey API 刷新

        ★ 流程：
          1. 通过本地 tun 代理(127.0.0.1:7897)请求 kookeey API
          2. API 返回后保存到本地文件(kookeey-1000.txt)
          3. API 请求失败时降级读取本地文件（上次保存的缓存）
        """
        api_url = self._config.api_url
        if not api_url:
            self._logger.warning("未配置 PROXY_API_URL，从本地文件加载（建议在 .env 中配置 API 地址）")
            return self._load_from_file()

        self._logger.info("正在从 kookeey API 刷新代理列表...")
        # 通过本地 tun 代理请求 kookeey API（境外环境可不配置 LOCAL_PROXY，直连即可）
        local_proxy = (self._config.local_proxy or "").strip()
        proxies_for_request = (
            {"http": local_proxy, "https": local_proxy} if local_proxy else None
        )
        try:
            response = requests.get(
                api_url,
                proxies=proxies_for_request,
                timeout=API_FETCH_TIMEOUT,
            )
            response.raise_for_status()
            content = response.text.strip()
            lines = [ln.strip() for ln in content.replace("\r\n", "\n").split("\n") if ln.strip()]
            if not lines:
                self._logger.warning("API 返回空列表，尝试从本地文件加载")
                return self._load_from_file()
            self._logger.info("API 返回 %d 条代理，已刷新到最新", len(lines))
            return lines
        except Exception as exc:
            self._logger.warning("API 刷新失败: %s，降级从本地文件加载", exc)
            return self._load_from_file()

    def _load_from_file(self) -> list[str]:
        """从本地文件加载代理列表"""
        pool_path = _PROJECT_DIR / self._config.pool_file
        if not pool_path.exists():
            raise RuntimeError(f"代理池文件不存在: {pool_path}")
        content = pool_path.read_text(encoding="utf-8").strip()
        lines = [ln.strip() for ln in content.replace("\r\n", "\n").split("\n") if ln.strip()]
        self._logger.info("从本地文件加载 %d 条代理: %s", len(lines), pool_path)
        return lines

    def _save_to_file(self, lines: list[str]) -> None:
        """保存代理列表到本地文件（供 API 失败时作为备用缓存）"""
        pool_path = _PROJECT_DIR / self._config.pool_file
        pool_path.write_text("\r\n".join(lines), encoding="utf-8")
        self._logger.info("代理列表已保存到本地缓存: %s（%d 条）", pool_path, len(lines))

    def _parse_lines(self, lines: list[str]) -> list[ProxyEntry]:
        """批量解析代理行"""
        proxies: list[ProxyEntry] = []
        for line in lines:
            entry = parse_proxy_line(line)
            if entry is not None:
                proxies.append(entry)
        return proxies

    def _test_single_proxy(self, proxy: ProxyEntry) -> bool:
        """测试单个代理是否可用"""
        socks5_url = proxy.socks5_url
        try:
            response = requests.get(
                PROXY_TEST_URL,
                proxies={"http": socks5_url, "https": socks5_url},
                timeout=self._config.test_timeout,
                allow_redirects=True,
            )
            return response.status_code < 500
        except Exception:
            return False

    def _test_proxies(self, task_count: int) -> None:
        """
        按需并发测试代理可用性

        task_count > 0 时只测试满足任务数量所需的代理（节省时间）
        task_count = 0 时测试全部代理
        """
        total = len(self._proxies)
        if task_count > 0:
            needed = -(-task_count // self._config.max_uses)
            test_count = min(total, max(needed * 5, task_count * 2))
        else:
            test_count = total

        proxies_to_test = self._proxies[:test_count]
        # 未参与测试的代理标记为不可用
        for proxy in self._proxies[test_count:]:
            proxy.is_available = False

        self._logger.info(
            "开始并发测试代理可用性（测试 %d/%d 条，并发=%d，超时=%ds）...",
            test_count, total, self._config.test_concurrency, self._config.test_timeout,
        )
        started = time.perf_counter()
        tested = 0
        passed = 0
        log_interval = 5 if test_count <= 30 else 50

        with ThreadPoolExecutor(max_workers=self._config.test_concurrency) as executor:
            future_to_proxy = {
                executor.submit(self._test_single_proxy, proxy): proxy
                for proxy in proxies_to_test
            }
            for future in as_completed(future_to_proxy):
                proxy = future_to_proxy[future]
                tested += 1
                try:
                    is_ok = future.result()
                except Exception:
                    is_ok = False
                proxy.is_available = is_ok
                if is_ok:
                    passed += 1
                if tested % log_interval == 0 or tested == test_count:
                    self._logger.info("测试进度: %d/%d（通过 %d）", tested, test_count, passed)

        self._tested_count = test_count
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._logger.info(
            "代理测试完成: 测试=%d/%d, 通过=%d, 失败=%d, 耗时=%dms",
            test_count, total, passed, test_count - passed, elapsed_ms,
        )

    def acquire(self) -> Optional[ProxyEntry]:
        """
        获取下一个可用代理（线程安全，轮询分配）

        返回:
          ProxyEntry 对象，或 None（无可用代理时）

        ★ 自动轮换逻辑：
          - 每个代理使用 max_uses 次后自动跳到下一个
          - 如果所有代理都已用完，返回 None
        """
        with self._lock:
            pool_size = len(self._proxies)
            if pool_size == 0:
                return None
            for offset in range(pool_size):
                idx = (self._index + offset) % pool_size
                proxy = self._proxies[idx]
                if proxy.is_available and proxy.use_count < self._config.max_uses:
                    proxy.use_count += 1
                    proxy.last_used_at = time.time()
                    if proxy.use_count >= self._config.max_uses:
                        self._index = (idx + 1) % pool_size
                    else:
                        self._index = idx
                    return proxy
            return None

    def report_failure(self, proxy: ProxyEntry) -> None:
        """
        报告代理使用失败

        连续失败 PROXY_FAIL_THRESHOLD 次后自动标记为不可用
        """
        with self._lock:
            proxy.fail_count += 1
            if proxy.fail_count >= PROXY_FAIL_THRESHOLD:
                proxy.is_available = False
                self._logger.warning(
                    "代理已标记为不可用（连续失败 %d 次）: %s",
                    proxy.fail_count, proxy.display_addr,
                )

    def report_success(self, proxy: ProxyEntry) -> None:
        """报告代理使用成功，重置失败计数"""
        with self._lock:
            proxy.fail_count = 0

    def get_stats(self) -> dict:
        """
        获取代理池统计信息

        返回字典:
          total: 代理总数
          available: 当前可用
          exhausted: 已用完（达到 max_uses 上限）
          failed: 测试失败或连续失败标记不可用
          total_uses: 累计使用次数
        """
        with self._lock:
            total = len(self._proxies)
            available = sum(
                1 for p in self._proxies
                if p.is_available and p.use_count < self._config.max_uses
            )
            exhausted = sum(
                1 for p in self._proxies
                if p.is_available and p.use_count >= self._config.max_uses
            )
            failed = sum(1 for p in self._proxies if not p.is_available)
            total_uses = sum(p.use_count for p in self._proxies)
            return {
                "total": total,
                "available": available,
                "exhausted": exhausted,
                "failed": failed,
                "total_uses": total_uses,
            }


# ============================================================
# 全局单例 - 代理池入口
# ============================================================

_pool_instance: Optional[ProxyPool] = None
_pool_lock = threading.Lock()


def get_proxy_pool(*, force_reinit: bool = False, task_count: int = 0) -> Optional[ProxyPool]:
    """
    获取全局代理池单例

    ★ 这是对外的主要入口函数，自动从 .env 读取配置

    参数:
      force_reinit: 是否强制重新初始化（每次调用都重新获取代理列表）
      task_count: 预计任务数量（用于优化测试范围）

    返回:
      ProxyPool 实例，或 None（代理未启用时）

    ★ 使用示例:
      pool = get_proxy_pool(task_count=10)
      if pool is None:
          print("代理未启用，使用直连")
      else:
          proxy = pool.acquire()
          # 使用 proxy.socks5_url 或 proxy.requests_proxies
    """
    global _pool_instance
    proxy_config = ProxyConfig.from_env()
    if not proxy_config.enabled:
        return None
    with _pool_lock:
        if _pool_instance is None or force_reinit:
            logger = _build_logger("proxy")
            _pool_instance = ProxyPool(proxy_config, logger)
            _pool_instance.initialize(task_count)
        return _pool_instance
