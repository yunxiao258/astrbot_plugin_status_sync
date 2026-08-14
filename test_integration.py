"""astrbot_plugin_status_sync 集成测试：命令分发、错误分支、播报逻辑。

运行：python test_integration.py
需要 venv 中的 astrbot 包（@register 装饰器依赖），不连接远程机器。
"""

import json
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_status_sync.main import StatusSyncPlugin
from astrbot_plugin_status_sync.monitor import Monitor
from astrbot_plugin_status_sync.perm import PermissionMonitor, RuleManager

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


class FakeCfg:
    def __init__(self, **kw):
        self.kw = kw

    def get(self, key, default=None):
        return self.kw.get(key, default)


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


class FakeEvent:
    def __init__(self, message_str, session="default:GroupMessage:1234567890"):
        self.message_str = message_str
        self._session = session
        self.sent_text = None

    @property
    def session(self):
        class _S:
            def __init__(self, v):
                self.v = v

            def __str__(self):
                return self.v

        return _S(self._session)

    async def send(self, chain):
        texts = [c.text for c in chain.chain if hasattr(c, "text")]
        self.sent_text = "".join(texts)


def make_plugin(cfg_extra=None):
    cfg = FakeCfg(**{
        "machines_config_file": "data/machines.json",
        "status_file": "data/status_sync.json",
        "enabled": True,
        "poll_interval_minutes": 30,
        "report_enabled": True,
        "report_groups": "",
        "file_formats": "json,md,txt,csv",
        "file_delivery": "text,file,local",
        "file_detail": "summary",
        "file_generate_timing": "manual,scheduled,on_change",
        **(cfg_extra or {}),
    })
    plugin = StatusSyncPlugin.__new__(StatusSyncPlugin)
    plugin.cfg = cfg
    plugin.plugin_dir = _PLUGIN_DIR
    plugin.monitor = Monitor(cfg, _PLUGIN_DIR)
    plugin.monitor.reload()
    plugin.context = FakeContext()
    plugin._recent_sessions = []
    plugin._task = None
    return plugin


def test_error_branches():
    print("[错误分支]")
    cfg = FakeCfg(machines_config_file="data/machines.json", status_file="data/status_sync.json")
    mon = Monitor(cfg, _PLUGIN_DIR)

    mon.machines = [{"name": "坏类型", "type": "alien", "targets": []}]
    import asyncio

    st = asyncio.run(mon.check_all())[0]
    check("未知类型机器返回错误状态", st.get("ok") is False and "未知" in st.get("error", ""))

    mon.machines = [{
        "name": "无凭据SSH", "type": "ssh", "os": "linux",
        "host": "192.168.1.1", "username": "root",
        "targets": [{"name": "opencode", "processes": ["opencode"]}],
    }]
    st = asyncio.run(mon.check_all())[0]
    check("SSH 无凭据不崩溃且报错", st.get("ok") is False and "密码" in st.get("error", ""))


