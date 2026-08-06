"""权限远程控制：日志事件监控 + opencode 配置规则管理 + serve 模式远程批准。

三层能力：
1. 日志监控：tail opencode 日志的权限评估行（message=evaluated permission=...），
   按级别转发到群（allow 正常操作 / deny 被阻止 / ask 请求批准）。
2. 规则管理：远程读写 opencode 全局配置（~/.config/opencode/opencode.jsonc）的
   permission 规则（allow/ask/deny），对后续请求生效（opencode 自动热加载配置）。
3. serve 远程批准：检测到 opencode serve 模式（HTTP API）时，订阅权限请求事件，
   管理员可在群里直接同意/拒绝当前挂起的请求。
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field

from astrbot.api import logger

# ---------------- 日志行解析 ----------------

_EVAL_RE = re.compile(
    r"timestamp=(?P<ts>[^ ]+) level=(?P<level>\S+) run=(?P<run>\S+)"
    r" message=(?P<msg>.+)$"
)
_MSG_RE = re.compile(
    r"evaluated permission=(?P<tool>\S+)"
    r" pattern=(?P<pattern>(?:\"(?:[^\"\\]|\\.)*\"|\S+))"
    r" action\.permission=(?P<ap>\S+) action\.action=(?P<action>\S+)"
    r"( action\.pattern=(?P<apat>\S+))?"
)
_ASKING_RE = re.compile(
    r"asking id=(?P<pid>\S+) permission=(?P<tool>\S+) patterns=(?P<patterns>.+)$"
)


@dataclass
class PermEvent:
    """一条权限评估事件"""

    ts: str
    source: str
    tool: str
    pattern: str
    action: str
    raw: str
    pid: str = ""  # 挂起权限请求 ID（asking 行，serve 批准用）


def parse_eval_line(line: str, source: str = "") -> PermEvent | None:
    """解析 opencode 日志中的权限评估行，无法解析返回 None。

    两种格式：
    - evaluated: timestamp=... message=evaluated permission=... action.action=ask|allow|deny
    - asking:    timestamp=... message=asking id=per_xxx permission=... patterns=[...]
      （asking 行表示有请求正在挂起等待批准，从中提取权限 ID 供 serve 远程批准）
    """
    m = _EVAL_RE.search(line)
    if not m:
        return None
    msg = m.group("msg")
    mm = _MSG_RE.search(msg)
    pid = ""
    if not mm:
        # asking 行：带挂起权限 ID
        am = _ASKING_RE.search(msg)
        if not am:
            return None
        mm = am
        pid = am.group("pid")
        pattern = am.group("patterns").strip()
        # 剥掉日志 C 转义引号后按 JSON 数组/字符串解码
        pattern = pattern.replace('\\"', '"')
        if len(pattern) >= 2 and pattern[0] == pattern[-1] == '"':
            try:
                pattern = json.loads(pattern)
            except json.JSONDecodeError:
                pattern = pattern[1:-1]
        if len(pattern) >= 2 and pattern[0] == "[" and pattern[-1] == "]":
            try:
                decoded = json.loads(pattern)
                if isinstance(decoded, list):
                    pattern = ", ".join(str(x) for x in decoded)
            except json.JSONDecodeError:
                pass
        return PermEvent(
            ts=m.group("ts"),
            source=source,
            tool=am.group("tool"),
            pattern=pattern,
            action="ask",
            raw=line.strip(),
            pid=pid,
        )
    pattern = mm.group("pattern")
    if pattern.startswith('"') and pattern.endswith('"'):
        try:
            pattern = json.loads(pattern)
        except json.JSONDecodeError:
            pattern = pattern[1:-1]
    return PermEvent(
        ts=m.group("ts"),
        source=source,
        tool=mm.group("tool"),
        pattern=pattern,
        action=mm.group("action"),
        raw=line.strip(),
    )


# ---------------- 日志监控 ----------------

def _tail_new_lines(path: str, offset: int) -> tuple[list[str], int]:
    """从 offset 字节开始读取文件新增行，返回 (新行列表, 新偏移)。
    文件被轮转（变小）时从头读。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            data = fh.read()
            new_offset = fh.tell()
    except OSError:
        return [], offset
    lines = data.splitlines()
    return lines, new_offset


