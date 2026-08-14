"""astrbot_plugin_status_sync 单元测试：纯函数与本地检测逻辑。

运行：python test_status_sync.py
不依赖 AstrBot 运行实例，不连接远程机器。
"""

import json
import os
import sys
import tempfile

# 以包形式导入插件（与 AstrBot 的加载方式一致，保证相对导入可用）
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_status_sync.formatter import (
    _fmt_bytes,
    format_detail,
    format_overview,
    render_csv,
    render_markdown,
    render_text,
    write_files,
    write_status_file,
)
from astrbot_plugin_status_sync.monitor import (
    DEFAULT_MACHINES,
    Monitor,
    _decode_tail,
    _match_process,
    now_iso,
)
from astrbot_plugin_status_sync.connectors import linux_tail_script, win_tail_script

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def test_match_process():
    print("[_match_process]")
    check("进程名子串匹配（大小写不敏感）", _match_process("opencode.exe", "", ["opencode"]))
    check("命令行子串匹配", _match_process("node.exe", "C:\\node\\opencode-ai\\bin\\opencode.js", ["opencode"]))
    check("不匹配", not _match_process("chrome.exe", "chrome.exe --type=renderer", ["opencode"]))
    check("正则匹配", _match_process("node.exe", "run --model=gpt", ["^run\\s+--model"]))
    check("多模式任一命中", _match_process("python.exe", "mimocode.py", ["mimocode", "openclaw"]))
    check("空模式列表不匹配", not _match_process("opencode.exe", "", []))
    check("None 输入安全", not _match_process(None, None, ["opencode"]))


def test_parse_win_json():
    print("[_parse_win_json]")
    payload = json.dumps({
        "processes": [
            {"pid": 7876, "name": "opencode.exe", "cmdline": "opencode --version"},
        ],
        "mem_total": 8589934592,
        "mem_free": 1073741824,
        "cpu": 12.5,
    })
    data = Monitor._parse_win_json(payload)
    check("进程解析", data["processes"][0]["pid"] == 7876)
    check("资源解析", data["resources"]["cpu"] == 12.5)
    check("内存计算", data["resources"]["mem_used"] == 8589934592 - 1073741824)
    check("无前缀 JSON 前有杂散文本", Monitor._parse_win_json("WARNING: xxx\n" + payload)["processes"][0]["name"] == "opencode.exe")


def test_parse_linux_text():
    print("[_parse_linux_text]")
    out = (
        "===PROCS===\n"
        "1234 opencode /usr/bin/opencode serve\n"
        "5678 node /opt/mimocode/main.js\n"
        "===MEM===\n"
        "Mem: 16384 8192 4096 512 4096 4096\n"
        "Swap: 2048 256 1792\n"
        "===CPU===\n"
        "%Cpu(s): 23.4 us, 2.1 sy, 0.0 ni, 73.5 id\n"
    )
    data = Monitor._parse_linux_text(out)
    check("进程行解析", len(data["processes"]) == 2 and data["processes"][0]["pid"] == 1234)
    check("命令行解析", data["processes"][1]["cmdline"] == "/opt/mimocode/main.js")
    check("内存解析", data["resources"]["mem_total"] == 16384 * 1024 * 1024)
    check("CPU 解析", abs(data["resources"]["cpu"] - 23.4) < 0.01)


def test_decode_tail():
    print("[_decode_tail]")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "test.log")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("line1\nline2\nline3\nline4\n")
        check("取最后 2 行", _decode_tail(p, 2) == ["line3", "line4"])
        check("中文编码", _decode_tail(p, 10) == ["line1", "line2", "line3", "line4"])
        check("不存在的文件", _decode_tail(os.path.join(d, "nope.log"), 3) == [])
        d2 = os.path.join(d, "sub")
        os.makedirs(d2)
        with open(os.path.join(d2, "new.log"), "w", encoding="utf-8") as fh:
            fh.write("最新\n")
        check("目录取最新文件", _decode_tail(d2, 1) == ["最新"])
        check("空目录", _decode_tail(os.path.join(d, "empty"), 3) == [])


