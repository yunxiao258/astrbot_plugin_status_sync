"""状态检测引擎：解析机器配置，执行本机/远程检测，汇总各机器状态。

机器配置格式（data/machines.json）：
{
  "machines": [
    {
      "name": "机器名（用于 /status <机器名> 查询）",
      "type": "local | ssh | winrm | http",
      "os": "windows | linux",            # 远程机器系统（默认 windows）
      "host": "ip", "port": 22,           # ssh/winrm 连接参数
      "username": "", "password": "",     # ssh/winrm 凭据
      "private_key": "",                  # ssh 私钥路径（可选）
      "base_url": "", "token": "",        # http 模式参数
      "targets": [
        {
          "name": "opencode",
          "processes": ["opencode", "opencode.exe"],   # 进程名/命令行子串，^ 开头为正则
          "cli": {"command": "opencode --version", "timeout": 15},
          "logs": [{"path": "~/.local/share/opencode/log", "lines": 5}]
        }
      ]
    }
  ]
}
"""

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime

from astrbot.api import logger

from .connectors import (
    LINUX_SYSTEM_SCRIPT,
    WIN_SYSTEM_SCRIPT,
    build_connector,
    linux_tail_script,
    win_tail_script,
)
from .secret import lock_machines_file

# 机器配置缺失时使用的内置默认配置
DEFAULT_MACHINES = {
    "machines": [
        {
            "name": "本机服务器",
            "type": "local",
            "os": "windows",
            "targets": [
                {
                    "name": "opencode",
                    "processes": ["opencode", "opencode.exe"],
                    "cli": {"command": "opencode --version", "timeout": 15},
                    "logs": [
                        {"path": "%USERPROFILE%/.local/share/opencode/log", "lines": 5}
                    ],
                },
                {"name": "mimocode", "processes": ["mimocode", "mimocode.exe"]},
                {"name": "openclaw", "processes": ["openclaw", "openclaw.exe"]},
            ],
        }
    ]
}


