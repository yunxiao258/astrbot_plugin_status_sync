# astrbot_plugin_status_sync

AstrBot 插件：同步电脑端 opencode / mimocode / openclaw 等程序运行状态，支持本机与远程多机器检测。版本 **1.2.0**，许可证 **MIT**。

> 凭据配置（Windows / Linux / Docker 三套方案）见 [SECURITY.md](SECURITY.md)。

## 功能

- 进程检测：各程序是否运行（按进程名/命令行匹配，支持正则）
- 资源占用：CPU 使用率、内存占用（系统级 + 进程级）
- 当前活动/最近任务：CLI 命令输出（如 `opencode --version`）
- 日志摘要：读取程序日志最后 N 行（目录自动取最新文件），概览中展示"最近活动"
- 定时自动播报（默认每 30 分钟，可配置）
- 状态变化即时播报：程序启动/停止立即通知群（默认每 60 秒检测一次，可配置）
- 状态文件多格式输出：JSON / Markdown / TXT / CSV，可组合
- 状态送达方式可组合：文本消息、文件消息、仅本地保存
- 状态生成时机可组合：手动命令、定时播报、状态变化时
- 快捷状态：`set` 一键切换（在线/忙碌/勿扰/隐身/离开/自定义词），`words` 查看词库，词库可配置扩展
- 在线时长统计：按天报表（每日总时长、各状态占比、最长连续在线），数据保留最近 90 天
- 状态变更通知：状态切换实时推送到指定群（去抖防重复）
- 权限远程控制：日志权限事件通知 + 远程规则管理 + serve 模式实时批准/拒绝
- 凭据安全：env/file/dpapi 引用，ACL 自动收紧，keys 不落明文
- WebSocket 实时推送：状态变化与定时快照推送给桌面端订阅者（需 `websockets` 库）
- 支持本机与远程（SSH/WinRM/HTTP）多机器，host 支持 IP 与域名
- 主动查询：`/机器状态` 任意时刻查看实时状态

## 使用方法

| 命令 | 说明 |
| --- | --- |
| `/机器状态` 或 `/sync` | 所有机器状态概览 |
| `/机器状态 <机器名>` | 查看指定机器详细状态（进程/CLI/日志） |
| `/机器状态 file [格式] [粒度]` | 立即生成状态文件并送达（格式 `json/md/txt/csv`，粒度 `summary/full`，可省略按配置） |
| `/机器状态 report` | 立即播报一次状态 |
| `/机器状态 reload` | 重新加载机器配置 |
| `/机器状态 权限` | 查看当前 opencode 权限规则 |
| `/机器状态 权限允许/拒绝/询问 <工具> <模式>` | 远程添加权限规则（如 `权限允许 bash git push *`） |
| `/机器状态 权限删除 <工具> <模式>` | 删除一条规则 |
| `/机器状态 权限清空` | 清空全部规则（恢复默认） |
| `/机器状态 权限事件` | 查看最近权限事件 |
| `/机器状态 同意 <ID>` / `拒绝 <ID>` | serve 模式下批准/拒绝挂起的权限请求（追加 `always` 表示记住） |
| `/机器状态 加密串 <明文>` | 生成 DPAPI 密文，供凭据字段使用（仅本机当前用户可解） |
| `/机器状态 set <状态词>` | 快捷切换状态：预设词（在线/忙碌/勿扰/隐身/离开/自定义）或任意自定义词（如 `set 摸鱼中`），无需审批 |
| `/机器状态 words` | 查看预设状态词库与说明（含配置扩展词与当前状态） |
| `/机器状态 report <N>` | 最近 N 天（默认 7，范围 1~90）在线时长报表：每日总时长、各状态占比、最长连续在线 |

> **权限说明**：概览、机器详情、`set`、`words`、`report <N>` 等只读/常规操作对所有人开放；`file`、`report`、`reload`、`权限*`、`同意/拒绝`、`加密串` 等管理命令仅限 `admin_umos` 白名单会话（未配置时全部管理命令不可用并提示配置）。
>
> 命令名遵循 AstrBot 常见命令风格（不带特殊前缀也可触发），与其他插件不冲突。

## 快捷状态与在线时长

### 状态词库（set / words）

- 内置预设词库：**在线**（正常工作，可接受任务）/ **忙碌**（正在处理任务，尽量勿扰）/ **勿扰**（请勿打扰）/ **隐身**（在线但对他人隐藏）/ **离开**（暂时离开）/ **自定义**（自定义状态词）
- `extra_words` 配置可扩展词库，支持纯词（说明默认为"配置扩展词"）或「词:说明」形式，如 `摸鱼:休息一下,开会:会议中`
- `/机器状态 set <任意词>` 支持任意自定义词（最长 20 字，防刷屏）；`set 自定义 <任意词>` 为兼容写法
- 同一状态连续设置不重复写入、不产生新事件