def test_command_dispatch():
    print("[命令分发]")
    import asyncio

    async def run_cmd(plugin, msg):
        ev = FakeEvent(msg)
        await plugin.status_cmd(ev)
        return ev.sent_text or ""

    # 概览（两种命令形式）
    plugin = make_plugin()
    t = asyncio.run(run_cmd(plugin, "机器状态"))
    check("裸命令返回概览", "机器状态概览" in t and "本机服务器" in t)
    check("命令后记住会话", plugin._recent_sessions == ["default:GroupMessage:1234567890"])

    t = asyncio.run(run_cmd(plugin, "/机器状态"))
    check("带斜杠命令返回概览", "机器状态概览" in t)
    t = asyncio.run(run_cmd(plugin, "sync"))
    check("sync 别名返回概览", "机器状态概览" in t)

    # 指定机器详情
    t = asyncio.run(run_cmd(plugin, "机器状态 本机服务器"))
    check("机器详情", "本机服务器" in t and "opencode" in t)

    # 不存在的机器
    t = asyncio.run(run_cmd(plugin, "机器状态 不存在的机器"))
    check("未找到机器提示", "未找到机器" in t)

    # reload（管理命令，需 admin_umos）
    plugin = make_plugin({"admin_umos": "default:GroupMessage:1234567890"})
    t = asyncio.run(run_cmd(plugin, "机器状态 reload"))
    check("reload 提示", "已重新加载" in t)

    # 无白名单时管理命令被拒，只读命令仍可用
    plugin = make_plugin({})
    t = asyncio.run(run_cmd(plugin, "机器状态 reload"))
    check("无白名单拒绝 reload", "未配置管理员白名单" in t)
    t = asyncio.run(run_cmd(plugin, "机器状态 加密串 密码"))
    check("无白名单拒绝加密串", "未配置管理员白名单" in t)
    t = asyncio.run(run_cmd(plugin, "机器状态"))
    check("无白名单仍可查看概览", "机器状态概览" in t)

    # 非白名单会话（不同 UMO）被拒
    plugin = make_plugin({"admin_umos": "default:GroupMessage:9999999999"})
    t = asyncio.run(run_cmd(plugin, "机器状态 reload"))
    check("非白名单会话拒绝 reload", "没有执行此命令的权限" in t)
    t = asyncio.run(run_cmd(plugin, "机器状态 本机服务器"))
    check("非白名单会话仍可查详情", "本机服务器" in t and "opencode" in t)

    # report：播报到配置的群（管理命令）
    plugin = make_plugin({
        "report_groups": "default:GroupMessage:1234567890",
        "admin_umos": "default:GroupMessage:1234567890",
    })
    t = asyncio.run(run_cmd(plugin, "机器状态 report"))
    check("report 播报成功", t == "已播报状态报告")
    check("播报发送到目标群", len(plugin.context.sent) == 1
          and plugin.context.sent[0][0] == "default:GroupMessage:1234567890")

    # file：生成状态文件并按配置送达（管理命令）
    with tempfile.TemporaryDirectory() as d:
        plugin = make_plugin({
            "status_file": os.path.join(d, "out", "status.json"),
            "report_groups": "default:GroupMessage:1234567890",
            "admin_umos": "default:GroupMessage:1234567890",
        })
        t = asyncio.run(run_cmd(plugin, "机器状态 file"))
        check("状态文件生成提示", "状态文件已生成" in t and "文件(json)" in t)
        check("状态文件存在", os.path.exists(os.path.join(d, "out", "status.json")))
        text_msgs = [c for (_, c) in plugin.context.sent
                     if all(hasattr(x, "text") for x in c.chain)]
        file_msgs = [c for (_, c) in plugin.context.sent
                     if any(hasattr(x, "file") for x in c.chain)]
        check("文本消息送达", len(text_msgs) == 1 and "机器状态概览" in text_msgs[0].chain[0].text)
        check("文件消息送达", len(file_msgs) == 4)
        check("本地文件落盘", os.path.exists(os.path.join(d, "out", "status.md")))

    # file 带参数：仅 md + 全量（管理命令）
    with tempfile.TemporaryDirectory() as d:
        plugin = make_plugin({
            "status_file": os.path.join(d, "out", "status.json"),
            "report_groups": "default:GroupMessage:1234567890",
            "admin_umos": "default:GroupMessage:1234567890",
        })
        t = asyncio.run(run_cmd(plugin, "机器状态 file md full"))
        check("指定格式提示", "文件(md)" in t and "文件(json)" not in t)
        with open(os.path.join(d, "out", "status.md"), "r", encoding="utf-8") as fh:
            md = fh.read()
        check("全量 Markdown 含 CLI", "CLI" in md and "### opencode" in md)


def test_file_delivery():
    print("[文件送达方式]")
    import asyncio

    async def run_cmd(plugin, msg):
        ev = FakeEvent(msg)
        await plugin.status_cmd(ev)
        return ev.sent_text or ""

    # 仅本地保存：不发送任何消息
    with tempfile.TemporaryDirectory() as d:
        plugin = make_plugin({
            "status_file": os.path.join(d, "out", "status.json"),
            "file_delivery": "local",
            "file_formats": "md",
            "report_groups": "default:GroupMessage:1234567890",
            "admin_umos": "default:GroupMessage:1234567890",
        })
        t = asyncio.run(run_cmd(plugin, "机器状态 file"))
        check("仅本地提示", "已存本地" in t and "文件(" not in t)
        check("仅本地不发消息", plugin.context.sent == [])

    # 仅文本消息：不生成文件
    plugin = make_plugin({
        "file_delivery": "text",
        "report_groups": "default:GroupMessage:1234567890",
        "admin_umos": "default:GroupMessage:1234567890",
    })
    t = asyncio.run(run_cmd(plugin, "机器状态 file"))
    check("仅文本提示", "文本" in t and "已存本地" not in t)
    check("仅文本发一条消息", len(plugin.context.sent) == 1)

    # 关闭文件格式：提示未生成文件
    plugin = make_plugin({
        "file_formats": "",
        "file_delivery": "file,local",
        "report_groups": "default:GroupMessage:1234567890",
        "admin_umos": "default:GroupMessage:1234567890",
    })
    t = asyncio.run(run_cmd(plugin, "机器状态 file"))
    check("无格式提示", "未生成文件" in t)

    # full 粒度文本送达（full 内容在播报消息中）
    plugin = make_plugin({
        "file_delivery": "text",
        "file_detail": "full",
        "report_groups": "default:GroupMessage:1234567890",
        "admin_umos": "default:GroupMessage:1234567890",
    })
    asyncio.run(run_cmd(plugin, "机器状态 file"))
    gaps = [c for (_, c) in plugin.context.sent]
    full_text = "".join(x.text for x in gaps[0].chain if hasattr(x, "text"))
    check("full 文本含 CLI", "CLI" in full_text)