def now_iso() -> str:
    """当前时间 ISO 字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _match_process(name: str, cmdline: str, patterns) -> bool:
    """进程匹配：普通字符串做大小写不敏感子串匹配，^ 开头视为正则"""
    name = name or ""
    cmdline = cmdline or ""
    for p in patterns or []:
        p = str(p).strip()
        if not p:
            continue
        if p.startswith("^"):
            try:
                if re.search(p, name, re.I) or re.search(p, cmdline, re.I):
                    return True
            except re.error:
                continue
        else:
            if p.lower() in name.lower() or p.lower() in cmdline.lower():
                return True
    return False


def _run_cli_local(cmd: str, timeout: int) -> str:
    """本机执行 CLI 检测命令"""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True,
            timeout=timeout, text=True, errors="replace",
        )
        return ((r.stdout or "") + (r.stderr or "")).strip()[:2000]
    except subprocess.TimeoutExpired:
        return "[CLI 超时]"
    except Exception as e:  # noqa: BLE001
        return f"[CLI 错误] {e}"


def _decode_tail(path: str, lines: int) -> list[str]:
    """读取本地文件最后 N 行：自动尝试编码；目录则取最新修改的文件"""
    path = os.path.expandvars(os.path.expanduser(path))
    if os.path.isdir(path):
        files = []
        try:
            for f in os.listdir(path):
                fp = os.path.join(path, f)
                if os.path.isfile(fp):
                    files.append(fp)
        except OSError:
            return []
        if not files:
            return []
        path = max(files, key=os.path.getmtime)
    if not os.path.exists(path):
        return []
    raw = None
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            with open(path, "r", encoding=enc, errors="strict") as fh:
                raw = fh.read()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if raw is None:
        try:
            with open(path, "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            return []
    return [ln.rstrip("\r\n") for ln in raw.splitlines() if ln.strip()][-lines:]


class Monitor:
    """状态检测引擎"""

    def __init__(self, config, plugin_dir: str):
        self.config = config
        self.plugin_dir = plugin_dir
        self.machines: list[dict] = []
        self._connectors: dict[str, object] = {}
        self._last_signature: dict | None = None

    # ---------- 配置加载 ----------

    def machines_config_path(self) -> str:
        rel = self.config.get("machines_config_file", "data/machines.json")
        if os.path.isabs(rel):
            return rel
        return os.path.join(self.plugin_dir, rel)

    def reload(self) -> int:
        """重新加载机器配置（/status reload 或配置变更后调用）"""
        # 先关闭旧连接器，避免 reload 泄漏 SSH/WinRM 会话（close 为异步，调度到当前循环）
        old_connectors = self._connectors
        self._connectors = {}
        if old_connectors:
            try:
                loop = asyncio.get_event_loop()
                for conn in old_connectors.values():
                    loop.create_task(conn.close())
            except RuntimeError:
                pass  # 无事件循环：连接将在插件卸载 close_all 时清理
        self._last_signature = None
        return self._load_machines()

    def _load_machines(self) -> int:
        """读取机器配置并更新 machines 列表，返回机器数量"""
        path = self.machines_config_path()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            machines = data.get("machines", [])
            logger.info(f"状态同步：已加载机器配置 {path}，共 {len(machines)} 台")
            try:
                # icacls 可能耗时（15s 超时），在事件循环中时放入线程避免阻塞
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.run_in_executor(None, lock_machines_file, path)
                else:
                    lock_machines_file(path)
            except Exception:  # noqa: BLE001
                pass
        except FileNotFoundError:
            machines = DEFAULT_MACHINES["machines"]
            logger.warning(f"状态同步：机器配置 {path} 不存在，使用内置默认配置")
        except Exception as e:  # noqa: BLE001
            machines = DEFAULT_MACHINES["machines"]
            logger.error(f"状态同步：机器配置解析失败，使用内置默认配置: {e}")
        self.machines = machines
        return len(machines)

    async def close_all(self):
        """关闭全部连接器"""
        for conn in self._connectors.values():
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._connectors = {}

    def _get_connector(self, machine: dict):
        mname = machine.get("name", "?")
        if mname not in self._connectors:
            conn = build_connector(machine)
            if conn is None:
                raise RuntimeError(f"未知机器类型: {machine.get('type')}")
            self._connectors[mname] = conn
        return self._connectors[mname]

    # ---------- 检测入口 ----------

    async def check_all(self) -> list[dict]:
        """并发检测所有机器，单台失败不影响其他"""
        tasks = [self.check_machine(m) for m in self.machines]
        return await asyncio.gather(*tasks)

    async def check_machine(self, machine: dict) -> dict:
        """检测单台机器"""
        mtype = machine.get("type", "local")
        name = machine.get("name", "未命名")
        try:
            if mtype == "local":
                return await asyncio.to_thread(self._check_local, machine)
            if mtype in ("ssh", "winrm"):
                return await self._check_remote(machine, self._get_connector(machine))
            if mtype == "http":
                return await self._check_http(machine, self._get_connector(machine))
            return {
                "name": name, "type": mtype, "ok": False,
                "error": f"未知机器类型 {mtype}", "ts": now_iso(),
            }
        except Exception as e:  # noqa: BLE001
            logger.exception(f"检测机器 {name} 失败")
            return {
                "name": name, "type": mtype, "ok": False,
                "error": str(e), "ts": now_iso(),
            }

    # ---------- 状态变化检测 ----------

    async def detect_changes(self) -> list[str]:
        """对比上次检测结果，返回目标程序运行状态变化描述列表（无变化返回空列表）"""
        states = await self.check_all()
        sig = self._signature(states)
        changes = []
        if self._last_signature is not None:
            changes = await self._change_from_signature(sig, self._last_signature)
        self._last_signature = sig
        return changes

    @staticmethod
    async def _change_from_signature(sig: dict, prev: dict) -> list[str]:
        """对比新旧签名，返回变化描述"""
        changes = []
        for (mname, tname), (running, pid) in sig.items():
            old = prev.get((mname, tname))
            if old is None:
                continue
            if old != (running, pid):
                if running:
                    changes.append(f"{mname} · {tname} 已启动 (PID {pid})")
                else:
                    changes.append(f"{mname} · {tname} 已停止")
        return changes

    @staticmethod
    def _signature(states: list[dict]) -> dict:
        """提取各机器目标程序的运行状态签名"""
        sig = {}
        for s in states:
            for t in s.get("targets", []):
                sig[(s.get("name", "?"), t.get("name", "?"))] = (
                    bool(t.get("running")),
                    t.get("pid"),
                )
        return sig

    # ---------- 本机检测（psutil） ----------

    def _check_local(self, machine: dict) -> dict:
        import psutil

        name = machine.get("name", "本机")
        procs = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                procs.append(
                    (p.info["pid"], p.info["name"] or "", " ".join(p.info["cmdline"] or []))
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        targets = []
        for t in machine.get("targets", []):
            entry = self._build_target_entry(t, procs)
            for lc in t.get("logs", []):
                entry.setdefault("logs", []).extend(
                    _decode_tail(lc.get("path", ""), int(lc.get("lines", 5)))
                )
            if "logs" in entry:
                entry["logs"] = entry["logs"][-5:]
            targets.append(entry)
        vm = psutil.virtual_memory()
        return {
            "name": name, "type": "local", "ok": True,
            "host": "本机", "ts": now_iso(),
            "resources": {
                "cpu": psutil.cpu_percent(interval=0.5),
                "mem_total": vm.total,
                "mem_used": vm.total - vm.available,
            },
            "targets": targets,
        }

    def _build_target_entry(self, t: dict, procs: list) -> dict:
        """本地模式：从进程快照构建单个 target 状态"""
        import psutil

        tname = t.get("name", "?")
        matches = [
            m for m in procs if _match_process(m[1], m[2], t.get("processes"))
        ]
        entry = {"name": tname, "running": bool(matches)}
        if matches:
            pid, pname, _ = matches[0]
            entry["pid"] = pid
            entry["proc_name"] = pname
            try:
                pp = psutil.Process(pid)
                mem = pp.memory_info()
                if mem.rss:
                    entry["mem_kb"] = round(mem.rss / 1024)
            except Exception:  # noqa: BLE001
                pass
        cli_cfg = t.get("cli")
        if cli_cfg:
            entry["cli"] = _run_cli_local(
                str(cli_cfg.get("command", "")), int(cli_cfg.get("timeout", 15))
            )
        return entry

    # ---------- 远程检测（ssh / winrm） ----------

    async def _check_remote(self, machine: dict, conn) -> dict:
        os_type = (machine.get("os") or "windows").lower()
        name = machine.get("name", "远程")
        if os_type in ("windows", "win"):
            out = await conn.run(WIN_SYSTEM_SCRIPT, timeout=90)
            data = self._parse_win_json(out)
        else:
            out = await conn.run(LINUX_SYSTEM_SCRIPT, timeout=90)
            data = self._parse_linux_text(out)
        targets = []
        for t in machine.get("targets", []):
            tname = t.get("name", "?")
            matches = [
                p for p in data["processes"]
                if _match_process(p["name"], p["cmdline"], t.get("processes"))
            ]
            entry = {"name": tname, "running": bool(matches)}
            if matches:
                entry["pid"] = matches[0]["pid"]
            cli_cfg = t.get("cli")
            if cli_cfg:
                try:
                    out_cli = await conn.run(
                        str(cli_cfg.get("command", "")),
                        timeout=int(cli_cfg.get("timeout", 15)),
                    )
                    entry["cli"] = (out_cli or "")[:2000]
                except Exception as e:  # noqa: BLE001
                    entry["cli"] = f"[CLI 错误] {e}"
            for lc in t.get("logs", []):
                path = lc.get("path", "")
                n = int(lc.get("lines", 5))
                script = (
                    win_tail_script(path, n) if os_type.startswith("win")
                    else linux_tail_script(path, n)
                )
                try:
                    tail = await conn.run(script, timeout=30)
                    if tail:
                        entry.setdefault("logs", []).extend(
                            [ln.rstrip() for ln in tail.splitlines() if ln.strip()]
                        )
                except Exception:  # noqa: BLE001
                    pass
            if "logs" in entry:
                entry["logs"] = entry["logs"][-5:]
            targets.append(entry)
        return {
            "name": name, "type": machine.get("type"), "ok": True,
            "host": machine.get("host", ""), "ts": now_iso(),
            "resources": data["resources"], "targets": targets,
        }

    @staticmethod
    def _parse_win_json(out: str) -> dict:
        """解析 Windows PowerShell 输出的 JSON（解析失败不崩溃，返回空数据）"""
        data = None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            start = out.find("{")
            if start >= 0:
                # 截取到最后一个 '}' 再解析，避免前导噪声/截断导致 ValueError
                end = out.rfind("}")
                candidate = out[start:] if end < 0 else out[start:end + 1]
                try:
                    data = json.loads(candidate)
                except (json.JSONDecodeError, ValueError):
                    data = None
        if not isinstance(data, dict):
            return {"processes": [], "resources": {"cpu": 0, "mem_total": 0, "mem_used": 0}}
        processes = []
        for p in data.get("processes") or []:
            processes.append({
                "pid": p.get("pid"),
                "name": str(p.get("name", "")),
                "cmdline": str(p.get("cmdline", "")),
            })
        mem_total = int(data.get("mem_total") or 0)
        mem_free = int(data.get("mem_free") or 0)
        try:
            cpu = float(data.get("cpu") or 0)
        except (TypeError, ValueError):
            cpu = 0.0
        return {
            "processes": processes,
            "resources": {
                "cpu": cpu,
                "mem_total": mem_total,
                "mem_used": max(0, mem_total - mem_free),
            },
        }

    @staticmethod
    def _parse_linux_text(out: str) -> dict:
        """解析 Linux 分段文本输出（PROCS/MEM/CPU）"""
        procs = []
        mem_line = None
        cpu_line = None
        section = None
        for ln in out.splitlines():
            ln = ln.strip()
            if ln == "===PROCS===":
                section = "procs"
                continue
            if ln == "===MEM===":
                section = "mem"
                continue
            if ln == "===CPU===":
                section = "cpu"
                continue
            if not ln:
                continue
            if section == "procs":
                parts = ln.split(None, 2)
                if len(parts) >= 2:
                    procs.append({
                        "pid": int(parts[0]),
                        "name": parts[1],
                        "cmdline": parts[2] if len(parts) > 2 else "",
                    })
            elif section == "mem" and mem_line is None and ln.lower().startswith("mem"):
                mem_line = ln
            elif section == "cpu" and cpu_line is None and ln.startswith("%Cpu"):
                cpu_line = ln
        mem_total_mb = mem_used_mb = 0
        if mem_line:
            parts = mem_line.split()
            try:
                mem_total_mb = int(parts[1])
                mem_used_mb = int(parts[2])
            except (ValueError, IndexError):
                pass
        cpu = 0.0
        if cpu_line:
            m = re.search(r"([\d.]+)\s*us", cpu_line)
            if m:
                try:
                    cpu = float(m.group(1))
                except ValueError:
                    pass
        return {
            "processes": procs,
            "resources": {
                "cpu": cpu,
                "mem_total": mem_total_mb * 1024 * 1024,
                "mem_used": mem_used_mb * 1024 * 1024,
            },
        }

    # ---------- HTTP 检测 ----------

    async def _check_http(self, machine: dict, conn) -> dict:
        name = machine.get("name", "远程")
        data = await conn.fetch_status()
        return {
            "name": name, "type": "http", "ok": True,
            "host": machine.get("base_url", ""), "ts": now_iso(),
            "http_data": data, "targets": [],
        }
