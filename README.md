# astrbot_plugin_status_sync

AstrBot 插件：同步电脑端 opencode / mimocode / openclaw 等程序运行状态，支持本机与远程多机器检测。

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
- 权限远程控制：日志权限事件通知 + 远程规则管理 + serve 模式实时批准/拒绝
- 凭据安全：env/file/dpapi 引用，ACL 自动收紧，keys 不落明文
- 支持本机与远程（SSH/WinRM/HTTP）多机器，host 支持 IP 与域名
- 主动查询：`/机器状态` 任意时刻查看实时状态

## 使用方法

| 命令 | 说明 |
| --- | --- |
| `/机器状态` 或 `/sync` | 所有机器状态概览 |
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

> 命令名遵循 AstrBot 常见命令风格（不带特殊前缀也可触发），与其他插件不冲突。

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
   `~/.config/opencode/opencode.jsonc` 的 permission 规则（opencode 自动热加载），
   对后续请求生效——可在手机上远程"同意"或"拒绝"某类操作。
3. **serve 模式实时批准**：配置 `opencode_serve_url`（如 `http://127.0.0.1:4096`）后，
   插件连接 `opencode serve` 的 HTTP API，权限请求发生时立即转发到群，
   管理员回复 `/机器状态 同意 <ID>` 或 `/机器状态 拒绝 <ID>` 直接处理**当前挂起**的请求。

## 插件配置

| 配置项 | 说明 |
| --- | --- |
| `enabled` | 插件总开关 |
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

## 依赖

- 本机模式：`psutil`（AstrBot 自带环境一般已装）
- SSH 模式：`paramiko`
- WinRM 模式：`pywinrm`
- HTTP 模式：`requests`（一般已装）
