"""用户快捷状态：状态词库、状态存储、在线时长统计与变更通知去抖。

功能：
- 快捷状态词库：内置预设词（在线/忙碌/勿扰/隐身/离开/自定义）+ 配置扩展词
- 状态写入：独立 JSON 文件（原子写），与机器运行状态文件互不干扰
- 在线时长统计：记录每次状态切换时间戳，按天拆分统计各状态持续时长
- 变更通知去抖：同状态在窗口内只通知一次

所有读取均为防御性：脏时间戳/非法状态词不崩溃，静默回退默认值。
"""

import json
import os
import time
from datetime import datetime, timedelta

# 内置预设状态词库：词 → 说明
BUILTIN_WORDS = {
    "在线": "正常工作，可接受任务",
    "忙碌": "正在处理任务，尽量勿扰",
    "勿扰": "请勿打扰，紧急事项请直接联系",
    "隐身": "在线但对他人隐藏",
    "离开": "暂时离开，稍后回来",
    "自定义": "自定义状态词：/机器状态 set <任意词>",
}

# 历史数据保留天数（防膨胀）
KEEP_DAYS = 90
# 单次状态词最大长度（防刷屏滥用）
MAX_WORD_LEN = 20
# 通知去抖默认窗口（秒）
DEBOUNCE_SECONDS = 60
# 报表默认统计天数
DEFAULT_REPORT_DAYS = 7


def _now_iso() -> str:
    """当前时间 ISO 字符串（与 monitor.now_iso 同格式）"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: str, data) -> None:
    """JSON 原子写：先写临时文件再 os.replace，避免写一半损坏"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _parse_ts(ts, default=None):
    """容错解析时间戳（%Y-%m-%d %H:%M:%S / ISO 格式 / 秒级数字），脏值返回 default"""
    if not ts:
        return default
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts))
        except (OSError, ValueError, OverflowError):
            return default
    text = str(ts).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return default


# ---------- 词库 ----------

def build_words(extra_words) -> dict:
    """合并内置词库与配置扩展词库。

    extra_words 元素支持纯词（说明为「配置扩展词」）或「词:说明」形式。
    """
    words = dict(BUILTIN_WORDS)
    for item in extra_words or []:
        item = str(item).strip()
        if not item:
            continue
        if ":" in item:
            w, _, desc = item.partition(":")
            w, desc = w.strip(), desc.strip()
        else:
            w, desc = item, "配置扩展词"
        if w:
            words[w] = desc or "配置扩展词"
    return words


def is_valid_word(word, words=None) -> bool:
    """状态词合法性校验：非空、非纯空白、长度不超限。非法返回 False（不抛异常）"""
    if not word or not str(word).strip():
        return False
    return len(str(word).strip()) <= MAX_WORD_LEN


def fmt_duration(seconds) -> str:
    """秒 → 时分秒文本（负数/脏值按 0 处理）"""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        s = 0
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


# ---------- 按天统计 ----------

