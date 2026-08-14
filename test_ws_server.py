# -*- coding: utf-8 -*-
"""status_sync WebSocket 推送模块测试：鉴权、客户端注册/移除、广播。

不依赖真实网络：用假 WS 连接对象驱动 handler 逻辑。
运行：python test_ws_server.py
"""

import asyncio
import sys
import unittest

_PLUGIN_DIR = r"D:\astrbot\data\plugins\astrbot_plugin_status_sync"
sys.path.insert(0, _PLUGIN_DIR)

from ws_server import WsHub  # noqa: E402


class FakeWS:
    """模拟 websockets 连接对象：记录 send/close，可控制 wait_closed 行为"""

    def __init__(self, path="/?token=sec", hold=False):
        self.path = path
        self.sent = []
        self.closed = False
        self.hold = hold
        self.wait_closed_waiter = asyncio.Event()

    async def send(self, data):
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = True
        self.wait_closed_waiter.set()

    async def wait_closed(self):
        if self.hold:
            await self.wait_closed_waiter.wait()


class TestAuth(unittest.TestCase):
    def test_no_token_allows_all(self):
        hub = WsHub(8765, "")
        self.assertTrue(hub._authorized(""))

    def test_token_missing_denied(self):
        hub = WsHub(8765, "secret")
        self.assertFalse(hub._authorized("/"))

    def test_token_in_query_allowed(self):
        hub = WsHub(8765, "secret")
        self.assertTrue(hub._authorized("/?token=secret"))

    def test_token_query_extra_params(self):
        hub = WsHub(8765, "secret")
        self.assertTrue(hub._authorized("/?token=secret&x=1"))

    def test_access_token_key(self):
        hub = WsHub(8765, "secret")
        self.assertTrue(hub._authorized("/?access_token=secret"))

    def test_wrong_token_denied(self):
        hub = WsHub(8765, "secret")
        self.assertFalse(hub._authorized("/?token=wrong"))


class TestHandler(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_unauthorized_closed(self):
        hub = WsHub(8765, "secret")
        ws = FakeWS(path="/?token=bad")
        self._run(hub._handler(ws))
        self.assertTrue(ws.closed)
        self.assertNotIn(ws, hub._clients)

    def test_authorized_registered_then_removed(self):
        hub = WsHub(8765, "secret")
        ws = FakeWS(path="/?token=secret")
        self._run(hub._handler(ws))
        self.assertNotIn(ws, hub._clients)

    def test_hold_client_stays_registered(self):
        hub = WsHub(8765, "")
        ws = FakeWS(path="/", hold=True)

        async def scenario():
            task = asyncio.create_task(hub._handler(ws))
            await asyncio.sleep(0.02)
            in_clients = ws in hub._clients
            await ws.close()
            await task
            return in_clients, ws in hub._clients

        registered, removed_after = self._run(scenario())
        self.assertTrue(registered)
        self.assertFalse(removed_after)


class TestBroadcast(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_broadcast_pushes_json(self):
        hub = WsHub(8765, "")
        ws = FakeWS(path="/", hold=True)

        async def scenario():
            task = asyncio.create_task(hub._handler(ws))
            await asyncio.sleep(0.02)
            n = hub.broadcast({"type": "status", "machines": [{"name": "pc"}]})
            await asyncio.sleep(0.02)
            task.cancel()
            return n, ws.sent

        n, sent = self._run(scenario())
        self.assertEqual(n, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn('"type": "status"', sent[0])
        self.assertIn('"name": "pc"', sent[0])

    def test_broadcast_no_clients(self):
        hub = WsHub(8765, "")
        self.assertEqual(hub.broadcast({"a": 1}), 0)

    def test_broadcast_drops_dead_client(self):
        hub = WsHub(8765, "")
        dead = FakeWS(path="/")

        async def send(data):
            raise RuntimeError("socket gone")
        dead.send = send
        hub._clients.add(dead)

        async def scenario():
            hub.broadcast({"a": 1})
            await asyncio.sleep(0.02)
            return dead in hub._clients

        self.assertFalse(self._run(scenario()))


class TestServerControl(unittest.TestCase):
    @staticmethod
    def _run(coro):
        return asyncio.run(coro)
    def test_available_flag(self):
        hub = WsHub(8765, "")
        self.assertTrue(hub.available)

    def test_start_stop_lifecycle(self):
        hub = WsHub(8799, "tok")

        async def scenario():
            srv = await hub.start()
            self.assertIsNotNone(srv)
            self.assertTrue(hub.running)
            # 重复 start 幂等
            srv2 = await hub.start()
            self.assertIs(srv, srv2)
            await hub.stop()
            self.assertFalse(hub.running)

        asyncio.run(scenario())

    def test_stop_closes_clients(self):
        hub = WsHub(8800, "")
        ws = FakeWS(path="/", hold=True)

        async def scenario():
            await hub.start()
            task = asyncio.create_task(hub._handler(ws))
            await asyncio.sleep(0.02)
            await hub.stop()
            await asyncio.sleep(0.02)
            task.cancel()
            return ws.closed, len(hub._clients)

        closed, remaining = self._run(scenario())
        self.assertTrue(closed)
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)