### 在线时长统计（report N）

- 每次状态切换追加一条历史事件（时间戳 + 状态词 + 来源会话），按自然日拆分统计
- 跨天场景正确归属：凌晨切换、设备重启后状态持续到次日，均按天切开计入各自然日
- 报表内容：每日总时长、各状态时长与占比、最长连续在线（仅统计「在线」状态）、合计
- 历史数据自动保留最近 90 天（裁剪旧数据防膨胀），报表支持 1~90 天窗口

### 状态变更通知

- `notify_enabled` 开启且配置 `notify_targets` 后，状态切换实时推送到指定群
- 通知内容：新状态、来源会话、时间
- 去抖机制：同一状态在 `notify_debounce_seconds`（默认 60 秒）窗口内只通知一次，不阻塞现有播报/检测循环

## 机器配置

编辑 `data/machines.json`（不存在时使用内置默认配置，检测本机 opencode/mimocode/openclaw）：

```json
{
  "machines": [
    {
      "name": "服务器A",
      "type": "local",
      "os": "windows",
      "targets": [
        {
          "name": "opencode",
          "processes": ["opencode", "opencode.exe"],
          "cli": { "command": "opencode --version", "timeout": 15 },
          "logs": [{ "path": "%USERPROFILE%/.local/share/opencode/log", "lines": 5 }]
        }
      ]
    },
    {
      "name": "服务器B",
      "type": "ssh",
      "os": "linux",
      "host": "192.168.1.10",
      "port": 22,
      "username": "root",
      "password": "",
      "private_key": "~/.ssh/id_rsa",
      "targets": [
        { "name": "mimocode", "processes": ["mimocode"] }
      ]
    }
  ]
}
```

### 机器类型

| type | 说明 |
| --- | --- |
| `local` | 本机检测（psutil），无需连接参数 |
| `ssh` | SSH 远程检测（需 `pip install paramiko`） |
| `winrm` | WinRM 远程检测（需 `pip install pywinrm`），参数 `host/port(5985)/username/password/https` |
| `http` | 拉取 `GET {base_url}/status` 返回的 JSON 状态（需远程端自行提供该接口），参数 `base_url/token` |

### target 字段

| 字段 | 说明 |
| --- | --- |
| `name` | 程序名（报告展示用） |
| `processes` | 进程匹配列表：进程名或命令行子串（大小写不敏感）；以 `^` 开头视为正则 |
| `cli` | 可选，CLI 检测命令及其超时 |
| `logs` | 可选，日志文件路径列表（`~`/`%USERPROFILE%` 自动展开，目录取最新文件） |

### 远程机器系统

`os` 字段：`windows`（默认，PowerShell 检测脚本）或 `linux`（bash 检测脚本）。ssh/winrm 模式下生效。

### host 字段

`host` 支持 IP 与**域名**（自动 DNS 解析）；可带 `ssh://` / `http(s)://` 等前缀与端口（自动清洗）。HTTP 模式的 `base_url` 直接填完整 URL（域名亦可）。

## 凭据安全（重要）

SSH 密码、WinRM 密码、HTTP token 等敏感字段**不要明文**写在 `data/machines.json` 里。
插件启动时会自动用 Windows ACL 收紧该文件权限（仅当前用户与 SYSTEM 可读），但同权限进程仍可能读取。

字段支持以下前缀（`password` / `token` / `private_key` 通用）：

| 前缀 | 说明 |
| --- | --- |
| `env:VAR_NAME` | 从环境变量读取（推荐，密码不落盘） |
| `file:路径` | 从外部文件读取（文件可放在任意识别安全的目录） |
| `dpapi:一串base64` | Windows DPAPI 密文，仅本机当前用户可解密（用 `/机器状态 加密串 <明文>` 生成） |
| `base64:...` | base64 弱混淆（仅防扫一眼，不防恶意读取） |
| 无前缀 | 原样使用（兼容旧配置；私钥字段为文件路径时保持原样） |

`private_key` 额外支持：直接填私钥文件路径，或填 `env:`/`file:` 引用私钥内容（自动识别 RSA/ECDSA/Ed25519）。

示例：

```json
{
  "name": "服务器B",
  "type": "ssh",
  "host": "ssh://vpn.srv.lan",
  "username": "root",
  "password": "env:SRV_SSH_PASSWORD",
  "targets": [{ "name": "mimocode", "processes": ["mimocode"] }]
}
```