def split_by_day(start_dt: datetime, end_dt: datetime):
    """把 [start, end) 时间区间按自然日拆分。

    返回 [(日期字符串, 段起点, 段终点), ...]，覆盖跨天（凌晨切换/重启后持续到次日）场景。
    """
    if end_dt <= start_dt:
        return []
    segs = []
    cur = start_dt
    while cur < end_dt:
        day_end = (cur + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        seg_end = min(day_end, end_dt)
        if seg_end > cur:
            segs.append((cur.strftime("%Y-%m-%d"), cur, seg_end))
        cur = seg_end
    return segs


def daily_stats(events, days: int = DEFAULT_REPORT_DAYS, now_dt: datetime | None = None) -> dict:
    """按天统计各状态持续时长。

    events: [(datetime, 状态词), ...]（时间升序；脏数据由调用方过滤）
    days: 统计最近 N 天（含今天，自动钳制到 1~90）
    now_dt: 统计截止时刻（默认当前时间）

    返回:
    {
        "days": [
            {"date": "2026-08-17", "total": 秒, "by_status": {状态: 秒},
             "longest_online": 秒}, ...
        ],
        "grand": {"total": 秒, "by_status": {状态: 秒}},
    }
    """
    now_dt = now_dt or datetime.now()
    if days is None:
        days = DEFAULT_REPORT_DAYS
    days = max(1, min(int(days), KEEP_DAYS))
    # 统计窗口起点：最近 days 天（含今天）的 0 点；窗口之前开始的段只统计窗口内部分
    window_start = (now_dt - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    window_start_str = window_start.strftime("%Y-%m-%d")
    pts = [(dt, str(st).strip()) for dt, st in events if dt <= now_dt and str(st).strip()]
    # 防御：乱序输入按时间排序，时间倒挂的段在下方跳过
    pts.sort(key=lambda p: p[0])
    result: dict[str, dict] = {}
    for i, (ts, st) in enumerate(pts):
        end = pts[i + 1][0] if i + 1 < len(pts) else now_dt
        if end <= ts:
            # 脏数据：时间倒挂，跳过该段
            continue
        # 按天拆分（跨天段归到各自然日）
        for date_str, seg_start, seg_end in split_by_day(ts, end):
            if date_str < window_start_str:
                continue
            secs = int((seg_end - seg_start).total_seconds())
            day = result.setdefault(
                date_str, {"total": 0, "by_status": {}, "longest_online": 0}
            )
            day["total"] += secs
            day["by_status"][st] = day["by_status"].get(st, 0) + secs
            if st == "在线":
                # 该天内连续在线的最大段长（跨天段已按天切开）
                day["longest_online"] = max(day["longest_online"], secs)
    out_days = []
    grand = {"total": 0, "by_status": {}}
    for i in range(days):
        d = (window_start + timedelta(days=i)).strftime("%Y-%m-%d")
        day = dict(result.get(d, {"total": 0, "by_status": {}, "longest_online": 0}))
        day["date"] = d
        out_days.append(day)
        grand["total"] += day["total"]
        for st, secs in day["by_status"].items():
            grand["by_status"][st] = grand["by_status"].get(st, 0) + secs
    return {"days": out_days, "grand": grand}


def render_report(stats: dict, days: int) -> str:
    """把 daily_stats 结果渲染为群内报表文本"""
    lines = [f"[状态同步] 在线时长报表（最近 {days} 天）"]
    for day in stats["days"]:
        if not day["total"]:
            lines.append(f"{day['date']}: 无记录")
            continue
        lines.append(f"{day['date']}: 总时长 {fmt_duration(day['total'])}")
        for st, secs in sorted(day["by_status"].items(), key=lambda kv: -kv[1]):
            pct = secs * 100.0 / day["total"]
            lines.append(f"  {st}: {fmt_duration(secs)}（{pct:.1f}%）")
        if day["longest_online"] > 0:
            lines.append(f"  最长连续在线: {fmt_duration(day['longest_online'])}")
    g = stats["grand"]
    if g["total"]:
        lines.append(f"合计: {fmt_duration(g['total'])}")
        for st, secs in sorted(g["by_status"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {st}: {secs * 100.0 / g['total']:.1f}%")
    return "\n".join(lines)


# ---------- 状态存储 ----------

class StatusStore:
    """用户状态存储：当前状态 + 历史事件，独立 JSON 文件原子写。"""

    def __init__(self, plugin_dir: str, keep_days: int = KEEP_DAYS):
        self.plugin_dir = plugin_dir
        self.status_path = os.path.join(plugin_dir, "data", "user_status.json")
        self.history_path = os.path.join(plugin_dir, "data", "status_history.json")
        self.keep_days = keep_days

    # ---- 当前状态 ----

    def load_status(self) -> dict | None:
        """读取当前状态（损坏文件/脏数据返回 None，不崩溃）"""
        try:
            with open(self.status_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("status"):
            return None
        return {
            "status": str(data["status"]),
            "updated_at": str(data.get("updated_at", "")),
            "source": str(data.get("source", "")),
        }

    def set_status(self, status: str, source: str, ts: str | None = None) -> tuple[bool, dict | None]:
        """写入当前状态（原子写）。

        返回 (是否实际变更, 变更前状态)。同状态连续写不重复写入、不产生新事件。
        """
        ts = ts or _now_iso()
        prev = self.load_status()
        if prev and prev["status"] == status:
            return False, prev
        _atomic_write_json(self.status_path, {
            "status": status,
            "updated_at": ts,
            "source": source,
        })
        return True, prev

    # ---- 历史事件 ----

    def load_events(self) -> list[dict]:
        """读取历史事件（脏时间戳/脏状态静默过滤），按时间升序返回"""
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        raw = data.get("events", []) if isinstance(data, dict) else []
        events = []
        for ev in raw:
            if not isinstance(ev, dict):
                continue
            dt = _parse_ts(ev.get("ts"))
            st = ev.get("status")
            if dt is None or not st or not str(st).strip():
                continue
            events.append({
                "ts": dt,
                "status": str(st).strip(),
                "source": str(ev.get("source", "")),
            })
        events.sort(key=lambda e: e["ts"])
        return events

    def append_event(self, status: str, source: str, ts: str | None = None) -> None:
        """追加一条状态切换事件，并裁剪 keep_days 天前的旧数据防膨胀（原子写）"""
        ts = ts or _now_iso()
        dt = _parse_ts(ts)
        if dt is None:
            return  # 脏时间戳：不写入
        events = self.load_events()
        events.append({"ts": dt, "status": str(status).strip(), "source": source})
        cutoff = datetime.now() - timedelta(days=self.keep_days)
        events = [e for e in events if e["ts"] >= cutoff]
        _atomic_write_json(self.history_path, {"events": [
            {
                "ts": e["ts"].strftime("%Y-%m-%d %H:%M:%S"),
                "status": e["status"],
                "source": e["source"],
            }
            for e in events
        ]})

    # ---- 报表 ----

    def report(self, days: int = DEFAULT_REPORT_DAYS) -> str:
        """最近 N 天在线时长报表文本（读取时计算，不依赖状态文件结构）"""
        events = [(e["ts"], e["status"]) for e in self.load_events()]
        stats = daily_stats(events, days)
        return render_report(stats, days)


# ---------- 通知去抖 ----------

class NotifyDebounce:
    """状态变更通知去抖：同状态在窗口内只通知一次"""

    def __init__(self):
        self._last_status: str | None = None
        self._last_ts: float = 0.0

    def should_notify(self, status: str, window: int = DEBOUNCE_SECONDS,
                      now: float | None = None) -> bool:
        """是否应通知：同状态且距上次通知不足窗口秒 → False；否则记录并返回 True"""
        try:
            window = max(0, int(window))
        except (TypeError, ValueError):
            window = DEBOUNCE_SECONDS
        now = now if now is not None else time.time()
        if status == self._last_status and now - self._last_ts < window:
            return False
        self._last_status = status
        self._last_ts = now
        return True

    def reset(self) -> None:
        """重置去抖状态（测试/热重载用）"""
        self._last_status = None
        self._last_ts = 0.0