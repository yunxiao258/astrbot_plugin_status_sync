"""报告格式化与状态文件输出"""

import json
import os
from datetime import datetime


def _fmt_bytes(b) -> str:
    """字节数 → 人类可读文本"""
    if not b:
        return "-"
    b = int(b)
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f}G"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.0f}M"
    if b >= 1024:
        return f"{b / 1024:.0f}K"
    return f"{b}B"


def _fmt_resources(r: dict) -> str:
    """资源摘要文本"""
    if not r:
        return ""
    parts = []
    cpu = r.get("cpu")
    if cpu is not None:
        parts.append(f"CPU {cpu:.0f}%")
    t, u = r.get("mem_total"), r.get("mem_used")
    if t and u:
        parts.append(f"内存 {_fmt_bytes(u)}/{_fmt_bytes(t)}")
    return " ".join(parts)


def _latest_activity(t: dict, max_len: int = 60) -> str:
    """从 target 的日志中提取最近活动摘要"""
    logs = t.get("logs") or []
    if not logs:
        return ""
    line = logs[-1].strip()
    if len(line) > max_len:
        line = line[: max_len - 3] + "..."
    return line


def format_overview(states: list[dict]) -> str:
    """所有机器概览报告"""
    lines = [
        "[状态同步] 机器状态概览",
        "时间: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]
    for s in states:
        if not s.get("ok"):
            lines.append(
                f"- {s.get('name', '?')} [{s.get('type')}] 检测失败: {s.get('error', '未知错误')}"
            )
            continue
        head = f"- {s.get('name')} [{s.get('type')}]"
        rs = _fmt_resources(s.get("resources"))
        if rs:
            head += f" ({rs})"
        tgs = s.get("targets") or []
        if tgs:
            for t in tgs:
                mark = "运行中" if t.get("running") else "未运行"
                head += f" | {t.get('name')}:{mark}"
                act = _latest_activity(t)
                if act:
                    head += f" ~ {act}"
        if s.get("http_data"):
            head += " | HTTP: " + json.dumps(s["http_data"], ensure_ascii=False)[:200]
        lines.append(head)
    lines.append("输入 /机器状态 <机器名> 查看详细，/机器状态 file 生成状态文件")
    return "\n".join(lines)


def format_detail(state: dict) -> str:
    """单台机器详细报告"""
    name = state.get("name", "?")
    if not state.get("ok"):
        return f"[{name}] 检测失败: {state.get('error', '未知错误')}"
    lines = [
        f"[状态同步] {name} [{state.get('type')}]",
        f"时间: {state.get('ts', '')}",
    ]
    rs = _fmt_resources(state.get("resources"))
    if rs:
        lines.append("资源: " + rs)
    if state.get("http_data"):
        lines.append("HTTP 状态: " + json.dumps(state["http_data"], ensure_ascii=False)[:1500])
    for t in state.get("targets") or []:
        head = f"- {t.get('name')}: {'运行中' if t.get('running') else '未运行'}"
        if t.get("pid"):
            head += f" (PID {t['pid']})"
        lines.append(head)
        if t.get("mem_kb"):
            lines.append(f"  内存: {_fmt_bytes(t['mem_kb'] * 1024)}")
        if t.get("cli"):
            lines.append("  CLI: " + t["cli"][:300])
        for ln in t.get("logs", []):
            lines.append("  日志: " + ln[:150])
    return "\n".join(lines)


def write_status_file(states: list[dict], path: str) -> str:
    """把状态写入 JSON 文件，返回实际路径"""
    payload = {
        "generated_at": datetime.now().isoformat(),
        "machines": states,
    }
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def render_text(states: list[dict], detail: str = "summary") -> str:
    """纯文本报告：summary=概览，full=全量详情拼接"""
    if detail == "full":
        return "\n\n".join(format_detail(s) for s in states)
    return format_overview(states)


def render_markdown(states: list[dict], detail: str = "summary") -> str:
    """Markdown 报告"""
    lines = [
        "# 状态同步报告",
        "",
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for s in states:
        if not s.get("ok"):
            lines.append(f"## {s.get('name', '?')} [{s.get('type')}]")
            lines.append(f"**检测失败:** {s.get('error', '未知错误')}")
            lines.append("")
            continue
        lines.append(f"## {s.get('name')} [{s.get('type')}]")
        rs = _fmt_resources(s.get("resources"))
        if rs:
            lines.append(f"**资源:** {rs}")
        lines.append("")
        lines.append("| 程序 | 状态 | PID | 最近活动 |")
        lines.append("| --- | --- | --- | --- |")
        for t in s.get("targets") or []:
            mark = "运行中" if t.get("running") else "未运行"
            act = (_latest_activity(t, 60) or "-").replace("|", "\\|")
            pid = t.get("pid") if t.get("pid") else "-"
            lines.append(f"| {t.get('name')} | {mark} | {pid} | {act} |")
        if detail == "full":
            for t in s.get("targets") or []:
                if t.get("cli"):
                    lines.append("")
                    lines.append(f"### {t.get('name')} CLI")
                    lines.append("```")
                    lines.append(t["cli"][:1000])
                    lines.append("```")
                if t.get("logs"):
                    lines.append("")
                    lines.append(f"### {t.get('name')} 日志")
                    lines.append("```")
                    lines.extend(f"{ln[:150]}" for ln in t["logs"])
                    lines.append("```")
        lines.append("")
    return "\n".join(lines)


def render_csv(states: list[dict]) -> str:
    """CSV 表格（含 BOM，Excel 可直接打开中文）"""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["机器", "类型", "程序", "状态", "PID", "内存KB", "CLI", "最近活动"])
    for s in states:
        for t in s.get("targets") or []:
            w.writerow([
                s.get("name", ""), s.get("type", ""), t.get("name", ""),
                "运行中" if t.get("running") else "未运行",
                t.get("pid") if t.get("pid") else "",
                t.get("mem_kb") if t.get("mem_kb") else "",
                (t.get("cli") or "")[:100],
                _latest_activity(t, 100),
            ])
    return buf.getvalue()


def write_files(
    states: list[dict], out_dir: str, base: str, formats: list[str], detail: str = "summary"
) -> dict:
    """按格式列表写入状态文件，返回 {格式: 路径}"""
    os.makedirs(out_dir, exist_ok=True)
    written = {}
    if "json" in formats:
        p = os.path.join(out_dir, base + ".json")
        write_status_file(states, p)
        written["json"] = p
    if "md" in formats:
        p = os.path.join(out_dir, base + ".md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(states, detail))
        written["md"] = p
    if "txt" in formats:
        p = os.path.join(out_dir, base + ".txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(render_text(states, detail))
        written["txt"] = p
    if "csv" in formats:
        p = os.path.join(out_dir, base + ".csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(render_csv(states))
        written["csv"] = p
    return written
