# astrbot_plugin_status_sync

AstrBot 插件：同步电脑端 opencode / mimocode / openclaw 等程序运行状态，支持本机与远程多机器检测。

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
- 主动查询：`/机器状态` 任意时刻查看实时状态

## 使用方法

| 命令 | 说明 |
| --- | --- |
| `/机器状态` 或 `/sync` | 所有机器状态概览 |
| `/机器状态 <机器名>` | 指定机器详细状态 |
| `/机器状态 file [格式] [粒度]` | 立即生成状态文件并送达（格式 `json/md/txt/csv`，粒度 `summary/full`，可省略按配置） |
| `/机器状态 report` | 立即播报一次状态 |
| `/机器状态 reload` | 重新加载机器配置 |

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

## 插件配置

| 配置项 | 说明 |
| --- | --- |
| `enabled` | 插件总开关 |
| `poll_interval_minutes` | 定时播报间隔（分钟，默认 30） |
| `report_enabled` | 是否定时自动播报 |
| `state_change_report` | 状态变化即时播报（启动/停止通知，默认开） |
| `state_change_interval_seconds` | 状态变化检测间隔（秒，默认 60，最小 10） |
| `report_groups` | 播报目标 UMO（如 `default:GroupMessage:1102410958`），逗号分隔；留空播到最近使用 `/机器状态` 的会话 |
| `status_file_enabled` | 是否启用状态文件输出 |
| `status_file` | 状态文件路径（相对插件目录，默认 `data/status_sync.json`），文件名作为各格式文件的基础名 |
| `file_formats` | 生成的文件格式（`json/md/txt/csv`，逗号分隔，默认全部；留空不生成文件） |
| `file_delivery` | 送达方式（`text`=文本消息、`file`=文件消息、`local`=仅本地保存，逗号分隔，可组合） |
| `file_detail` | 文件内容粒度（`summary`=概览摘要 / `full`=全量详情，默认 summary） |
| `file_generate_timing` | 生成时机（`manual`=手动命令、`scheduled`=定时播报时、`on_change`=状态变化时，逗号分隔，可组合） |
| `machines_config_file` | 机器配置路径（相对插件目录，默认 `data/machines.json`） |

## 依赖

- 本机模式：`psutil`（AstrBot 自带环境一般已装）
- SSH 模式：`paramiko`
- WinRM 模式：`pywinrm`
- HTTP 模式：`requests`（一般已装）