class PermissionMonitor:
    """监控 opencode 日志中的权限评估事件"""

    def __init__(self, config, plugin_dir: str):
        self.cfg = config
        self.plugin_dir = plugin_dir
        self._cursor: dict[str, int] = {}
        self._buffer: list[PermEvent] = []

    def _cursor_path(self) -> str:
        return os.path.join(self.plugin_dir, "data", "perm_cursor.json")

    def load_cursor(self):
        try:
            with open(self._cursor_path(), "r", encoding="utf-8") as fh:
                self._cursor = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self._cursor = {}

    def save_cursor(self):
        try:
            parent = os.path.dirname(self._cursor_path())
            os.makedirs(parent, exist_ok=True)
            with open(self._cursor_path(), "w", encoding="utf-8") as fh:
                json.dump(self._cursor, fh)
        except OSError as e:
            logger.warning(f"权限监控游标保存失败: {e}")

    @staticmethod
    def _expand(path: str) -> str:
        p = os.path.expandvars(os.path.expanduser(path))
        return p

    def log_paths(self, machines: list[dict]) -> list[str]:
        """从机器配置中收集日志路径，目录取其中最新的文件"""
        paths = []
        for m in machines:
            for t in m.get("targets") or []:
                for lg in t.get("logs") or []:
                    raw = lg.get("path", "") if isinstance(lg, dict) else str(lg)
                    p = self._expand(raw)
                    if os.path.isdir(p):
                        try:
                            files = [
                                os.path.join(p, f)
                                for f in os.listdir(p)
                                if os.path.isfile(os.path.join(p, f))
                                and not f.endswith(".zip")
                            ]
                        except OSError:
                            continue
                        if not files:
                            continue
                        latest = max(files, key=os.path.getmtime)
                        paths.append(latest)
                    elif os.path.isfile(p):
                        paths.append(p)
        return paths

    def poll(self, machines: list[dict]) -> list[PermEvent]:
        """检查所有日志路径，返回新出现的权限事件（同时更新事件缓冲）"""
        events = []
        for path in self.log_paths(machines):
            offset = self._cursor.get(path, 0)
            lines, new_offset = _tail_new_lines(path, offset)
            for ln in lines:
                ev = parse_eval_line(ln, source=os.path.basename(path))
                if ev:
                    events.append(ev)
            self._cursor[path] = new_offset
        if events:
            self._buffer.extend(events)
            max_buf = max(30, int(self.cfg.get("permission_event_buffer", 30)))
            del self._buffer[: -max_buf]
            self.save_cursor()
        return events

    def recent(self, limit: int = 10) -> list[PermEvent]:
        return list(reversed(self._buffer[-limit:]))


# ---------------- 规则管理 ----------------

_STRIP_JSONC_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRIP_LINE_RE = re.compile(r"(?m)^\s*//[^\n]*")