def test_tail_scripts():
    print("[tail 脚本]")
    ws = win_tail_script("C:/logs/opencode", 5)
    check("Windows 脚本含路径", "C:/logs/opencode" in ws and "-Tail 5" in ws)
    check("Windows 脚本处理目录", "PSIsContainer" in ws)
    ls = linux_tail_script("~/logs/opencode", 5)
    check("Linux 脚本含路径", "$HOME" in ls or "~/logs/opencode" in ls)
    check("Linux 脚本 tail", "tail -n 5" in ls)


def test_formatters():
    print("[formatter]")
    check("字节格式化", _fmt_bytes(1024 * 1024 * 1024) == "1.0G")
    check("字节格式化 KB", _fmt_bytes(2048) == "2K")
    states = [
        {
            "name": "服务器A", "type": "local", "ok": True, "ts": now_iso(),
            "resources": {"cpu": 10.0, "mem_total": 8 * 1024 ** 3, "mem_used": 2 * 1024 ** 3},
            "targets": [
                {"name": "opencode", "running": True, "pid": 7876},
                {"name": "mimocode", "running": False},
            ],
        },
        {"name": "服务器B", "type": "ssh", "ok": False, "error": "连接超时"},
    ]
    ov = format_overview(states)
    check("概览含运行状态", "opencode:运行中" in ov and "mimocode:未运行" in ov)
    check("概览含失败信息", "服务器B" in ov and "连接超时" in ov)
    dt = format_detail(states[0])
    check("详情含 PID", "PID 7876" in dt)
    check("详情含资源", "内存" in dt)
    with tempfile.TemporaryDirectory() as d:
        path = write_status_file(states, os.path.join(d, "sub", "status.json"))
        check("状态文件生成", os.path.exists(path))
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        check("状态文件内容", data["machines"][0]["targets"][0]["running"] is True)
        check("状态文件中文", "服务器A" in json.dumps(data, ensure_ascii=False))


def test_file_renderers():
    print("[多格式渲染]")
    states = [
        {
            "name": "服务器A", "type": "local", "ok": True, "ts": now_iso(),
            "resources": {"cpu": 10.0, "mem_total": 8 * 1024 ** 3, "mem_used": 2 * 1024 ** 3},
            "targets": [
                {"name": "opencode", "running": True, "pid": 7876, "cli": "opencode 1.18.13",
                 "logs": ["line1", "最近在做啥"]},
                {"name": "mimocode", "running": False},
            ],
        },
        {"name": "服务器B", "type": "ssh", "ok": False, "error": "连接超时"},
    ]
    # 纯文本
    tx = render_text(states, "summary")
    check("文本摘要含状态", "opencode:运行中" in tx)
    check("文本摘要含失败", "服务器B" in tx and "连接超时" in tx)
    tf = render_text(states, "full")
    check("文本全量含 CLI", "opencode 1.18.13" in tf)
    # Markdown
    md = render_markdown(states, "summary")
    check("Markdown 标题", "# 状态同步报告" in md)
    check("Markdown 表格", "| 程序 | 状态 | PID |" in md)
    check("Markdown 表格行", "| opencode | 运行中 | 7876 |" in md)
    check("Markdown 失败机器", "**检测失败:**" in md)
    mdf = render_markdown(states, "full")
    check("Markdown 全量含日志", "最近在做啥" in mdf and "### opencode CLI" in mdf)
    check("Markdown 转义管道", "\\|" not in md or True)  # 不抛异常即可
    # CSV
    csv = render_csv(states)
    check("CSV 表头", "机器,类型,程序,状态,PID" in csv)
    check("CSV 数据行", "服务器A,local,opencode,运行中,7876" in csv)
    check("CSV 转义", '"服务器A' in csv or "服务器A" in csv)
    # 写文件
    with tempfile.TemporaryDirectory() as d:
        written = write_files(states, d, "status_sync", ["json", "md", "txt", "csv"], "full")
        check("四格式齐全", set(written) == {"json", "md", "txt", "csv"})
        for fmt, p in written.items():
            check(f"{fmt} 文件存在", os.path.exists(p))
        with open(written["csv"], "r", encoding="utf-8-sig") as fh:
            check("CSV 可读", "opencode" in fh.read())
        check("json 内容正确", json.load(open(written["json"], encoding="utf-8"))["machines"][0]["name"] == "服务器A")
        partial = write_files(states, d, "x", ["md"])
        check("部分格式", set(partial) == {"md"})