def make_perm_plugin(tmp_dir):
    """构造带权限管理能力的插件（注入 perm_rules/perm_monitor/serve）"""
    import asyncio

    plugin = make_plugin({
        "opencode_config_file": os.path.join(tmp_dir, "opencode.jsonc"),
        "permission_report": True,
        "permission_forward_level": "deny,ask",
        "permission_check_interval_seconds": 30,
        "permission_event_buffer": 30,
    })
    plugin.perm_rules = RuleManager(os.path.join(tmp_dir, "opencode.jsonc"))
    plugin.perm_monitor = PermissionMonitor(plugin.cfg, _PLUGIN_DIR)
    plugin.serve = None
    plugin._loop = asyncio.new_event_loop()
    return plugin


def test_permission_commands():
    print("[权限命令]")
    import asyncio

    async def run_cmd(plugin, msg):
        ev = FakeEvent(msg)
        await plugin.status_cmd(ev)
        return ev.sent_text or ""

    with tempfile.TemporaryDirectory() as d:
        plugin = make_perm_plugin(d)
        # 未配置白名单时管理命令被拒绝
        t = asyncio.run(run_cmd(plugin, "机器状态 权限允许 bash git push *"))
        check("无白名单时拒绝管理命令", "未配置管理员白名单" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 权限"))
        check("无白名单时拒绝权限查询", "未配置管理员白名单" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 reload"))
        check("无白名单时拒绝 reload", "未配置管理员白名单" in t)

        # 配置白名单后：非白名单会话仍被拒绝
        plugin = make_perm_plugin(d)
        plugin.cfg.kw["admin_umos"] = "default:GroupMessage:8888888888"
        t = asyncio.run(run_cmd(plugin, "机器状态 权限允许 bash git push *"))
        check("非白名单会话被拒绝", "没有执行此命令的权限" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态"))
        check("只读查询对普通成员开放", "机器状态概览" in t)

        # 白名单会话（FakeEvent 默认 default:GroupMessage:1234567890）
        plugin = make_perm_plugin(d)
        plugin.cfg.kw["admin_umos"] = "default:GroupMessage:1234567890"
        t = asyncio.run(run_cmd(plugin, "机器状态 权限"))
        check("权限无规则提示", "无自定义规则" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 权限允许 bash git push *"))
        check("远程设置允许", "已设置规则" in t and "allow" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 权限"))
        check("规则写入生效", "git push *" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 权限拒绝 bash rm *"))
        check("远程设置拒绝", "已设置规则" in t and "deny" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 权限删除 bash git push *"))
        check("删除规则", "已删除规则" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 权限询问 edit *"))
        check("设为询问", "已设置规则" in t and "ask" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 权限事件"))
        check("权限事件空", "暂无权限事件记录" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 同意 abc"))
        check("未启用 serve 提示", "未启用 opencode serve" in t)
        t = asyncio.run(run_cmd(plugin, "机器状态 权限清空"))
        check("清空权限", "已清空" in t)

        # 配置文件实际落盘
        with open(os.path.join(d, "opencode.jsonc"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        check("配置写入磁盘", data.get("permission") == {} or "permission" not in data)


def test_encrypt_command():
    print("[加密串命令]")
    import asyncio

    from astrbot_plugin_status_sync.secret import is_windows

    async def run_cmd(plugin, msg):
        ev = FakeEvent(msg)
        await plugin.status_cmd(ev)
        return ev.sent_text or ""

    plugin = make_plugin({"admin_umos": "default:GroupMessage:1234567890"})
    t = asyncio.run(run_cmd(plugin, "机器状态 加密串 我的密码"))
    check("加密命令有输出", "dpapi:" in t or "仅支持 Windows" in t)
    if "dpapi:" in t:
        from astrbot_plugin_status_sync.secret import dpapi_decrypt

        b64 = t.split("dpapi:")[1].split("\n")[0].strip()
        check("加密串可解密还原", dpapi_decrypt(b64) == "我的密码")


def test_report_targets():
    print("[播报目标]")
    import asyncio

    plugin = make_plugin({"report_groups": ""})
    async def remember(msg):
        await plugin._remember(FakeEvent(msg, session=str(i)))

    for i in range(6):
        asyncio.run(remember(f"status-{i}"))
    check("最多保留 5 个最近会话", len(plugin._recent_sessions) == 5)
    check("最新会话在最前", plugin._recent_sessions[0] == "5")
    check("播报目标取最近会话", plugin._report_targets() == plugin._recent_sessions)

    plugin2 = make_plugin({"report_groups": "x:1, y:2"})
    check("配置的播报目标优先", plugin2._report_targets() == ["x:1", "y:2"])


def test_disabled():
    print("[插件禁用]")
    import asyncio

    plugin = make_plugin({"enabled": False})
    ev = FakeEvent("机器状态")
    asyncio.run(plugin.status_cmd(ev))
    check("禁用时不响应", ev.sent_text is None)


if __name__ == "__main__":
    print("=== 状态同步插件集成测试 ===")
    test_error_branches()
    test_command_dispatch()
    test_file_delivery()
    test_permission_commands()
    test_encrypt_command()
    test_report_targets()
    test_disabled()
    print(f"\n===== 结果: {PASS} PASS / {FAIL} FAIL =====")
    sys.exit(1 if FAIL else 0)