def strip_jsonc_comments(text: str) -> str:
    """剥离 JSONC 注释（状态机：跳过字符串内的 // 与 /* */）"""
    out = []
    i = 0
    n = len(text)
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            out.append("\n")
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            out.append("\n")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class RuleManager:
    """远程管理 opencode 全局配置中的 permission 规则"""

    def __init__(self, config_file: str):
        self.config_file = config_file

    def load(self) -> dict:
        """读取配置（剥离 JSONC 注释），不存在或损坏返回 {}"""
        if not os.path.exists(self.config_file):
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as fh:
                text = fh.read()
            return json.loads(strip_jsonc_comments(text))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"读取 opencode 配置失败: {e}")
            return {}

    def save(self, data: dict):
        parent = os.path.dirname(self.config_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def set_rule(self, tool: str, pattern: str, action: str) -> str:
        """设置一条规则，返回描述文本"""
        data = self.load()
        perm = data.get("permission")
        if isinstance(perm, str):
            # 全局单一值（如 {"permission": "allow"}）转为对象形式
            global_val = perm
            perm = {}
            data["permission"] = perm
            if tool != "*" and global_val:
                perm["*"] = global_val
        elif not isinstance(perm, dict):
            perm = {}
            data["permission"] = perm
        cur = perm.get(tool)
        if not isinstance(cur, dict):
            base = cur if isinstance(cur, str) and cur in ("allow", "ask", "deny") else None
            cur = {}
            if base:
                cur["*"] = base
            perm[tool] = cur
        if pattern == "*" and tool != "*":
            cur["*"] = action
        else:
            cur[pattern] = action
        self.save(data)
        return f"已设置规则: {tool} 「{pattern}」→ {action}"

    def remove_rule(self, tool: str, pattern: str) -> str:
        data = self.load()
        perm = data.get("permission")
        if not isinstance(perm, dict):
            return "没有可删除的规则"
        cur = perm.get(tool)
        if not isinstance(cur, dict) or pattern not in cur:
            return f"规则不存在: {tool} 「{pattern}」"
        del cur[pattern]
        if not cur:
            del perm[tool]
        if not perm:
            del data["permission"]
        self.save(data)
        return f"已删除规则: {tool} 「{pattern}」"

    def clear_rules(self) -> str:
        data = self.load()
        if "permission" not in data:
            return "本来就没有 permission 规则"
        del data["permission"]
        self.save(data)
        return "已清空全部 permission 规则（恢复 opencode 默认权限）"

    def format_rules(self) -> str:
        data = self.load()
        perm = data.get("permission")
        if not perm:
            return "当前无自定义规则（使用 opencode 默认权限）\n配置: " + self.config_file
        lines = ["当前权限规则:"]
        for tool, rule in perm.items():
            if isinstance(rule, dict):
                for pat, act in rule.items():
                    lines.append(f"  {tool} 「{pat}」→ {act}")
            else:
                lines.append(f"  {tool} → {rule}")
        lines.append(f"配置文件: {self.config_file}")
        return "\n".join(lines)


# ---------------- serve 模式远程批准 ----------------


@dataclass
class PermRequest:
    """一条挂起的权限请求"""

    session_id: str
    permission_id: str
    tool: str
    detail: str
    ts: float = field(default_factory=time.time)


def parse_sse_event(event: str, data: str) -> PermRequest | None:
    """解析 SSE 事件为权限请求。返回 None 表示非权限请求事件。"""
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    etype = event or payload.get("type", "")
    if "permission.request" not in str(etype):
        return None
    session_id = payload.get("sessionID") or payload.get("session_id") or payload.get("sessionId")
    perm_id = (
        payload.get("permissionID")
        or payload.get("permission_id")
        or payload.get("permissionId")
        or payload.get("id")
    )
    if not session_id or not perm_id:
        return None
    tool = (
        payload.get("toolName")
        or payload.get("tool")
        or payload.get("name")
        or "未知工具"
    )
    perm = payload.get("permission") or {}
    detail = (
        payload.get("description")
        or payload.get("pattern")
        or (perm.get("pattern") if isinstance(perm, dict) else None)
        or payload.get("title")
        or ""
    )
    return PermRequest(session_id=str(session_id), permission_id=str(perm_id),
                       tool=str(tool), detail=str(detail))


class ServeClient:
    """连接 opencode serve HTTP API，订阅权限请求并远程响应"""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = None
        if username or password:
            import base64
            raw = f"{username or 'opencode'}:{password}".encode()
            self.auth = {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        self.pending: dict[str, PermRequest] = {}
        self._requests = None

    def _get_requests(self):
        if self._requests is None:
            import requests

            self._requests = requests
        return self._requests

    def probe(self) -> bool:
        """探测 serve 是否可用"""
        try:
            r = self._get_requests().get(
                f"{self.base_url}/global/health", timeout=5, headers=self.auth
            )
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def respond(self, permission_id: str, response: str, remember: bool = False) -> bool:
        """响应权限请求。response: once/always/reject"""
        req = self.pending.get(permission_id)
        if not req:
            return False
        try:
            r = self._get_requests().post(
                f"{self.base_url}/session/{req.session_id}/permissions/{req.permission_id}",
                json={"response": response, "remember": remember},
                timeout=10,
                headers=self.auth,
            )
            ok = r.status_code == 200
            if ok:
                # serve 可能返回裸 true/false 或 {"result": ...}
                try:
                    body = r.json()
                except ValueError:
                    body = None
                if body is False or (
                    isinstance(body, dict) and body.get("result", True) is False
                ):
                    ok = False
            if ok:
                self.pending.pop(permission_id, None)
            return ok
        except Exception:  # noqa: BLE001
            return False

    def prune(self, max_age: float = 600):
        now = time.time()
        stale = [pid for pid, r in self.pending.items() if now - r.ts > max_age]
        for pid in stale:
            self.pending.pop(pid, None)

    async def listen_loop(self, on_request, loop=None):
        """SSE 事件监听循环（断线自动重连）"""
        while True:
            try:
                await asyncio.to_thread(self._listen_once, on_request, loop)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"serve 事件监听异常: {e}")
            await asyncio.sleep(5)

    def _listen_once(self, on_request, loop):
        r = self._get_requests().get(
            f"{self.base_url}/global/event", stream=True, timeout=None, headers=self.auth
        )
        event = ""
        data_lines = []
        for raw in r.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            if raw.startswith("event:"):
                event = raw[len("event:"):].strip()
            elif raw.startswith("data:"):
                data_lines.append(raw[len("data:"):].strip())
            elif raw == "":
                if event and data_lines:
                    req = parse_sse_event(event, "\n".join(data_lines))
                    if req:
                        self.pending[req.permission_id] = req
                        if loop is not None:
                            # 投递到 AstrBot 主事件循环执行
                            asyncio.run_coroutine_threadsafe(on_request(req), loop)
                        else:
                            asyncio.run(on_request(req))
                event = ""
                data_lines = []