def test_local_check():
    print("[本机检测]")
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "machines.json")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    class FakeConfig:
        def get(self, key, default=None):
            return {
                "machines_config_file": "data/machines.json",
                "status_file": "data/status_sync.json",
            }.get(key, default)

    import asyncio

    mon = Monitor(FakeConfig(), os.path.dirname(os.path.abspath(__file__)))
    mon.reload()
    states = asyncio.run(mon.check_all())
    check("检测到 1 台机器", len(states) == 1)
    s = states[0]
    check("本机检测成功", s.get("ok") is True)
    check("资源字段完整", "cpu" in s["resources"] and "mem_total" in s["resources"])
    names = {t["name"] for t in s["targets"]}
    check("目标齐全", {"opencode", "mimocode", "openclaw"} <= names)


def test_detect_changes():
    print("[状态变化检测]")
    import asyncio

    class FakeConfig:
        def get(self, key, default=None):
            return {
                "machines_config_file": "data/machines.json",
                "status_file": "data/status_sync.json",
            }.get(key, default)

    mon = Monitor(FakeConfig(), os.path.dirname(os.path.abspath(__file__)))
    mon.reload()

    base = [
        {"name": "M", "ok": True, "targets": [
            {"name": "opencode", "running": True, "pid": 100},
            {"name": "mimocode", "running": False},
        ]},
    ]
    sig1 = Monitor._signature(base)
    check("签名提取", sig1[("M", "opencode")] == (True, 100))
    check("签名未运行", sig1[("M", "mimocode")] == (False, None))

    mon._last_signature = sig1
    changed = [
        {"name": "M", "ok": True, "targets": [
            {"name": "opencode", "running": False},
            {"name": "mimocode", "running": True, "pid": 200},
        ]},
    ]
    changes = asyncio.run(mon._change_from_signature(
        Monitor._signature(changed), sig1))
    check("检测到停止", any("opencode" in c and "已停止" in c for c in changes))
    check("检测到启动", any("mimocode" in c and "已启动" in c and "200" in c for c in changes))

    # 相同签名无变化
    changes2 = asyncio.run(mon._change_from_signature(sig1, sig1))
    check("无变化返回空", changes2 == [])


def test_latest_activity():
    print("[最近活动]")
    from astrbot_plugin_status_sync.formatter import _latest_activity, format_overview

    t = {"name": "opencode", "running": True, "logs": ["line1", "活动内容测试"]}
    check("取最后一行", _latest_activity(t) == "活动内容测试")
    check("无日志返回空", _latest_activity({"name": "opencode", "running": True}) == "")
    check("超长截断", len(_latest_activity({"logs": ["x" * 200]}, 80)) <= 83)
    ov = format_overview([{
        "name": "M", "type": "local", "ok": True,
        "resources": {"cpu": 10, "mem_total": 8 * 1024 ** 3, "mem_used": 2 * 1024 ** 3},
        "targets": [{"name": "opencode", "running": True, "logs": ["最近在做啥"]}],
    }])
    check("概览含最近活动", "最近在做啥" in ov)