> 安全提示：机器配置与 AstrBot 数据同目录是常规做法，但任何能读取该文件的本机进程
> （如被攻破的常驻服务）都能拿到明文凭据；请优先使用 `env:`/`dpapi:` 形式，并定期轮换。

## 权限远程控制

三层能力：

1. **日志事件通知**：监控 opencode 日志中的权限评估事件（`message=evaluated permission=...`），
   按 `permission_forward_level` 配置转发到群（默认 `deny,ask`：只通知被阻止与请求批准的事件）。
2. **远程规则管理**：`/机器状态 权限允许/拒绝/询问 <工具> <模式>` 直接修改
   `~/.config/opencode/opencode.jsonc` 的 permission 规则（保留原文件注释，opencode 自动热加载），
   对后续请求生效——可在手机上远程"同意"或"拒绝"某类操作。
3. **serve 模式实时批准**：配置 `opencode_serve_url`（如 `http://127.0.0.1:4096`）后，
   插件连接 `opencode serve` 的 HTTP API（可选 Basic Auth），权限请求发生时立即转发到群，
   管理员回复 `/机器状态 同意 <ID>` 或 `/机器状态 拒绝 <ID>` 直接处理**当前挂起**的请求。
   服务端未就绪时自动后台重连；日志 `asking id=` 行（带权限 ID）也会桥接为挂起请求供群内审批。

## WebSocket 实时推送

配置 `ws_enabled` 开启后，插件在 `ws_port`（默认 8765）启动 WebSocket 广播服务（需 `pip install websockets`）：

- 客户端连接：`ws://<astrbot主机>:<ws_port>/?token=<ws_token>`（未配置 `ws_token` 时本机内网可直连，不鉴权）
- 推送内容：定时播报时的全量状态快照（`type: status`）与状态变化事件（`type: state_change`，含变化描述与全量状态）
- token 校验使用常量时间比较，支持 `?token=` 与 `?access_token=` 两种参数形式

## 与 astrbot_plugin_remote_task 联动

