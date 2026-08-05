"""凭据安全：敏感字段解析与 Windows DPAPI 加解密。

避免把 SSH 密码 / WinRM 密码 / HTTP token 明文写进 data/machines.json。
字段支持前缀：
- `env:VAR_NAME`   -> 从环境变量读取
- `file:路径`      -> 从外部文件读取（文件可放在任意识别安全的目录）
- `dpapi:一串base64` -> Windows DPAPI 密文（仅当前 Windows 用户可解密）
- `base64:...`     -> base64 编码的明文（弱混淆，仅防"扫一眼"）
- 其他             -> 原样（兼容旧配置，也支持私钥文件路径）

Windows 上提供 `dpapi_encrypt / dpapi_decrypt`，可用
`/机器状态 加密串 <明文>` 把密码就地加密成 dpapi 密文。
"""

import base64
import ctypes
import os
import sys
from ctypes import POINTER, Structure, byref, c_ubyte, c_ulong, cast, create_string_buffer

try:
    import machine
except ImportError:
    pass


class DATA_BLOB(Structure):
    _fields_ = [("cbData", c_ulong), ("pbData", POINTER(c_ubyte))]


def _blob_to_bytes(blob: DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def is_windows() -> bool:
    return sys.platform.startswith("win")


def dpapi_encrypt(plain: str) -> str:
    """Windows DPAPI 加密（当前用户作用域），返回 base64 字符串。
    非 Windows 平台返回原样（无法加密）。"""
    if not is_windows():
        raise RuntimeError("DPAPI 仅支持 Windows（本机环境）")
    raw = plain.encode("utf-8")
    buf = create_string_buffer(raw)
    data_in = DATA_BLOB(c_ulong(len(raw)), cast(buf, POINTER(c_ubyte)))
    data_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        byref(data_in), None, None, None, None, 0, byref(data_out)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return base64.b64encode(_blob_to_bytes(data_out)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


def dpapi_decrypt(b64: str) -> str:
    """解密 dpapi_encrypt 的结果。不同用户 / 非本机无法解密。"""
    if not is_windows():
        raise RuntimeError("DPAPI 仅支持 Windows（本机环境）")
    raw = base64.b64decode(b64)
    buf = create_string_buffer(raw)
    data_in = DATA_BLOB(c_ulong(len(raw)), cast(buf, POINTER(c_ubyte)))
    data_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        byref(data_in), None, None, None, None, 0, byref(data_out)
    )
    if not ok:
        raise RuntimeError("DPAPI 解密失败：可能不是本机/当前用户加密")
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.pbData)


def resolve_secret(value, env=None) -> str:
    """按前缀解析敏感字段值，明文返回。"""
    if not isinstance(value, str) or not value:
        return value or ""
    if value.startswith("env:"):
        name = value[4:]
        return (env if env is not None else os.environ).get(name, "")
    if value.startswith("file:"):
        p = os.path.expanduser(os.path.expandvars(value[5:]))
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    if value.startswith("dpapi:"):
        try:
            return dpapi_decrypt(value[6:])
        except Exception:  # noqa: BLE001
            return ""
    if value.startswith("base64:"):
        try:
            return base64.b64decode(value[7:]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    return value


def clean_host(host: str) -> str:
    """清洗 host 字段：剥掉 scheme 前缀与路径，支持 IP 与域名。"""
    h = (host or "").strip()
    if not h:
        return h
    lowered = h.lower()
    for scheme in ("ssh://", "http://", "https://", "winrm://"):
        if lowered.startswith(scheme):
            h = h[len(scheme):]
            break
    if "/" in h:
        h = h.split("/", 1)[0]
    return h.strip()


def lock_machines_file(path: str) -> None:
    """收紧机器配置文件的 Windows ACL：仅当前用户与系统可读。"""
    if not is_windows() or not os.path.exists(path):
        return
    try:
        import subprocess

        subprocess.run(
            [
                "icacls",
                path,
                "/inheritance:r",
                "/grant:r",
                f"{os.getlogin()}:R",
                "SYSTEM:R",
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass  # 权限收紧失败不应影响功能，仅提示性尝试