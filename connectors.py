"""连接器模块：本地执行、SSH、WinRM、HTTP 四种执行通道。

所有连接器只负责把一段检测脚本拿到目标执行并返回 stdout 文本，
具体的状态解析由 monitor.py 完成。paramiko / pywinrm 采用懒加载，
未安装时仅在真正使用对应类型的机器时才会报错。
"""

import asyncio
import io
import os
import subprocess

from astrbot.api import logger

from .secret import clean_host, resolve_secret

# ---------- 远程检测脚本模板 ----------

# Windows 远程进程/资源检测（PowerShell，输出 UTF-8 JSON）
WIN_SYSTEM_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$os = Get-CimInstance Win32_OperatingSystem
$cpuAvg = @((Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average)
$procs = @(Get-CimInstance Win32_Process | Select-Object ProcessId, Name, CommandLine)
$arr = @()
foreach ($p in $procs) {
    $arr += [pscustomobject]@{ pid = $p.ProcessId; name = [string]$p.Name; cmdline = [string]$p.CommandLine }
}
[pscustomobject]@{
    processes = $arr
    mem_total = [long]$os.TotalVisibleMemorySize * 1024
    mem_free  = [long]$os.FreePhysicalMemory * 1024
    cpu       = if ($null -eq $cpuAvg) { 0 } else { [math]::Round($cpuAvg, 1) }
} | ConvertTo-Json -Compress -Depth 5
"""

# Linux 远程检测（bash，分段文本输出，本地解析）
LINUX_SYSTEM_SCRIPT = (
    "echo '===PROCS==='\n"
    "ps -eo pid=,comm=,args= --no-headers\n"
    "echo '===MEM==='\n"
    "free -m\n"
    "echo '===CPU==='\n"
    "top -bn1 | head -4\n"
)


def win_tail_script(path: str, lines: int) -> str:
    """PowerShell 日志尾部脚本（支持 ~ 展开与目录取最新文件）"""
    safe_path = path.replace("~", "$env:USERPROFILE").replace("'", "''")
    return (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        f"$p = '{safe_path}'\n"
        "if ((Get-Item -LiteralPath $p -ErrorAction SilentlyContinue).PSIsContainer) {\n"
        "  $p = Get-ChildItem -LiteralPath $p -File -ErrorAction SilentlyContinue | "
        "Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName\n"
        "}\n"
        f"if ($p -and (Test-Path -LiteralPath $p)) {{ Get-Content -LiteralPath $p -Tail {int(lines)} -Encoding UTF8 }}\n"
    )


def linux_tail_script(path: str, lines: int) -> str:
    """bash 日志尾部脚本（支持 ~ 展开与目录取最新文件）"""
    return (
        f"p={path!r}\n"
        'if [ -d "$p" ]; then p=$(ls -t "$p" | head -1); fi\n'
        f'tail -n {int(lines)} "$p" 2>/dev/null || true\n'
    )


class BaseConnector:
    """连接器基类"""

    type = "base"

    async def run(self, script: str, timeout: int = 30) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class LocalConnector(BaseConnector):
    """本机执行（用于 CLI 检测命令）"""

    type = "local"

    async def run(self, script: str, timeout: int = 30) -> str:
        def _run():
            try:
                r = subprocess.run(
                    script, shell=True, capture_output=True,
                    timeout=timeout, text=True, errors="replace",
                )
                return ((r.stdout or "") + (r.stderr or "")).strip()
            except subprocess.TimeoutExpired:
                return "[CLI 超时]"
            except Exception as e:  # noqa: BLE001
                return f"[CLI 错误] {e}"

        return await asyncio.to_thread(_run)


class SshConnector(BaseConnector):
    """SSH 远程执行（paramiko，懒加载）"""

    type = "ssh"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.host = clean_host(cfg.get("host", ""))
        self.port = int(cfg.get("port") or 22)
        self.username = cfg.get("username", "")
        self.password = resolve_secret(cfg.get("password", ""))
        self.private_key_raw = cfg.get("private_key")
        self._client = None
        self._lock = asyncio.Lock()

    def _make_key(self):
        """构造 paramiko PKey：支持文件路径 / env:内容 / file内容"""
        raw = self.private_key_raw
        if not raw:
            return None
        path = os.path.expanduser(os.path.expandvars(raw))
        if os.path.isfile(path):
            return None, path  # 交给 key_filename
        content = resolve_secret(raw)
        if content.startswith("-----BEGIN"):
            import paramiko

            pkey = None
            for cls in (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key):
                try:
                    pkey = cls.from_private_key(io.StringIO(content))
                    break
                except Exception:  # noqa: BLE001
                    continue
            return pkey, None
        return None, None

    async def _ensure(self):
        if self._client is not None:
            try:
                if self._client.get_transport() is not None:
                    return
            except Exception:
                self._client = None
        if not self.password and not self.private_key_raw:
            raise RuntimeError(f"SSH {self.host} 未配置密码或私钥")
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {"username": self.username, "timeout": 15}
        if self.password:
            kwargs["password"] = self.password
        pkey, key_path = None, None
        if self.private_key_raw:
            pkey, key_path = self._make_key()
            if pkey:
                kwargs["pkey"] = pkey
            elif key_path:
                kwargs["key_filename"] = key_path
        await asyncio.to_thread(client.connect, self.host, port=self.port, **kwargs)
        self._client = client

    async def run(self, script: str, timeout: int = 30) -> str:
        async with self._lock:
            await self._ensure()
            _, stdout, stderr = await asyncio.to_thread(
                self._client.exec_command, script, timeout=timeout
            )
            out = await asyncio.to_thread(stdout.read)
            err = await asyncio.to_thread(stderr.read)
            return (out + err).decode("utf-8", "replace").strip()

    async def close(self):
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:  # noqa: BLE001
                pass
            self._client = None


class WinrmConnector(BaseConnector):
    """WinRM 远程执行（pywinrm，懒加载）"""

    type = "winrm"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.host = clean_host(cfg.get("host", ""))
        self.port = int(cfg.get("port") or 5985)
        self.username = cfg.get("username", "")
        self.password = resolve_secret(cfg.get("password", ""))
        self.transport = cfg.get("transport", "ntlm")
        self.https = bool(cfg.get("https", False))
        self._session = None

    def _ensure(self):
        if self._session is not None:
            return self._session
        import winrm

        scheme = "https" if self.https else "http"
        endpoint = f"{scheme}://{self.host}:{self.port}/wsman"
        self._session = winrm.Session(
            endpoint, auth=(self.username, self.password), transport=self.transport
        )
        return self._session

    async def run(self, script: str, timeout: int = 30) -> str:
        def _run():
            s = self._ensure()
            return s.run_ps(script)

        r = await asyncio.to_thread(_run)
        if r.status_code != 0:
            err = r.std_err.decode("utf-8", "replace")[:200]
            logger.warning(f"WinRM {self.host} 脚本返回码 {r.status_code}: {err}")
        return r.std_out.decode("utf-8", "replace").strip()


class HttpConnector(BaseConnector):
    """HTTP 检测：GET {base_url}/status 拉取远程状态 JSON（由远程服务提供）"""

    type = "http"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.token = resolve_secret(cfg.get("token", ""))

    async def fetch_status(self, timeout: int = 15) -> dict:
        import requests

        headers = {"User-Agent": "astrbot-status-sync"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        def _get():
            return requests.get(f"{self.base_url}/status", headers=headers, timeout=timeout)

        r = await asyncio.to_thread(_get)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {"raw_text": r.text[:4000]}

    async def run(self, script: str, timeout: int = 30) -> str:
        raise RuntimeError("HTTP 连接器不支持脚本执行")


def build_connector(machine: dict) -> BaseConnector | None:
    """根据机器配置构建连接器"""
    mtype = machine.get("type", "local")
    if mtype == "local":
        return LocalConnector()
    if mtype == "ssh":
        return SshConnector(machine)
    if mtype == "winrm":
        return WinrmConnector(machine)
    if mtype == "http":
        return HttpConnector(machine)
    return None
