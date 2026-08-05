"""AstrBot 插件：同步电脑端 opencode / mimocode / openclaw 等程序运行状态。

功能：
- /机器状态（或 /sync）：查看所有机器状态概览
- /机器状态 <机器名>：查看指定机器详细状态（进程/CLI/日志）
- /机器状态 file [格式] [粒度]：立即生成状态文件（格式 json/md/txt/csv，粒度 summary/full）
- /机器状态 report：立即向播报目标发送一次状态报告
- /机器状态 reload：重新加载机器配置
- 定时自动播报（默认每 30 分钟），状态变化实时播报（默认每 60 秒检测）
- 状态文件支持多格式、多种送达方式（文本/文件/本地），生成时机可配置（手动/定时/变化时）

机器配置见 data/machines.json，格式说明见 monitor.py 顶部注释。
"""

import asyncio
import os
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star, register

from .formatter import (
    format_detail,
    format_overview,
    render_text,
    write_files,
)
from .monitor import Monitor

FILE_FORMATS = ("json", "md", "txt", "csv")
FILE_DETAILS = ("summary", "full")


@register(
    "astrbot_plugin_status_sync",
    "yunxiao258",
    "同步电脑端 opencode / mimocode / openclaw 等程序运行状态",
    "1.0.0",
    repo="https://github.com/yunxiao258/astrbot_plugin_status_sync",
)
class StatusSyncPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.monitor = Monitor(config, self.plugin_dir)
        self._task: asyncio.Task | None = None
        self._change_task: asyncio.Task | None = None
        self._recent_sessions: list[str] = []

    # ---------- 命令 ----------

    @filter.command("机器状态", alias={"sync", "状态同步"})
    async def status_cmd(self, event: AstrMessageEvent):
        """查询机器状态：/机器状态、/机器状态 <机器名>、/机器状态 file、/机器状态 report、/机器状态 reload"""
        if not self.cfg.get("enabled", True):
            return
        try:
            text = await self._handle_command(event)
        except Exception as e:  # noqa: BLE001
            logger.exception("状态查询失败")
            text = f"状态查询失败: {e}"
        await self._remember(event)
        await event.send(MessageChain([Plain(text)]))

    async def _handle_command(self, event: AstrMessageEvent) -> str:
        arg = event.message_str.strip()
        parts = arg.split(maxsplit=1)
        sub = parts[1].strip() if len(parts) > 1 else ""
        if not sub:
            states = await self.monitor.check_all()
            return format_overview(states)
        tokens = sub.split()
        cmd = tokens[0]
        if cmd == "reload":
            n = self.monitor.reload()
            return f"机器配置已重新加载，共 {n} 台"
        if cmd == "file":
            states = await self.monitor.check_all()
            fmt_arg = tokens[1] if len(tokens) > 1 else None
            det_arg = tokens[2] if len(tokens) > 2 else None
            formats = [fmt_arg] if fmt_arg in FILE_FORMATS else None
            detail = det_arg if det_arg in FILE_DETAILS else None
            done = await self._generate_and_deliver(
                states, formats=formats, detail=detail
            )
            return f"状态文件已生成并送达（{done}）"
        if cmd == "report":
            states = await self.monitor.check_all()
            await self._broadcast(format_overview(states))
            return "已播报状态报告"
        states = await self.monitor.check_all()
        st = next((s for s in states if s.get("name") == sub), None)
        if st is None:
            names = "、".join(s.get("name", "?") for s in states)
            return f"未找到机器 {sub}，可用机器: {names}"
        return format_detail(st)

    # ---------- 播报 ----------

    async def _remember(self, event: AstrMessageEvent):
        """记录最近询问过状态的会话，作为定时播报的默认目标"""
        umo = str(event.session)
        if umo in self._recent_sessions:
            self._recent_sessions.remove(umo)
        self._recent_sessions.insert(0, umo)
        del self._recent_sessions[5:]

    def _report_targets(self) -> list[str]:
        """定时播报目标会话 UMO 列表"""
        groups = self.cfg.get("report_groups", "")
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",") if g.strip()]
        else:
            groups = [g for g in (groups or []) if g]
        return groups or list(self._recent_sessions)

    async def _broadcast(self, text: str):
        for umo in self._report_targets():
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"播报到 {umo} 失败: {e}")

    def _split_cfg(self, key: str, default: str | None) -> list[str]:
        """把逗号分隔的配置项拆成列表"""
        v = self.cfg.get(key, default)
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return [x for x in (v or []) if x]

    def _status_file_dir(self) -> str:
        """状态文件输出目录（绝对路径）"""
        rel = self.cfg.get("status_file", "data/status_sync.json")
        out_dir = os.path.dirname(rel) or "data"
        base = os.path.splitext(os.path.basename(rel))[0] or "status_sync"
        if not os.path.isabs(out_dir):
            out_dir = os.path.join(self.plugin_dir, out_dir)
        return out_dir, base

    async def _send_file(self, path: str, fmt: str):
        """把状态文件作为文件消息发送到所有播报目标"""
        name = f"状态同步_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"
        for umo in self._report_targets():
            try:
                await self.context.send_message(
                    umo, MessageChain([File(name=name, file=path)])
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"发送文件到 {umo} 失败: {e}")

    async def _generate_and_deliver(
        self,
        states: list[dict],
        formats: list[str] | None = None,
        detail: str | None = None,
        send_text: bool = True,
    ) -> str:
        """按配置生成状态文件并送达。返回送达描述字符串。

        送达方式（file_delivery）：text=文本消息，file=文件消息，local=仅本地保存。
        格式（file_formats）：json/md/txt/csv，逗号分隔。
        粒度（file_detail）：summary=概览，full=全量详情。
        """
        out = []
        detail = detail or self.cfg.get("file_detail", "summary") or "summary"
        delivery = self._split_cfg("file_delivery", "text,file,local") or ["local"]
        fmt_list = (
            formats
            if formats is not None
            else self._split_cfg("file_formats", "json,md,txt,csv")
        )
        if "text" in delivery and send_text:
            await self._broadcast(render_text(states, detail))
            out.append("文本")
        if "file" in delivery or "local" in delivery:
            out_dir, base = self._status_file_dir()
            written = write_files(states, out_dir, base, fmt_list, detail)
            if not written:
                out.append("未生成文件")
            for fmt, path in written.items():
                if "file" in delivery:
                    await self._send_file(path, fmt)
                    out.append(f"文件({fmt})")
                if "local" in delivery:
                    out.append(f"已存本地 {os.path.basename(path)}")
        return "，".join(out) if out else "未生成"

    # ---------- 生命周期与定时播报 ----------

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if not self.cfg.get("enabled", True):
            return
        self.monitor.reload()
        if self._task is None:
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("状态同步：定时播报任务已启动")
        if self._change_task is None:
            self._change_task = asyncio.create_task(self._change_loop())
            logger.info("状态同步：状态变化检测任务已启动")

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self):
        for task in (self._task, self._change_task):
            if task:
                task.cancel()
        self._task = None
        self._change_task = None
        await self.monitor.close_all()

    async def _poll_loop(self):
        while True:
            minutes = max(1, int(self.cfg.get("poll_interval_minutes", 30)))
            await asyncio.sleep(minutes * 60)
            try:
                if not self.cfg.get("report_enabled", True):
                    continue
                if not self._report_targets():
                    continue
                if "scheduled" not in self._split_cfg(
                    "file_generate_timing", "manual,scheduled,on_change"
                ):
                    continue
                states = await self.monitor.check_all()
                await self._generate_and_deliver(states)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("定时状态播报失败")

    async def _change_loop(self):
        """短间隔检测目标程序运行状态变化，变化时立即播报（实时性）"""
        interval = max(10, int(self.cfg.get("state_change_interval_seconds", 60)))
        while True:
            await asyncio.sleep(interval)
            try:
                if not self.cfg.get("state_change_report", False):
                    continue
                if not self._report_targets():
                    continue
                changes = await self.monitor.detect_changes()
                if changes:
                    await self._broadcast("[状态同步] " + "；".join(changes))
                    if "on_change" in self._split_cfg(
                        "file_generate_timing", "manual,scheduled,on_change"
                    ):
                        states = await self.monitor.check_all()
                        await self._generate_and_deliver(states, send_text=False)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("状态变化检测失败")
