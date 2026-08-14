"""WebSocket 实时推送服务器：把机器状态变化实时推给桌面端订阅者。

客户端连接：ws://<astrbot主机>:<ws_port>/?token=<ws_token>
若未配置 ws_token（或留空）则本机内网可直连，不鉴权。

依赖：websockets（pip install websockets）
"""

import asyncio
import json
import secrets

from astrbot.api import logger

try:
    import websockets  # noqa: F401
    WEBSOCKETS_OK = True
except ImportError:
    WEBSOCKETS_OK = False


class WsHub:
    """基于 websockets 库的轻量广播服务器，按 token 鉴权。"""

    def __init__(self, port: int = 8765, token: str = ""):
        self.port = int(port or 8765)
        self.token = (token or "").strip()
        self._clients: set = set()
        self._server = None
        self._task: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        """websockets 库是否可用"""
        return WEBSOCKETS_OK

    @property
    def running(self) -> bool:
        return self._server is not None

    def _authorized(self, path_or_query: str) -> bool:
        """校验连接路径/query 中的 token；未配置 token 时放行"""
        if not self.token:
            return True
        q = path_or_query or ""
        # 支持 ?token=xxx 与 ?access_token=xxx
        for key in ("token=", "access_token="):
            idx = q.find(key)
            if idx >= 0:
                val = q[idx + len(key):]
                val = val.split("&")[0].strip()
                return secrets.compare_digest(val, self.token)
        return False

    async def _handler(self, ws):
        """单个客户端连接处理：鉴权 → 注册 → 挂起等待断开"""
        try:
            path = str(getattr(ws, "path", "") or "")
            if not self._authorized(path):
                await ws.close(code=4001, reason="unauthorized")
                return
            self._clients.add(ws)
            try:
                await ws.wait_closed()
            finally:
                self._clients.discard(ws)
        except Exception:  # noqa: BLE001
            self._clients.discard(ws)

    async def start(self):
        """启动监听（0.0.0.0:port）。websockets 不可用时返回 None。"""
        if not self.available:
            logger.warning("状态同步：未安装 websockets 库，WebSocket 推送不可用")
            return None
        if self.running:
            return self._server
        self._server = await websockets.serve(self._handler, "0.0.0.0", self.port)
        logger.info(f"状态同步：WebSocket 推送服务已启动 ws://0.0.0.0:{self.port}")
        self._task = asyncio.create_task(self._server.wait_closed())
        return self._server

    async def stop(self):
        """停止监听并断开所有客户端"""
        if self._task:
            self._task.cancel()
            self._task = None
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()

    def broadcast(self, payload: dict) -> int:
        """向所有在线客户端异步推送 JSON，返回在线客户端数。"""
        data = json.dumps(payload, ensure_ascii=False)
        n = 0
        for ws in list(self._clients):
            asyncio.ensure_future(self._push(ws, data))
            n += 1
        return n

    async def _push(self, ws, data: str):
        """单客户端推送：发送失败（连接已断开）时移除客户端"""
        try:
            await ws.send(data)
        except Exception:  # noqa: BLE001
            self._clients.discard(ws)
