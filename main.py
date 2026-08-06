"""AstrBot 插件：同步电脑端 opencode / mimocode / openclaw 等程序运行状态。

功能：
- /机器状态（或 /sync）：查看所有机器状态概览
- /机器状态 <机器名>：查看指定机器详细状态（进程/CLI/日志）
- /机器状态 file [格式] [粒度]：立即生成状态文件（格式 json/md/txt/csv，粒度 summary/full）
- /机器状态 report：立即向播报目标发送一次状态报告
- /机器状态 reload：重新加载机器配置
- /机器状态 权限...：权限规则远程管理（允许/拒绝/询问/删除/清空/事件）
- /机器状态 同意 <ID> / 拒绝 <ID>：serve 模式下远程批准/拒绝挂起的权限请求
- /机器状态 加密串 <明文>：把密码加密成 dpapi: 密文（凭据安全）
- 定时自动播报（默认每 30 分钟），状态变化实时播报（默认每 60 秒检测）
- 权限事件监控：opencode 日志中的权限请求/评估自动转发到群
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
from .perm import PermissionMonitor, PermRequest, RuleManager, ServeClient

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
        self._perm_task: asyncio.Task | None = None
        self._serve_task: asyncio.Task | None = None
        self._serve_retry_task: asyncio.Task | None = None
        self._recent_sessions: list[str] = []
        self._loop = asyncio.get_event_loop()
        # 权限远程控制
        self.perm_monitor = PermissionMonitor(config, self.plugin_dir)
        cfg_file = config.get("opencode_config_file", "%USERPROFILE%/.config/opencode/opencode.jsonc")
        self.perm_rules = RuleManager(os.path.expandvars(os.path.expanduser(cfg_file)))
        serve_url = config.get("opencode_serve_url", "") or ""
        self.serve = (
            ServeClient(
                serve_url,
                config.get("opencode_serve_username", "") or "",
                config.get("opencode_serve_password", "") or "",
            )
            if serve_url
            else None
        )

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
        if cmd == "权限":
            return await self._handle_permission(tokens)
        if cmd.startswith("权限") and len(cmd) > 2:
            # 兼容连写形式：/机器状态 权限允许 bash git push *
            return await self._handle_permission(["权限", cmd[2:]] + tokens[1:])
        if cmd in ("同意", "批准", "allow"):
            return await self._handle_approve(tokens, approve=True)
        if cmd in ("拒绝", "驳回", "deny"):
            return await self._handle_approve(tokens, approve=False)
        if cmd in ("加密串", "encrypt"):
            return await self._handle_encrypt(tokens)
        states = await self.monitor.check_all()
        st = next((s for s in states if s.get("name") == sub), None)
        if st is None:
            names = "、".join(s.get("name", "?") for s in states)
            return f"未找到机器 {sub}，可用机器: {names}"
        return format_detail(st)

    # ---------- 权限远程控制 ----------

    async def _handle_permission(self, tokens: list[str]) -> str:
        """权限规则管理：权限 / 权限允许 <工具> <模式> / 权限拒绝 / 权限询问 / 权限删除 / 权限清空 / 权限事件"""
        sub = tokens[1] if len(tokens) > 1 else ""
        if not sub:
            return self.perm_rules.format_rules()
        if sub == "清空":
            return self.perm_rules.clear_rules()
        if sub == "事件":
            evs = self.perm_monitor.recent(10)
            if not evs:
                return "暂无权限事件记录"
            return "\n".join(
                f"[{e.action}] {e.source} {e.tool}「{e.pattern}」 @{e.ts}"
                for e in evs
            )
        if sub in ("允许", "allow", "拒绝", "deny", "询问", "ask"):
            if len(tokens) < 3:
                return f"用法: /机器状态 权限{sub} <工具> <模式>（如: 权限{sub} bash git push *）"
            action = {"允许": "allow", "allow": "allow", "拒绝": "deny", "deny": "deny", "询问": "ask", "ask": "ask"}[sub]
            tool = tokens[2]
            pattern = " ".join(tokens[3:]) or "*"
            return self.perm_rules.set_rule(tool, pattern, action)
        if sub in ("删除", "remove"):
            if len(tokens) < 3:
                return "用法: /机器状态 权限删除 <工具> <模式>"
            tool = tokens[2]
            pattern = " ".join(tokens[3:]) or "*"
            return self.perm_rules.remove_rule(tool, pattern)
        return f"未知子命令: {sub}（支持: 允许/拒绝/询问/删除/清空/事件）"

    async def _handle_approve(self, tokens: list[str], approve: bool) -> str:
        """同意/拒绝 serve 模式下挂起的权限请求"""
        if not self.serve:
            return "未启用 opencode serve 远程批准（请配置 opencode_serve_url）"
        if len(tokens) < 2:
            return "用法: /机器状态 同意 <权限ID> [always]"
        pid = tokens[1]
        remember = len(tokens) > 2 and tokens[2] == "always"
        response = "once" if approve else "reject"
        if remember:
            response = "always"
        self.serve.prune()
        req = self.serve.pending.get(pid)
        if not req:
            pending_ids = list(self.serve.pending.keys())[:10]
            return (
                f"未找到权限请求 {pid}（可能已过期）"
                + (f"\n当前挂起: {pending_ids}" if pending_ids else "")
            )
        ok = await asyncio.to_thread(
            self.serve.respond, pid, response, remember
        )
        if ok:
            return f"已{'批准' if approve else '拒绝'}权限请求 {pid}（{req.tool}「{req.detail}」）"
        return f"响应失败：请求可能已被处理或会话已结束"

    async def _handle_encrypt(self, tokens: list[str]) -> str:
        """把密码明文就地加密成 dpapi 密文（仅当前 Windows 用户可解）"""
        if len(tokens) < 2:
            return "用法: /机器状态 加密串 <明文密码>（Windows 上生成 dpapi: 密文）"
        try:
            from .secret import dpapi_encrypt
        except Exception as e:  # noqa: BLE001
            return f"加密不可用: {e}"
        try:
            b64 = dpapi_encrypt(" ".join(tokens[1:]))
        except Exception as e:  # noqa: BLE001
            return f"加密失败（仅支持 Windows）: {e}"
        return (
            "已生成 DPAPI 密文（仅本机当前用户可解密），"
            "填入 machines.json 对应字段:\n"
            f"dpapi:{b64}\n"
            "注意：此密文在别的用户/机器上无法解密，请勿下发到群以外环境。"
        )

    async def _on_perm_request(self, req: PermRequest):
        """收到 serve 权限请求，转发到群"""
        if not self.cfg.get("permission_report", True):
            return
        text = (
            "[权限请求] opencode 请求执行\n"
            f"工具: {req.tool}\n"
            f"内容: {req.detail}\n"
            f"权限ID: {req.permission_id}\n"
            "回复「同意 <ID>」放行，或「拒绝 <ID>」阻止"
        )
        await self._broadcast(text)

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

    def _first_self_id(self, platform_name: str) -> str:
        """取平台首个已连接 OneBot 客户端的 self_id（多连接时缺 self_id 会 ApiNotAvailable）"""
        try:
            plat = next(
                (
                    p
                    for p in self.context.platform_manager.platform_insts
                    if p.meta().id == platform_name
                ),
                None,
            )
            bot = getattr(plat, "bot", None)
            clients = getattr(bot, "_wsr_api_clients", None) or getattr(bot, "_api_clients", None)
            if isinstance(clients, dict) and clients:
                return str(next(iter(clients)))
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _send_chain(self, umo: str, chain) -> bool:
        """带 self_id 直发（多连接时 context.send_message 会 ApiNotAvailable），失败回退"""
        try:
            from astrbot.core.platform.message_session import MessageSesion
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            from astrbot.api.all import MessageType

            session = MessageSesion.from_str(umo)
            plat = next(
                (
                    p
                    for p in self.context.platform_manager.platform_insts
                    if p.meta().id == session.platform_name
                ),
                None,
            )
            if plat is not None and getattr(plat, "bot", None) is not None:
                sid = self._first_self_id(session.platform_name)
                if sid:
                    seg = await AiocqhttpMessageEvent._parse_onebot_json(chain)
                    if not seg:
                        return True
                    if session.message_type == MessageType.GROUP_MESSAGE:
                        await plat.bot.send_group_msg(
                            group_id=int(session.session_id),
                            message=seg,
                            self_id=sid,
                        )
                    elif session.message_type == MessageType.FRIEND_MESSAGE:
                        await plat.bot.send_private_msg(
                            user_id=int(session.session_id),
                            message=seg,
                            self_id=sid,
                        )
                    return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"self_id 直发失败（回退 send_message）: {e!r}")
        return await self.context.send_message(umo, chain)

    async def _broadcast(self, text: str):
        for umo in self._report_targets():
            try:
                await self._send_chain(umo, MessageChain([Plain(text)]))
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
                await self._send_chain(
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
        if self._perm_task is None:
            self.perm_monitor.load_cursor()
            self._perm_task = asyncio.create_task(self._perm_loop())
            logger.info("状态同步：权限事件监控任务已启动")
        if self.serve is not None and self._serve_task is None:
            if await asyncio.to_thread(self.serve.probe):
                self._serve_task = asyncio.create_task(
                    self.serve.listen_loop(self._on_perm_request, self._loop)
                )
                logger.info(f"状态同步：已连接 opencode serve ({self.serve.base_url})")
            else:
                self._serve_retry_task = asyncio.create_task(self._serve_connect_loop())
                logger.warning("状态同步：opencode serve 暂不可达，后台轮询等待重连")

    async def _serve_connect_loop(self):
        """serve 未就绪时轮询探测，成功后转正式监听任务（serve 可能晚于插件启动）"""
        try:
            while self._serve_task is None:
                await asyncio.sleep(10)
                if not self.serve or await asyncio.to_thread(self.serve.probe):
                    break
            if self.serve and self._serve_task is None:
                self._serve_task = asyncio.create_task(
                    self.serve.listen_loop(self._on_perm_request, self._loop)
                )
                logger.info(f"状态同步：已连接 opencode serve ({self.serve.base_url})")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"状态同步：serve 重试连接失败: {e}")

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self):
        for task in (self._task, self._change_task, self._perm_task, self._serve_task, self._serve_retry_task):
            if task:
                task.cancel()
        self._task = None
        self._change_task = None
        self._perm_task = None
        self._serve_task = None
        self._serve_retry_task = None
        await self.monitor.close_all()

    async def _perm_loop(self):
        """定期检查 opencode 日志中的权限评估事件并转发"""
        interval = max(5, int(self.cfg.get("permission_check_interval_seconds", 30)))
        while True:
            await asyncio.sleep(interval)
            try:
                if not self.cfg.get("permission_report", True):
                    continue
                if not self._report_targets():
                    continue
                events = self.perm_monitor.poll(self.monitor.machines)
                if not events:
                    continue
                level_cfg = self.cfg.get("permission_forward_level", "deny,ask")
                allowed = {
                    x.strip() for x in str(level_cfg).split(",") if x.strip()
                }
                for ev in events:
                    if ev.action not in allowed:
                        continue
                    text = (
                        f"[权限] {ev.source}: {ev.tool} 请求执行「{ev.pattern}」"
                        f" → {ev.action}"
                    )
                    if ev.pid and self.serve:
                        # asking 行：注册到 serve 挂起审批，实现日志→现场放行桥接
                        await self._register_perm_from_log(ev)
                        text += (
                            f"\n权限ID: {ev.pid}"
                            f"\n回复「同意 {ev.pid}」现场放行，或「拒绝 {ev.pid}」阻止"
                        )
                    await self._broadcast(text)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("权限事件监控失败")

    async def _register_perm_from_log(self, ev):
        """把日志 asking 行（带权限 ID）桥接为 serve 挂起请求，供群回复审批"""
        if not ev.pid or ev.pid in self.serve.pending:
            return
        sid = await self._resolve_perm_session(ev.ts)
        if not sid:
            return
        self.serve.pending[ev.pid] = PermRequest(
            session_id=sid, permission_id=ev.pid, tool=ev.tool, detail=ev.pattern
        )

    async def _resolve_perm_session(self, ts: str) -> str:
        """权限询问时刻 → 归属 serve 会话：取 created 最晚且早于询问时刻的会话"""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00") if ts.endswith("Z") else ts)
            limit_ms = int(dt.timestamp() * 1000)
        except ValueError:
            return ""
        try:
            r = await asyncio.to_thread(
                self.serve._get_requests().get,
                self.serve.base_url + "/session",
                timeout=10,
                headers=self.serve.auth,
            )
            if r.status_code != 200:
                return ""
            # serve 按 created 降序返回，首个 created < 询问时刻即最近创建的任务会话
            for s in r.json():
                t = s.get("time") or {}
                if int(t.get("created", 0)) < limit_ms:
                    return str(s.get("id", ""))
        except Exception:  # noqa: BLE001
            return ""
        return ""

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
