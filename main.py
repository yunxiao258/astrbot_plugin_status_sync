"""AstrBot 插件：同步电脑端 opencode / mimocode / openclaw 等程序运行状态。

功能：
- /机器状态（或 /sync、/status）：查看所有机器状态概览
- /机器状态 <机器名>：查看指定机器详细状态（进程/CLI/日志）
- /机器状态 file [格式] [粒度]：立即生成状态文件（格式 json/md/txt/csv，粒度 summary/full）
- /机器状态 report：立即向播报目标发送一次状态报告
- /机器状态 reload：重新加载机器配置
- /机器状态 权限...：权限规则远程管理（允许/拒绝/询问/删除/清空/事件）
- /机器状态 同意 <ID> / 拒绝 <ID>：serve 模式下远程批准/拒绝挂起的权限请求
- /机器状态 加密串 <明文>：把密码加密成 dpapi: 密文（凭据安全）
- /机器状态 set <状态词>：快捷切换状态（在线/忙碌/勿扰/隐身/离开/自定义或任意词）
- /机器状态 words：查看预设状态词库与说明
- /机器状态 report <N>：最近 N 天（默认 7）在线时长报表（每日总时长/各状态占比/最长连续在线）
- 定时自动播报（默认每 30 分钟），状态变化实时播报（默认每 60 秒检测）
- 权限事件监控：opencode 日志中的权限请求/评估自动转发到群
- 状态文件支持多格式、多种送达方式（文本/文件/本地），生成时机可配置（手动/定时/变化时）
- 状态变更通知：状态切换实时推送到指定群（notify_targets，去抖防重复）

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
from .user_status import (
    DEFAULT_REPORT_DAYS,
    MAX_WORD_LEN,
    NotifyDebounce,
    StatusStore,
    build_words,
    is_valid_word,
)
from .ws_server import WsHub

FILE_FORMATS = ("json", "md", "txt", "csv")
FILE_DETAILS = ("summary", "full")


def _cfg_int(cfg, key: str, default: int) -> int:
    """防御性读取整数配置：脏值（如字符串/None）回退默认"""
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


@register(
    "astrbot_plugin_status_sync",
    "yunxiao258",
    "同步电脑端 opencode / mimocode / openclaw 等程序运行状态",
    "1.2.0",
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
        self._serve_init_task: asyncio.Task | None = None
        self._ws_task: asyncio.Task | None = None
        self._recent_sessions: list[str] = []
        self._loop = asyncio.get_event_loop()
        # 快捷状态与在线时长统计（惰性初始化，兼容测试用 __new__ 构造）
        self.status_store = StatusStore(self.plugin_dir)
        self._status_debounce = NotifyDebounce()
        # WebSocket 实时推送
        self._ws = WsHub(
            config.get("ws_port", 8765),
            config.get("ws_token", "") or "",
        )
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

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """是否管理员会话（admin_umos 白名单）"""
        umos = self._admin_umos()
        return str(event.session) in umos if umos else False

    def _admin_umos(self) -> list[str]:
        v = self.cfg.get("admin_umos", "")
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return list(v or [])

    def _deny(self) -> str:
        umos = self._admin_umos()
        if not umos:
            return (
                "本插件未配置管理员白名单（admin_umos），管理命令不可用。\n"
                "请在插件配置中填写 admin_umos（如 default:GroupMessage:1234567890）后重启 AstrBot。"
            )
        return "你没有执行此命令的权限（不在 admin_umos 白名单内）"

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
            if not self._is_admin(event):
                return self._deny()
            n = self.monitor.reload()
            return f"机器配置已重新加载，共 {n} 台"
        if cmd == "file":
            if not self._is_admin(event):
                return self._deny()
            states = await self.monitor.check_all()
            fmt_arg = tokens[1] if len(tokens) > 1 else None
            det_arg = tokens[2] if len(tokens) > 2 else None
            formats = [fmt_arg] if fmt_arg in FILE_FORMATS else None
            detail = det_arg if det_arg in FILE_DETAILS else None
            done = await self._generate_and_deliver(
                states, formats=formats, detail=detail
            )
            return f"状态文件已生成并送达（{done}）"
        if cmd == "set":
            return await self._handle_status_set(tokens, event)
        if cmd == "words":
            return self._handle_status_words()
        if cmd == "report" and len(tokens) > 1 and str(tokens[1]).isdigit():
            # 带数字参数：在线时长报表（只读查询，对所有人开放）
            return await self._handle_status_report(tokens)
        if cmd == "report":
            if not self._is_admin(event):
                return self._deny()
            states = await self.monitor.check_all()
            await self._broadcast(format_overview(states))
            return "已播报状态报告"
        if cmd == "权限":
            if not self._is_admin(event):
                return self._deny()
            return await self._handle_permission(tokens)
        if cmd.startswith("权限") and len(cmd) > 2:
            # 兼容连写形式：/机器状态 权限允许 bash git push *
            if not self._is_admin(event):
                return self._deny()
            return await self._handle_permission(["权限", cmd[2:]] + tokens[1:])
        if cmd in ("同意", "批准", "allow"):
            if not self._is_admin(event):
                return self._deny()
            return await self._handle_approve(tokens, approve=True)
        if cmd in ("拒绝", "驳回", "deny"):
            if not self._is_admin(event):
                return self._deny()
            return await self._handle_approve(tokens, approve=False)
        if cmd in ("加密串", "encrypt"):
            if not self._is_admin(event):
                return self._deny()
            return await self._handle_encrypt(tokens)
        states = await self.monitor.check_all()
        st = next((s for s in states if s.get("name") == sub), None)
        if st is None:
            names = "、".join(s.get("name", "?") for s in states)
            return f"未找到机器 {sub}，可用机器: {names}"
        return format_detail(st)

    # ---------- 快捷状态（set / words / report N） ----------

    def _get_status_store(self) -> StatusStore:
        """惰性获取状态存储（兼容测试用 __new__ 构造的实例）"""
        store = getattr(self, "status_store", None)
        if store is None:
            store = StatusStore(self.plugin_dir)
            self.status_store = store
        return store

    def _extra_words(self) -> list[str]:
        """配置扩展词库（extra_words），兼容逗号分隔字符串与 list"""
        return self._split_cfg("extra_words", "")

    async def _handle_status_set(self, tokens: list[str], event: AstrMessageEvent) -> str:
        """快捷状态切换：/机器状态 set <状态词>（预设词或任意自定义词，无需审批）"""
        if len(tokens) < 2:
            cur = self._get_status_store().load_status()
            cur_txt = (
                f"当前状态: {cur['status']}（更新于 {cur['updated_at']}）"
                if cur else "当前无状态记录"
            )
            return (
                "用法: /机器状态 set <状态词>\n"
                "预设词: 在线/忙碌/勿扰/隐身/离开/自定义\n"
                "也支持任意自定义词（如 set 摸鱼中），/机器状态 words 查看词库说明\n"
                + cur_txt
            )
        word = " ".join(tokens[1:]).strip()
        if tokens[1].strip() == "自定义" and len(tokens) > 2:
            # 兼容「set 自定义 <任意词>」形式：取后续词为实际状态
            word = " ".join(tokens[2:]).strip()
        if not is_valid_word(word):
            return f"状态词无效或过长（最多 {MAX_WORD_LEN} 字）"
        store = self._get_status_store()
        source = str(event.session)
        changed, prev = store.set_status(word, source)
        logger.info(
            f"状态同步：{source} 设置状态"
            f"「{prev.get('status') if prev else '（无）'}」→「{word}」（变更={changed}）"
        )
        if not changed:
            return (
                f"状态未变化，当前已是「{word}」"
                f"（{prev.get('updated_at', '')} 设置）"
            )
        store.append_event(word, source)
        # 状态变更实时通知（notify_enabled 开启且配置目标时推送，去抖防重复）
        if self.cfg.get("notify_enabled", False):
            await self._notify_status_change(word, source)
        return f"状态已切换为「{word}」"

    def _handle_status_words(self) -> str:
        """查看预设状态词库与说明"""
        words = build_words(self._extra_words())
        lines = ["[状态同步] 快捷状态词库"]
        for w, desc in words.items():
            lines.append(f"- {w}: {desc}")
        cur = self._get_status_store().load_status()
        if cur:
            lines.append(
                f"当前状态: {cur['status']}（更新于 {cur['updated_at']}，来源 {cur['source']}）"
            )
        lines.append("用法: /机器状态 set <状态词>；/机器状态 report <N> 查看在线时长报表")
        return "\n".join(lines)

    async def _handle_status_report(self, tokens: list[str]) -> str:
        """最近 N 天在线时长报表：/机器状态 report [N]（默认 7，范围 1~90）"""
        days = DEFAULT_REPORT_DAYS
        if len(tokens) > 1 and str(tokens[1]).isdigit():
            try:
                days = max(1, min(int(tokens[1]), 90))
            except (TypeError, ValueError):
                days = DEFAULT_REPORT_DAYS
        try:
            return self._get_status_store().report(days)
        except Exception as e:  # noqa: BLE001
            logger.exception("在线时长报表生成失败")
            return f"在线时长报表生成失败: {e}"

    def _notify_targets(self) -> list[str]:
        """状态变更通知目标群 UMO 列表（notify_targets 配置）"""
        v = self.cfg.get("notify_targets", "")
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return [x for x in (v or []) if x]

    def _get_status_debounce(self) -> NotifyDebounce:
        """惰性获取通知去抖器（兼容测试用 __new__ 构造的实例）"""
        db = getattr(self, "_status_debounce", None)
        if db is None:
            db = NotifyDebounce()
            self._status_debounce = db
        return db

    async def _notify_status_change(self, status: str, source: str) -> None:
        """状态变更实时通知（去抖：同状态窗口内只通知一次，不阻塞现有循环）"""
        targets = self._notify_targets()
        if not targets:
            return
        window = _cfg_int(self.cfg, "notify_debounce_seconds", 60)
        if not self._get_status_debounce().should_notify(status, window):
            return
        text = (
            "[状态同步] 状态变更通知\n"
            f"新状态: {status}\n"
            f"来源: {source}\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await self._broadcast_to(targets, text)

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
        # 拒绝时无论是否 always 都响应 reject（remember 仅控制是否记住），
        # 修复拒绝+always 被反转成放行（always）的安全漏洞
        if approve and remember:
            response = "always"
        elif approve:
            response = "once"
        else:
            response = "reject"
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
                        # 解析失败：不能视为已发送，走 send_message 回退
                        return False
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

    async def _broadcast_to(self, umos: list[str], text: str):
        """向指定会话 UMO 列表播报文本（单目标失败不影响其他）"""
        for umo in umos:
            try:
                await self._send_chain(umo, MessageChain([Plain(text)]))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"播报到 {umo} 失败: {e}")

    async def _broadcast(self, text: str):
        await self._broadcast_to(self._report_targets(), text)

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
        if ("file" in delivery or "local" in delivery) and self.cfg.get(
            "status_file_enabled", True
        ):
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
        self.initialize()

    def initialize(self):
        """插件热重载后启动全部后台任务（幂等；done_callback 记录异常防静默死亡）"""
        if not self.cfg.get("enabled", True):
            return
        self.monitor.reload()
        if self._task is None:
            self._task = self._spawn("播报", self._poll_loop)
        if self._change_task is None:
            self._change_task = self._spawn("变化检测", self._change_loop)
        if self._perm_task is None:
            self.perm_monitor.load_cursor()
            self._perm_task = self._spawn("权限监控", self._perm_loop)
        if self.serve is not None and self._serve_task is None:
            if self._serve_init_task is None:
                self._serve_init_task = self._spawn(
                    "serve 初始化", self._init_serve
                )
        if self.cfg.get("ws_enabled", False) and self._ws_task is None:
            if self._ws_start():
                self._ws_task = self._ws._task
                logger.info(f"状态同步：WebSocket 实时推送已启用 (ws_port={self._ws.port})")

    async def _init_serve(self):
        """异步初始化 serve 连接：探测可达则监听，否则进入重连循环（幂等）"""
        try:
            if self._serve_task is not None:
                return
            if await self._probe_serve_async():
                self._serve_task = self._spawn(
                    "serve 监听", self.serve.listen_loop, self._on_perm_request, self._loop
                )
                logger.info(f"状态同步：已连接 opencode serve ({self.serve.base_url})")
            elif self._serve_retry_task is None:
                self._serve_retry_task = self._spawn("serve 重连", self._serve_connect_loop)
                logger.warning("状态同步：opencode serve 暂不可达，后台轮询等待重连")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"状态同步：serve 初始化失败: {e}")

    def _spawn(self, name: str, coro, *args) -> asyncio.Task:
        """创建后台任务并挂 done_callback：记录异常防止静默死亡"""
        task = asyncio.create_task(coro(*args))

        def _done(t: asyncio.Task):
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.warning(f"状态同步：{name}任务异常退出: {exc}")

        task.add_done_callback(_done)
        return task

    def _probe_serve(self) -> bool:
        """同步探测 serve（阻塞调用，调用方自行保证不在事件循环内直接执行）"""
        try:
            return self.serve.probe()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"状态同步：探测 serve 失败: {e}")
            return False

    async def _probe_serve_async(self) -> bool:
        """异步探测 serve（阻塞调用放入线程）"""
        try:
            return await asyncio.to_thread(self.serve.probe)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"状态同步：探测 serve 失败: {e}")
            return False

    def _ws_start(self) -> bool:
        try:
            return self._ws.start()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"状态同步：WebSocket 启动失败: {e}")
            return False

    async def _serve_connect_loop(self):
        """serve 未就绪时轮询探测，成功后转正式监听任务（serve 可能晚于插件启动）"""
        try:
            while self._serve_task is None:
                await asyncio.sleep(10)
                if not self.serve or await self._probe_serve_async():
                    break
            if self.serve and self._serve_task is None:
                self._serve_task = self._spawn(
                    "serve 监听", self.serve.listen_loop, self._on_perm_request, self._loop
                )
                logger.info(f"状态同步：已连接 opencode serve ({self.serve.base_url})")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"状态同步：serve 重试连接失败: {e}")

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self):
        for task in (self._task, self._change_task, self._perm_task, self._serve_task, self._serve_retry_task, self._serve_init_task, self._ws_task):
            if task:
                task.cancel()
        self._task = None
        self._change_task = None
        self._perm_task = None
        self._serve_task = None
        self._serve_retry_task = None
        self._serve_init_task = None
        self._ws_task = None
        await self._ws.stop()
        await self.monitor.close_all()

    async def _perm_loop(self):
        """定期检查 opencode 日志中的权限评估事件并转发"""
        while True:
            try:
                # 每次循环重读配置，支持运行中调整且脏值不杀任务
                interval = max(5, _cfg_int(self.cfg, "permission_check_interval_seconds", 30))
            except Exception:  # noqa: BLE001
                interval = 30
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
            minutes = max(1, _cfg_int(self.cfg, "poll_interval_minutes", 30))
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
                # WebSocket 推送全量状态快照
                if self._ws.running:
                    self._ws.broadcast({
                        "type": "status",
                        "time": datetime.now().isoformat(),
                        "machines": states,
                    })
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("定时状态播报失败")

    async def _change_loop(self):
        """短间隔检测目标程序运行状态变化，变化时立即播报（实时性）"""
        while True:
            interval = max(10, _cfg_int(self.cfg, "state_change_interval_seconds", 60))
            await asyncio.sleep(interval)
            try:
                if not self.cfg.get("state_change_report", False):
                    continue
                if not self._report_targets():
                    continue
                changes = await self.monitor.detect_changes()
                if changes:
                    await self._broadcast("[状态同步] " + "；".join(changes))
                    # WebSocket 实时推送状态变化
                    if self._ws.running:
                        self._ws.broadcast({
                            "type": "state_change",
                            "time": datetime.now().isoformat(),
                            "changes": changes,
                            "machines": await self.monitor.check_all(),
                        })
                    if "on_change" in self._split_cfg(
                        "file_generate_timing", "manual,scheduled,on_change"
                    ):
                        states = await self.monitor.check_all()
                        await self._generate_and_deliver(states, send_text=False)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("状态变化检测失败")
