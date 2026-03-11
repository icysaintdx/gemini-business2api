"""
本地 SOCKS5 中继 - 无认证本地端口 → 带认证的上游 SOCKS5 代理

★ 用途：
  解决 Chromium/Playwright 不支持 SOCKS5 代理认证的限制。
  浏览器连接 socks5://127.0.0.1:<local_port>（无认证），
  中继服务自动带认证转发到上游 kookeey SOCKS5 代理。

★ 使用方式：
  from proxy import LocalSocksRelay

  relay = LocalSocksRelay(
      upstream_host="gate.kookeey.info",
      upstream_port=1000,
      upstream_username="xxx",
      upstream_password="xxx",
  )
  relay.start()
  print(f"本地代理端口: {relay.port}")

  # 方式1: 用于 Playwright
  browser.new_context(proxy=relay.playwright_proxy)

  # 方式2: 用于 requests
  requests.get(url, proxies={"http": f"socks5://127.0.0.1:{relay.port}",
                              "https": f"socks5://127.0.0.1:{relay.port}"})

  # 使用完毕后停止
  relay.stop()

★ 结合代理池使用：
  from proxy import get_proxy_pool, LocalSocksRelay

  pool = get_proxy_pool(task_count=5)
  proxy = pool.acquire()
  relay = LocalSocksRelay(proxy.host, proxy.port, proxy.username, proxy.password)
  relay.start()
  # ... 使用 relay.port 作为本地无认证代理端口 ...
  relay.stop()
"""
from __future__ import annotations

import select
import socket
import struct
import threading
from typing import Optional

import socks  # PySocks

RELAY_BUFFER_SIZE = 65536
RELAY_TIMEOUT = 120


class LocalSocksRelay:
    """
    本地 SOCKS5 中继服务

    为每个浏览器任务启动一个实例，绑定随机本地端口，
    将无认证的本地 SOCKS5 请求转发到带认证的上游代理。
    """

    def __init__(
        self,
        upstream_host: str,
        upstream_port: int,
        upstream_username: str = "",
        upstream_password: str = "",
    ) -> None:
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._upstream_username = upstream_username
        self._upstream_password = upstream_password
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.port = 0  # 启动后自动分配

    @property
    def playwright_proxy(self) -> dict:
        """返回可直接传给 Playwright browser.new_context() 的代理配置（无认证）"""
        return {"server": f"socks5://127.0.0.1:{self.port}"}

    @property
    def local_socks5_url(self) -> str:
        """本地 SOCKS5 代理 URL（无认证）"""
        return f"socks5://127.0.0.1:{self.port}"

    def start(self) -> None:
        """启动本地中继，绑定随机端口"""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._server.listen(16)
        self._server.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止中继服务"""
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client, _ = self._server.accept()
                handler = threading.Thread(
                    target=self._handle_client, args=(client,), daemon=True
                )
                handler.start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle_client(self, client: socket.socket) -> None:
        remote: Optional[socket.socket] = None
        try:
            client.settimeout(RELAY_TIMEOUT)

            # SOCKS5 握手
            greeting = client.recv(256)
            if len(greeting) < 2 or greeting[0] != 0x05:
                return
            # 回复：不需要认证
            client.sendall(b"\x05\x00")

            # 连接请求
            request = client.recv(256)
            if len(request) < 4:
                return
            _ver, cmd, _rsv, atyp = struct.unpack("!BBBB", request[:4])
            if cmd != 0x01:  # 仅支持 CONNECT
                client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                return

            # 解析目标地址
            if atyp == 0x01:  # IPv4
                dst_addr = socket.inet_ntoa(request[4:8])
                dst_port = struct.unpack("!H", request[8:10])[0]
            elif atyp == 0x03:  # 域名
                name_len = request[4]
                dst_addr = request[5: 5 + name_len].decode("ascii")
                dst_port = struct.unpack("!H", request[5 + name_len: 7 + name_len])[0]
            elif atyp == 0x04:  # IPv6
                dst_addr = socket.inet_ntop(socket.AF_INET6, request[4:20])
                dst_port = struct.unpack("!H", request[20:22])[0]
            else:
                client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                return

            # 通过上游 SOCKS5 代理（带认证）连接目标
            remote = socks.socksocket()
            remote.set_proxy(
                socks.SOCKS5,
                self._upstream_host,
                self._upstream_port,
                rdns=True,
                username=self._upstream_username or None,
                password=self._upstream_password or None,
            )
            remote.settimeout(RELAY_TIMEOUT)
            remote.connect((dst_addr, dst_port))

            # 回复连接成功
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

            # 双向转发
            self._relay(client, remote)

        except Exception:
            try:
                client.sendall(b"\x05\x01\x00\x01" + b"\x00" * 6)
            except Exception:
                pass
        finally:
            for s in (client, remote):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass

    @staticmethod
    def _relay(client: socket.socket, remote: socket.socket) -> None:
        """双向数据转发，直到任一端关闭"""
        pair = [client, remote]
        while True:
            try:
                readable, _, exceptional = select.select(pair, [], pair, RELAY_TIMEOUT)
            except Exception:
                break
            if exceptional or not readable:
                break
            for sock in readable:
                try:
                    data = sock.recv(RELAY_BUFFER_SIZE)
                except Exception:
                    return
                if not data:
                    return
                target = remote if sock is client else client
                try:
                    target.sendall(data)
                except Exception:
                    return