配合 [astrbot_plugin_remote_task](https://github.com/yunxiao258/astrbot_plugin_remote_task)
（群内下发任务给 opencode 执行），本插件提供**任务权限现场审批闭环**：

### 场景

remote_task 下发的任务执行中，opencode 若需要对某工具请求权限（`ask`），
会话会挂起等待；此时通过本插件在群内直接批准，任务自动继续，无需登录机器。

### 流程

1. remote_task 任务触发 `ask` 权限 → opencode 挂起该请求，同时写入权限日志
2. 本插件轮询日志捕获 `asking id=per_xxx ...` 行，把权限 ID 桥接为挂起请求，
   并向群播报 `[权限] ... 权限ID: per_xxx`，提示「回复 同意 <ID> 现场放行」
3. 管理员回复 `/机器状态 同意 <ID>`（或 `/机器状态 拒绝 <ID>`）
4. 本插件调用 `POST /session/{sid}/permissions/{id}` 放行/拒绝
5. 任务继续执行，remote_task 随后广播完成结果

### 前提

- 本插件与 remote_task **共用同一个 opencode serve**（`opencode_serve_url`
  与 remote_task 的 `serve_url` 一致，如 `http://127.0.0.1:4096`）
- `permission_report` 保持开启；`permission_forward_level` 包含 `ask`
- opencode 全局配置 `~/.config/opencode/opencode.jsonc` 中相关工具为
  `ask`（默认敏感工具即 ask；也可用 `/机器状态 权限询问 <工具> *` 显式设置）
- 会话归属按"最近创建的 serve 会话"匹配（remote_task 每次任务新建会话，
  因此单任务场景始终准确；并发多任务时取最近一个）

### 说明

- 日志桥接解决的是 serve 不推送 `permission.request` SSE 事件的兼容问题，
  通过 opencode 日志 `asking id=` 行拿到权限 ID，实现与「远程规则管理」、
  「serve 模式实时批准」等效的群内审批
- 只播报不审批时：本插件单独使用也可监控/通知所有 opencode 权限事件
- 远程规则管理（`权限允许/拒绝/询问`）对**后续**请求生效（opencode 热加载），
  与「现场放行」互补

## 插件配置

| 配置项 | 说明 |
| --- | --- |
| `enabled` | 插件总开关 |
| `admin_umos` | 管理员会话 UMO 白名单（`file`/`report`/`reload`/`权限*`/`同意/拒绝`/`加密串` 等管理命令仅限此列表），逗号分隔；留空则管理命令全部不可用 |
| `poll_interval_minutes` | 定时播报间隔（分钟，默认 30） |
| `report_enabled` | 是否定时自动播报 |
| `state_change_report` | 状态变化即时播报（启动/停止通知，默认开） |
| `state_change_interval_seconds` | 状态变化检测间隔（秒，默认 60，最小 10） |
| `report_groups` | 播报目标 UMO（如 `default:GroupMessage:1234567890`），逗号分隔；留空播到最近使用 `/机器状态` 的会话 |
| `status_file_enabled` | 是否启用状态文件输出 |
| `status_file` | 状态文件路径（相对插件目录，默认 `data/status_sync.json`），文件名作为各格式文件的基础名 |
| `file_formats` | 生成的文件格式（`json/md/txt/csv`，逗号分隔，默认全部；留空不生成文件） |
| `file_delivery` | 送达方式（`text`=文本消息、`file`=文件消息、`local`=仅本地保存，逗号分隔，可组合） |
| `file_detail` | 文件内容粒度（`summary`=概览摘要 / `full`=全量详情，默认 summary） |
| `file_generate_timing` | 生成时机（`manual`=手动命令、`scheduled`=定时播报时、`on_change`=状态变化时，逗号分隔，可组合） |
| `permission_report` | 是否把权限事件转发到群（默认开） |
| `permission_forward_level` | 日志权限事件转发级别（`deny`/`ask`/`allow`，逗号分隔，默认 `deny,ask`） |
| `permission_check_interval_seconds` | 权限日志检测间隔（秒，默认 30，最小 5） |
| `permission_event_buffer` | 内存保留的权限事件条数（默认 30） |
| `opencode_config_file` | opencode 全局配置文件路径（默认 `%USERPROFILE%/.config/opencode/opencode.jsonc`） |
| `opencode_serve_url` | opencode serve 服务地址（如 `http://127.0.0.1:4096`），留空不启用实时批准 |
| `opencode_serve_username` / `opencode_serve_password` | serve HTTP Basic Auth（可选） |
| `machines_config_file` | 机器配置路径（相对插件目录，默认 `data/machines.json`） |
| `extra_words` | 快捷状态词库扩展（逗号分隔，支持纯词或「词:说明」，如 `摸鱼:休息一下`） |
| `notify_enabled` | 状态变更实时通知开关（状态切换时推送，默认关） |
| `notify_targets` | 状态变更通知目标群 UMO（如 `default:GroupMessage:1234567890`），逗号分隔 |
| `notify_debounce_seconds` | 状态通知去抖窗口（秒，默认 60）：同状态在窗口内只通知一次 |
| `ws_enabled` | 是否启用 WebSocket 实时推送（需要 `websockets` 库） |
| `ws_port` | WebSocket 监听端口（默认 8765） |
| `ws_token` | WebSocket 访问令牌（客户端需 `?token=<令牌>` 连接；留空不鉴权，仅建议内网使用） |

## 数据存储

插件数据保存在插件目录 `data/` 下：

| 文件 | 说明 |
| --- | --- |
| `machines.json` | 机器配置（不存在时用内置默认配置；启动时自动收紧 Windows ACL） |
| `status_sync.json` 等 | 状态文件输出（格式由 `file_formats` 决定，文件名来自 `status_file` 基础名） |
| `user_status.json` | 当前快捷状态（状态词/更新时间/来源，原子写） |
| `status_history.json` | 状态切换历史事件（保留最近 90 天，原子写，供在线时长报表） |
| `perm_cursor.json` | 权限日志监控游标（各日志文件的读取偏移，断点续读） |

## 依赖

- 本机模式：`psutil`（AstrBot 自带环境一般已装）
- SSH 模式：`paramiko`
- WinRM 模式：`pywinrm`
- HTTP 模式：`requests`（一般已装）
- WebSocket 推送：`websockets`（可选，未安装时仅提示不可用）

## 更新记录

- **1.2.0**：新增快捷状态词库（`set`/`words`，内置 6 词 + `extra_words` 扩展）、在线时长统计（`report <N>`，跨天拆分、保留 90 天）、状态变更通知（`notify_targets` 推送 + 去抖防重复）
- **1.1.3**：修复 reload 连接泄漏、WinRM 加锁与超时、消息解析失败回退发送
- **1.1.2**：新增 WebSocket 实时推送（状态变化 + 定时快照，token 鉴权）
- **1.1.1**：修复 SSH 读取无超时挂死、Windows JSON 截断解析崩溃
- **1.1.0**：安全加固：`admin_umos` 白名单，管理命令仅限管理员
- **1.0.0**：初始版本：多机器状态检测、多格式状态文件与群播报

## 开发与测试

```bash
python -m unittest test_status_sync test_user_status test_perm test_ws_server test_integration -v
```

测试共 74 个，覆盖命令路由与权限、快捷状态词库与在线时长统计、权限事件解析与规则管理、WebSocket 鉴权与推送、端到端集成流程。