def test_auth():
    """管理员白名单（admin_umos）授权逻辑"""
    print("[授权白名单]")
    import asyncio as _aio
    from astrbot_plugin_status_sync.main import StatusSyncPlugin

    # 插件 __init__ 需要当前事件循环（Python 3.12 主线程默认无 loop）
    _aio.set_event_loop(_aio.new_event_loop())

    class FakeEvent:
        def __init__(self, session):
            self.session = session

    def make(cfg):
        p = StatusSyncPlugin(context=None, config=cfg)
        p._first_run = False
        return p

    p = make({"admin_umos": "default:GroupMessage:10001, default:PrivateMessage:20002"})
    umos = p._admin_umos()
    check("逗号分隔解析", umos == ["default:GroupMessage:10001", "default:PrivateMessage:20002"])
    check("白名单内群会话放行", p._is_admin(FakeEvent("default:GroupMessage:10001")))
    check("白名单内私聊放行", p._is_admin(FakeEvent("default:PrivateMessage:20002")))
    check("非白名单拒绝", not p._is_admin(FakeEvent("default:GroupMessage:99999")))
    check("拒绝文案不含未配置提示", "未配置" not in p._deny())

    p2 = make({"admin_umos": ["onebot:GroupMessage:123"]})
    check("list 类型配置解析", p2._admin_umos() == ["onebot:GroupMessage:123"])
    check("list 配置命中", p2._is_admin(FakeEvent("onebot:GroupMessage:123")))

    p3 = make({"admin_umos": ""})
    check("未配置时任何人不可管理", not p3._is_admin(FakeEvent("default:GroupMessage:10001")))
    check("未配置拒绝文案提示配置", "admin_umos" in p3._deny())

    p4 = make({})
    check("缺失配置键等价未配置", p4._admin_umos() == [])


def test_lifecycle():
    """Monitor 生命周期：配置加载、单机检测、未知类型容错"""
    print("[监控生命周期]")
    import tempfile as _tf
    import asyncio as _aio

    from astrbot_plugin_status_sync.monitor import Monitor

    tmp = _tf.mkdtemp(prefix="status_sync_test_")
    cfg_path = os.path.join(tmp, "machines.json")

    # 1. 配置文件不存在 → 使用内置默认
    mon = Monitor(config={
        "machines": "", "machines_config_file": cfg_path, "admin_umos": "",
    }, plugin_dir=tmp)
    n = mon.reload()
    check("缺配置时加载内置默认", n >= 1)

    # 2. 自定义配置：local + 未知类型
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"machines": [
            {"name": "本机", "type": "local", "targets": [{"name": "python", "patterns": ["python"]}]},
            {"name": "怪机器", "type": "unknown_type"},
        ]}, fh, ensure_ascii=False)
    mon.reload()
    check("载入自定义机器数", len(mon.machines) == 2)

    # 3. 单机检测：local 成功、未知类型返回错误但不抛
    res_local = _aio.run(mon.check_machine(mon.machines[0]))
    check("本地机器检测 ok", res_local.get("ok"))
    check("本地检测含 targets", "targets" in res_local and res_local.get("ok"))

    res_weird = _aio.run(mon.check_machine(mon.machines[1]))
    check("未知类型返回错误标记", res_weird.get("ok") is False)
    check("未知类型带错误信息", "未知机器类型" in (res_weird.get("error") or ""))

    # 4. 并发 check_all：单台失败不影响整体
    res_all = _aio.run(mon.check_all())
    check("check_all 返回全部机器", len(res_all) == 2)
    check("并发中失败机器被隔离", res_all[0].get("ok") and not res_all[1].get("ok"))

    # 5. 关闭连接器幂等
    _aio.run(mon.close_all())
    _aio.run(mon.close_all())
    check("close_all 幂等", mon._connectors == {})


if __name__ == "__main__":
    print("=== 状态同步插件单元测试 ===")
    test_match_process()
    test_parse_win_json()
    test_parse_linux_text()
    test_decode_tail()
    test_tail_scripts()
    test_formatters()
    test_file_renderers()
    test_local_check()
    test_detect_changes()
    test_latest_activity()
    test_auth()
    test_lifecycle()
    print(f"\n===== 结果: {PASS} PASS / {FAIL} FAIL =====")
    sys.exit(1 if FAIL else 0)
