"""快捷状态 / 在线时长统计 / 变更通知 单元测试（unittest，兼容 -m unittest discover）。

覆盖：
- 词库构建与校验（内置预设 + extra_words 扩展 + 自定义词）
- 状态写入（原子写 / 同状态连续写不重复 / 脏文件容错）
- 在线时长统计（跨天拆分 / 时分秒格式 / 占比 / 最长连续在线 / 90 天截断）
- 通知去抖与目标解析
- 命令级集成（set / words / report N / 通知推送）
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_status_sync.user_status import (  # noqa: E402
    BUILTIN_WORDS,
    DEBOUNCE_SECONDS,
    KEEP_DAYS,
    MAX_WORD_LEN,
    NotifyDebounce,
    StatusStore,
    build_words,
    daily_stats,
    fmt_duration,
    is_valid_word,
    render_report,
    split_by_day,
)


# ---------- 词库 ----------

class TestWords(unittest.TestCase):
    def test_builtin_words(self):
        self.assertIn("在线", BUILTIN_WORDS)
        self.assertIn("忙碌", BUILTIN_WORDS)
        self.assertIn("勿扰", BUILTIN_WORDS)
        self.assertIn("隐身", BUILTIN_WORDS)
        self.assertIn("离开", BUILTIN_WORDS)
        self.assertIn("自定义", BUILTIN_WORDS)

    def test_build_words_default(self):
        words = build_words(None)
        self.assertEqual(words, BUILTIN_WORDS)
        words2 = build_words([])
        self.assertEqual(words2, BUILTIN_WORDS)

    def test_build_words_extra(self):
        words = build_words(["摸鱼", "开会:会议中"])
        self.assertIn("摸鱼", words)
        self.assertEqual(words["摸鱼"], "配置扩展词")
        self.assertIn("开会", words)
        self.assertEqual(words["开会"], "会议中")
        # 内置词不被扩展覆盖语义（扩展加同词则覆盖说明）
        self.assertIn("在线", words)

    def test_build_words_dirty(self):
        words = build_words([None, " ", "", 123, "词:说明:多余"])
        self.assertIn("词", words)  # 词:说明:多余 → 词/说明:多余（partition 只切第一个冒号）
        self.assertNotIn("", words)
        self.assertNotIn(" ", words)

    def test_is_valid_word(self):
        self.assertTrue(is_valid_word("在线"))
        self.assertTrue(is_valid_word("摸鱼中"))
        self.assertFalse(is_valid_word(""))
        self.assertFalse(is_valid_word("   "))
        self.assertFalse(is_valid_word(None))
        self.assertFalse(is_valid_word("长" * (MAX_WORD_LEN + 1)))


# ---------- 状态存储 ----------

class TestStatusStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StatusStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_status_and_load(self):
        changed, prev = self.store.set_status("在线", "default:GroupMessage:1", "2026-08-17 10:00:00")
        self.assertTrue(changed)
        self.assertIsNone(prev)
        cur = self.store.load_status()
        self.assertEqual(cur["status"], "在线")
        self.assertEqual(cur["source"], "default:GroupMessage:1")
        self.assertEqual(cur["updated_at"], "2026-08-17 10:00:00")
        # 状态文件原子落盘
        self.assertTrue(os.path.exists(self.store.status_path))
        with open(self.store.status_path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "在线")

    def test_same_status_not_changed(self):
        self.store.set_status("在线", "s1", "2026-08-17 10:00:00")
        changed, prev = self.store.set_status("在线", "s2", "2026-08-17 10:05:00")
        self.assertFalse(changed)
        self.assertEqual(prev["status"], "在线")
        # 同状态不覆盖 updated_at（不产生新事件）
        cur = self.store.load_status()
        self.assertEqual(cur["updated_at"], "2026-08-17 10:00:00")

    def test_load_status_missing_file(self):
        self.assertIsNone(self.store.load_status())

    def test_load_status_dirty_file(self):
        os.makedirs(os.path.dirname(self.store.status_path), exist_ok=True)
        with open(self.store.status_path, "w", encoding="utf-8") as fh:
            fh.write("not json{{{")
        self.assertIsNone(self.store.load_status())  # 不崩溃
        with open(self.store.status_path, "w", encoding="utf-8") as fh:
            fh.write('{"status": ""}')
        self.assertIsNone(self.store.load_status())

    def test_append_and_load_events(self):
        self.store.append_event("在线", "s1", "2026-08-17 10:00:00")
        self.store.append_event("忙碌", "s2", "2026-08-17 11:00:00")
        evs = self.store.load_events()
        self.assertEqual(len(evs), 2)
        self.assertEqual([e["status"] for e in evs], ["在线", "忙碌"])
        self.assertEqual(evs[0]["source"], "s1")

    def test_append_event_dirty_ts(self):
        self.store.append_event("在线", "s1", "不是时间")  # 不崩溃、不写入
        self.assertEqual(self.store.load_events(), [])

    def test_load_events_dirty(self):
        os.makedirs(os.path.dirname(self.store.history_path), exist_ok=True)
        with open(self.store.history_path, "w", encoding="utf-8") as fh:
            json.dump({"events": [
                {"ts": "2026-08-17 10:00:00", "status": "在线", "source": "s1"},
                {"ts": "坏时间", "status": "忙碌"},          # 脏时间戳过滤
                {"ts": "2026-08-17 11:00:00", "status": ""},  # 空状态过滤
                {"ts": "2026-08-17 11:00:00", "status": "离开"},  # 正常
                "not a dict",                                # 非对象过滤
            ]}, fh)
        evs = self.store.load_events()
        self.assertEqual([e["status"] for e in evs], ["在线", "离开"])

    def test_events_trim_90_days(self):
        now = datetime.now()
        for i, days_ago in enumerate((0, 30, 91, 100)):
            ts = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
            self.store.append_event(f"状态{i}", "s", ts)
        evs = self.store.load_events()
        # 91/100 天前的被裁剪，只剩 0/30 天的
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0]["ts"].strftime("%Y-%m-%d"),
                         (now - timedelta(days=30)).strftime("%Y-%m-%d"))

    def test_keep_days_constant(self):
        self.assertEqual(KEEP_DAYS, 90)


# ---------- 时长统计 ----------

class TestDailyStats(unittest.TestCase):
    def test_basic(self):
        now = datetime(2026, 8, 17, 10, 0, 0)
        evs = [
            (datetime(2026, 8, 17, 8, 0, 0), "在线"),
            (datetime(2026, 8, 17, 9, 0, 0), "忙碌"),
        ]
        st = daily_stats(evs, days=1, now_dt=now)
        day = st["days"][0]
        self.assertEqual(day["date"], "2026-08-17")
        self.assertEqual(day["total"], 3600 + 3600)
        self.assertEqual(day["by_status"], {"在线": 3600, "忙碌": 3600})
        self.assertEqual(day["longest_online"], 3600)
        self.assertEqual(st["grand"]["total"], 7200)

    def test_cross_midnight_split(self):
        """凌晨切换：23:00 在线持续到次日 02:00 忙碌，按天拆分"""
        now = datetime(2026, 8, 17, 10, 0, 0)
        evs = [
            (datetime(2026, 8, 16, 23, 0, 0), "在线"),
            (datetime(2026, 8, 17, 2, 0, 0), "忙碌"),
        ]
        st = daily_stats(evs, days=2, now_dt=now)
        d1, d2 = st["days"]
        self.assertEqual(d1["date"], "2026-08-16")
        self.assertEqual(d1["total"], 3600)          # 23:00-24:00 在线 1h
        self.assertEqual(d1["by_status"], {"在线": 3600})
        self.assertEqual(d1["longest_online"], 3600)
        self.assertEqual(d2["date"], "2026-08-17")
        self.assertEqual(d2["by_status"], {"在线": 7200, "忙碌": 28800})  # 0-2点在线, 2-10点忙碌
        self.assertEqual(d2["longest_online"], 7200)
        self.assertEqual(d2["total"], 36000)

    def test_reboot_persist_next_day(self):
        """设备重启后无新事件：最后状态持续到现在，跨天拆分"""
        now = datetime(2026, 8, 17, 3, 0, 0)
        evs = [(datetime(2026, 8, 16, 22, 30, 0), "在线")]
        st = daily_stats(evs, days=2, now_dt=now)
        d1, d2 = st["days"]
        self.assertEqual(d1["by_status"], {"在线": 5400})   # 22:30-24:00
        self.assertEqual(d2["by_status"], {"在线": 10800})  # 00:00-03:00
        self.assertEqual(d1["longest_online"], 5400)
        self.assertEqual(d2["longest_online"], 10800)

    def test_invalid_events_ignored(self):
        now = datetime(2026, 8, 17, 10, 0, 0)
        st = daily_stats([], days=7, now_dt=now)
        self.assertEqual(len(st["days"]), 7)
        self.assertTrue(all(d["total"] == 0 for d in st["days"]))
        # 时间倒挂的脏输入不崩溃：排序后倒挂段自然消失，其余段正常统计
        evs = [
            (datetime(2026, 8, 17, 9, 0, 0), "在线"),
            (datetime(2026, 8, 17, 8, 0, 0), "忙碌"),
        ]
        st2 = daily_stats(evs, days=1, now_dt=now)
        # 排序后：忙碌 08:00-09:00 3600s，在线 09:00-10:00 3600s
        self.assertEqual(st2["days"][0]["total"], 7200)
        self.assertEqual(st2["days"][0]["by_status"], {"忙碌": 3600, "在线": 3600})

    def test_days_clamped(self):
        now = datetime(2026, 8, 17, 10, 0, 0)
        st = daily_stats([], days=999, now_dt=now)
        self.assertEqual(len(st["days"]), KEEP_DAYS)
        st2 = daily_stats([], days=0, now_dt=now)
        self.assertEqual(len(st2["days"]), 1)

    def test_report_text(self):
        now = datetime(2026, 8, 17, 10, 0, 0)
        evs = [
            (datetime(2026, 8, 17, 8, 0, 0), "在线"),
            (datetime(2026, 8, 17, 9, 0, 0), "忙碌"),
        ]
        txt = render_report(daily_stats(evs, days=1, now_dt=now), 1)
        self.assertIn("在线时长报表", txt)
        self.assertIn("总时长", txt)
        self.assertIn("50.0%", txt)   # 在线/忙碌各占一半
        self.assertIn("最长连续在线", txt)

    def test_fmt_duration(self):
        self.assertEqual(fmt_duration(3600), "1小时0分0秒")
        self.assertEqual(fmt_duration(90), "1分30秒")
        self.assertEqual(fmt_duration(5), "5秒")
        self.assertEqual(fmt_duration(0), "0秒")
        self.assertEqual(fmt_duration(-10), "0秒")
        self.assertEqual(fmt_duration(None), "0秒")
        self.assertEqual(fmt_duration("abc"), "0秒")

    def test_split_by_day(self):
        segs = split_by_day(datetime(2026, 8, 16, 23, 30), datetime(2026, 8, 17, 0, 30))
        self.assertEqual(len(segs), 2)
        self.assertEqual([s[0] for s in segs], ["2026-08-16", "2026-08-17"])
        # 同一天不拆分
        self.assertEqual(len(split_by_day(
            datetime(2026, 8, 17, 1, 0), datetime(2026, 8, 17, 2, 0))), 1)
        # 倒挂区间返回空
        self.assertEqual(split_by_day(
            datetime(2026, 8, 17, 2, 0), datetime(2026, 8, 17, 1, 0)), [])


# ---------- 通知去抖 ----------

class TestDebounce(unittest.TestCase):
    def test_same_status_within_window(self):
        db = NotifyDebounce()
        self.assertTrue(db.should_notify("在线", 60, now=1000.0))
        self.assertFalse(db.should_notify("在线", 60, now=1030.0))  # 30s 内
        self.assertTrue(db.should_notify("在线", 60, now=1061.0))  # 超窗口

    def test_diff_status_immediate(self):
        db = NotifyDebounce()
        db.should_notify("在线", 60, now=1000.0)
        self.assertTrue(db.should_notify("忙碌", 60, now=1000.5))

    def test_dirty_window(self):
        db = NotifyDebounce()
        db.should_notify("在线", "abc", now=1000.0)
        self.assertFalse(db.should_notify("在线", None, now=1000 + DEBOUNCE_SECONDS - 1))

    def test_reset(self):
        db = NotifyDebounce()
        db.should_notify("在线", 60, now=1000.0)
        db.reset()
        self.assertTrue(db.should_notify("在线", 60, now=1001.0))


# ---------- 命令集成 ----------

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


def make_plugin(cfg_extra=None, tmp_dir=None):
    """构造最小插件实例（__new__ 绕过 __init__，风格同 test_integration）"""
    from astrbot_plugin_status_sync.main import StatusSyncPlugin
    from astrbot_plugin_status_sync.monitor import Monitor

    cfg = FakeCfg(**{
        "enabled": True,
        "machines_config_file": "data/machines.json",
        "status_file": "data/status_sync.json",
        **(cfg_extra or {}),
    })
    plugin = StatusSyncPlugin.__new__(StatusSyncPlugin)
    plugin.cfg = cfg
    plugin.plugin_dir = tmp_dir if tmp_dir else _PLUGIN_DIR
    plugin.monitor = Monitor(cfg, plugin.plugin_dir)
    plugin.context = FakeContext()
    plugin._recent_sessions = []
    plugin._task = None
    return plugin


class TestStatusCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        asyncio.set_event_loop(asyncio.new_event_loop())

    def tearDown(self):
        self.tmp.cleanup()
        asyncio.set_event_loop(asyncio.new_event_loop())

    def run_cmd(self, plugin, msg):
        ev = FakeEvent(msg)
        self._loop(plugin.status_cmd(ev))
        return ev.sent_text or ""

    def _loop(self, coro):
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(coro)

    def test_set_command(self):
        plugin = make_plugin(tmp_dir=self.tmp.name)
        t = self.run_cmd(plugin, "status set 在线")
        self.assertIn("状态已切换为「在线」", t)
        # 同状态连续写：不重复
        t = self.run_cmd(plugin, "status set 在线")
        self.assertIn("状态未变化", t)
        # 自定义词
        t = self.run_cmd(plugin, "status set 摸鱼中")
        self.assertIn("状态已切换为「摸鱼中」", t)
        # 自定义形式「set 自定义 <词>」
        t = self.run_cmd(plugin, "status set 自定义 开会中")
        self.assertIn("状态已切换为「开会中」", t)
        # 非法状态词不崩溃
        t = self.run_cmd(plugin, "status set " + "长" * (MAX_WORD_LEN + 1))
        self.assertIn("无效或过长", t)
        # 无参数给出用法与当前状态
        t = self.run_cmd(plugin, "status set")
        self.assertIn("用法", t)
        self.assertIn("开会中", t)

    def test_words_command(self):
        plugin = make_plugin({"extra_words": "摸鱼:休息一下"}, tmp_dir=self.tmp.name)
        t = self.run_cmd(plugin, "status words")
        self.assertIn("快捷状态词库", t)
        self.assertIn("在线", t)
        self.assertIn("摸鱼", t)
        self.assertIn("休息一下", t)

    def test_report_command(self):
        store = StatusStore(self.tmp.name)
        # 动态日期：昨天 + 今天，避免测试随真实日期过期失效
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        store.append_event("在线", "s1", f"{yesterday} 12:00:00")
        store.append_event("忙碌", "s2", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        plugin = make_plugin(tmp_dir=self.tmp.name)
        plugin.status_store = store
        t = self.run_cmd(plugin, "status report 2")
        self.assertIn("在线时长报表（最近 2 天）", t)
        self.assertIn(datetime.now().strftime("%Y-%m-%d"), t)
        self.assertIn("最长连续在线", t)
        # 默认 7 天
        t = self.run_cmd(plugin, "status report 7")
        self.assertIn("最近 7 天", t)
        # 非法天数不崩溃（钳制范围）
        t = self.run_cmd(plugin, "status report 999")
        self.assertIn("在线时长报表", t)

    def test_notify_push(self):
        plugin = make_plugin({
            "notify_enabled": True,
            "notify_targets": "default:GroupMessage:999",
        }, tmp_dir=self.tmp.name)
        self.run_cmd(plugin, "status set 忙碌")
        self.assertEqual(len(plugin.context.sent), 1)
        umo, chain = plugin.context.sent[0]
        self.assertEqual(umo, "default:GroupMessage:999")
        text = "".join(c.text for c in chain.chain if hasattr(c, "text"))
        self.assertIn("状态变更通知", text)
        self.assertIn("忙碌", text)
        self.assertIn("来源", text)
        # 同状态连续写：不重复通知
        self.run_cmd(plugin, "status set 忙碌")
        self.assertEqual(len(plugin.context.sent), 1)

    def test_notify_disabled(self):
        plugin = make_plugin({"notify_enabled": False}, tmp_dir=self.tmp.name)
        self.run_cmd(plugin, "status set 在线")
        self.assertEqual(plugin.context.sent, [])

    def test_notify_no_targets(self):
        plugin = make_plugin({"notify_enabled": True}, tmp_dir=self.tmp.name)
        self.run_cmd(plugin, "status set 在线")
        self.assertEqual(plugin.context.sent, [])

    def test_set_logs_and_no_permission_required(self):
        """普通切换无需审批白名单，任何会话可用"""
        plugin = make_plugin({"admin_umos": ""}, tmp_dir=self.tmp.name)
        ev = FakeEvent("status set 在线", session="default:GroupMessage:666")
        self._loop(plugin.status_cmd(ev))
        self.assertIn("状态已切换", ev.sent_text or "")
        # 历史事件已记录
        evs = plugin._get_status_store().load_events()
        self.assertEqual([e["status"] for e in evs], ["在线"])
        self.assertEqual(evs[0]["source"], "default:GroupMessage:666")


if __name__ == "__main__":
    unittest.main(verbosity=2)