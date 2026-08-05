"""权限远程控制单元测试：日志解析、增量读取、规则管理、SSE 解析。

运行：python test_perm.py
不依赖 AstrBot 运行实例，不连接 opencode serve。
"""

import json
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_status_sync.monitor import Monitor
from astrbot_plugin_status_sync.perm import (
    PermissionMonitor,
    RuleManager,
    _tail_new_lines,
    parse_eval_line,
    parse_sse_event,
    strip_jsonc_comments,
)

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


def test_parse_eval_line():
    print("[日志行解析]")
    line = (
        'timestamp=2026-08-05T11:28:20.456Z level=INFO run=78077fdc '
        'message=evaluated permission=bash pattern="Select-Object -Last 4" '
        'action.permission=* action.action=allow action.pattern=*'
    )
    ev = parse_eval_line(line, source="opencode.log")
    check("解析成功", ev is not None)
    check("工具名", ev.tool == "bash")
    check("命令内容", ev.pattern == "Select-Object -Last 4")
    check("动作", ev.action == "allow")
    check("来源", ev.source == "opencode.log")
    check("时间戳", ev.ts == "2026-08-05T11:28:20.456Z")
    check("无护栏行不解析", parse_eval_line("some random log line", "") is None)
    check("非 evaluated 不解析",
          parse_eval_line(
              'timestamp=x level=INFO run=r message=hello world', "") is None)


def test_tail_new_lines():
    print("[增量读取]")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.log")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("line1\nline2\n")
        lines, off = _tail_new_lines(p, 0)
        check("首次读全部", lines == ["line1", "line2"] and off > 0)
        lines, off2 = _tail_new_lines(p, off)
        check("无新增返回空", lines == [] and off2 == off)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("line3\n")
        lines, off3 = _tail_new_lines(p, off)
        check("增量读取新行", lines == ["line3"])
        # 轮转：文件变小，从头读
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("new\n")
        lines, _ = _tail_new_lines(p, off3)
        check("轮转后从头读", lines == ["new"])
        # 不存在文件
        lines, off = _tail_new_lines(os.path.join(d, "nope.log"), 99)
        check("不存在文件安全", lines == [] and off == 99)


def test_strip_jsonc():
    print("[JSONC 剥离]")
    text = '''{
  // 权限注释
  "permission": { "bash": "allow" /* 内联 */ },
  "desc": "//不是注释",
  "cmd": "C:/a//b"
}'''
    stripped = strip_jsonc_comments(text)
    data = json.loads(stripped)
    check("行注释剥离", data["permission"]["bash"] == "allow")
    check("字符串内斜杠保留", data["cmd"] == "C:/a//b")
    check("字符串内 // 保留", data["desc"] == "//不是注释")


def test_rule_manager():
    print("[规则管理]")
    with tempfile.TemporaryDirectory() as d:
        cf = os.path.join(d, "opencode.jsonc")
        rm = RuleManager(cf)
        check("空配置无规则", "无自定义规则" in rm.format_rules())
        # 设置规则
        r = rm.set_rule("bash", "git push *", "allow")
        check("设置成功", "已设置规则" in r)
        check("规则文件生成", os.path.exists(cf))
        check("规则写入", rm.format_rules().count("→ allow") == 1)
        # 再设 deny，同工具变对象形式
        rm.set_rule("bash", "rm *", "deny")
        txt = rm.format_rules()
        check("对象形式保留两条", txt.count("→") == 2 and "git push *" in txt and "rm *" in txt)
        # "*" 模式设置全局
        rm.set_rule("edit", "*", "ask")
        check("全局模式", "edit 「*」→ ask" in rm.format_rules())
        # 全局工具 *
        rm.set_rule("*", "external_directory", "allow")
        check("全局工具规则", "* 「external_directory」→ allow" in rm.format_rules())
        # 移除
        r = rm.remove_rule("bash", "rm *")
        check("删除规则", "已删除规则" in r and "rm *" not in rm.format_rules())
        r = rm.remove_rule("bash", "不存在")
        check("删除不存在", "规则不存在" in r)
        # 清空
        r = rm.clear_rules()
        check("清空规则", "已清空" in r)
        check("清空后无规则", "无自定义规则" in rm.format_rules())
        # 已存在的配置保持其他字段
        existing = {
            "model": "deepseek/deepseek-v4",
            "permission": "allow",
        }
        with open(cf, "w", encoding="utf-8") as fh:
            json.dump(existing, fh)
        rm.set_rule("bash", "git *", "deny")
        data = rm.load()
        check("保留其他字段", data["model"] == "deepseek/deepseek-v4")
        check("全局字符串转对象", isinstance(data["permission"]["bash"], dict)
              and data["permission"]["bash"]["git *"] == "deny")
        check("原全局值保留", data["permission"]["*"] == "allow")


def test_monitor_cursor():
    print("[监控游标]")
    import asyncio

    class FakeCfg:
        def get(self, key, default=None):
            return {"permission_event_buffer": 30}.get(key, default)

    class FakeMonitor:
        machines = []

    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "opencode.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(
                'timestamp=t1 level=INFO run=r message=evaluated '
                'permission=bash pattern="cmd1" action.permission=* '
                'action.action=deny action.pattern=*\n'
            )
        mon = PermissionMonitor(FakeCfg(), d)
        # 构造一个返回该路径的机器配置
        FakeMonitor.machines = [{
            "name": "M", "targets": [{
                "name": "opencode",
                "logs": [{"path": log, "lines": 5}],
            }],
        }]
        evs = mon.poll(FakeMonitor.machines)
        check("首次 poll 出事件", len(evs) == 1 and evs[0].action == "deny")
        check("游标文件生成", os.path.exists(mon._cursor_path()))
        evs2 = mon.poll(FakeMonitor.machines)
        check("重复 poll 无事件", evs2 == [])
        # 新行
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(
                'timestamp=t2 level=INFO run=r message=evaluated '
                'permission=edit pattern="app.py" action.permission=* '
                'action.action=ask action.pattern=*\n'
            )
        evs3 = mon.poll(FakeMonitor.machines)
        check("增量出新事件", len(evs3) == 1 and evs3[0].action == "ask")
        check("recent 缓冲", mon.recent(5)[0].pattern == "app.py")
        # 目录取最新文件
        sub = os.path.join(d, "logs")
        os.makedirs(sub)
        p1 = os.path.join(sub, "old.log")
        p2 = os.path.join(sub, "new.log")
        for p in (p1, p2):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("junk\n")
        base = 1000000000
        os.utime(p1, (base, base))
        os.utime(p2, (base + 10, base + 10))
        FakeMonitor.machines = [{
            "name": "M", "targets": [{
                "name": "opencode",
                "logs": [{"path": sub, "lines": 5}],
            }],
        }]
        paths = mon.log_paths(FakeMonitor.machines)
        check("目录取最新文件", len(paths) == 1 and paths[0] == p2)


def test_parse_sse():
    print("[SSE 解析]")
    req = parse_sse_event(
        "permission.request",
        json.dumps({
            "sessionID": "ses_123", "permissionID": "pid_abc",
            "toolName": "bash", "description": "run git push",
        }),
    )
    check("权限请求解析", req is not None)
    check("会话ID", req.session_id == "ses_123")
    check("权限ID", req.permission_id == "pid_abc")
    check("工具", req.tool == "bash")
    check("内容", req.detail == "run git push")
    check("非权限事件忽略", parse_sse_event("message.updated", json.dumps({"type": "message.updated"})) is None)
    check("损坏 JSON 忽略", parse_sse_event("permission.request", "not json") is None)
    # data.type 形式
    req2 = parse_sse_event("", json.dumps({
        "type": "permission.request", "session_id": "s", "permission_id": "p",
        "pattern": "git *",
    }))
    check("data.type 形式", req2 is not None and req2.tool == "未知工具" and req2.detail == "git *")


def test_secret():
    print("[凭据安全]")
    import base64

    from astrbot_plugin_status_sync.secret import (
        clean_host,
        dpapi_decrypt,
        dpapi_encrypt,
        is_windows,
        resolve_secret,
    )

    # 前缀解析
    check("env 引用", resolve_secret("env:MY_PWD", {"MY_PWD": "s3cret"}) == "s3cret")
    check("env 未设置返回空", resolve_secret("env:NOT_SET", {}) == "")
    check("普通值原样", resolve_secret("hello") == "hello")
    check("base64 解码", resolve_secret("base64:c2VjcmV0") == "secret")
    check("空值", resolve_secret("") == "" and resolve_secret(None) == "")
    with tempfile.TemporaryDirectory() as d:
        fp = os.path.join(d, "key.pem")
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write("KEY-CONTENT\n")
        check("file 引用", resolve_secret("file:" + fp) == "KEY-CONTENT")

    # DPAPI 加解密（当前 Windows 环境）
    if is_windows():
        b64 = dpapi_encrypt("my-dpapi-secret")
        check("DPAPI 加密输出", len(b64) > 10)
        check("DPAPI 解密还原", dpapi_decrypt(b64) == "my-dpapi-secret")
        check("dpapi 前缀解析", resolve_secret("dpapi:" + b64) == "my-dpapi-secret")
    else:
        print("  跳过 DPAPI（非 Windows）")

    # host 清洗（支持域名）
    check("IP 原样", clean_host("192.168.1.10") == "192.168.1.10")
    check("域名支持", clean_host("myserver.example.com") == "myserver.example.com")
    check("剥 ssh scheme", clean_host("ssh://192.168.1.10") == "192.168.1.10")
    check("剥 http scheme", clean_host("https://node1.lan") == "node1.lan")
    check("剥端口路径", clean_host("mypc.lan:22") == "mypc.lan:22")
    check("剥斜杠路径", clean_host("myhost.lan/extra") == "myhost.lan")
    check("空 host", clean_host("") == "")

    # 连接器应用（token/密码解析发生在构造时）
    from astrbot_plugin_status_sync.connectors import SshConnector

    sc = SshConnector({"host": "ssh://git@a.lan", "username": "root",
                       "password": "", "private_key": None})
    check("SSH host 剥离", sc.host == "git@a.lan")


if __name__ == "__main__":
    print("=== 权限远程控制单元测试 ===")
    test_parse_eval_line()
    test_tail_new_lines()
    test_strip_jsonc()
    test_rule_manager()
    test_monitor_cursor()
    test_parse_sse()
    test_secret()
    print(f"\n===== 结果: {PASS} PASS / {FAIL} FAIL =====")
    sys.exit(1 if FAIL else 0)