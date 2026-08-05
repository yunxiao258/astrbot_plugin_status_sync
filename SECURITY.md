# 凭据安全配置指南

SSH 密码、WinRM 密码、HTTP token 等敏感字段**不要明文**写在 `data/machines.json` 里。
本指南覆盖 Windows 宿主 / Linux 裸机 / Docker 三种部署方式的凭据配置方案。

---

## 1. 威胁模型

- 机器配置 `data/machines.json` 与 AstrBot 同机存放。
- Windows 宿主：插件启动会自动执行 `icacls`，把该文件 ACL 收紧为
  **仅当前用户 + SYSTEM 可读**，挡住其他系统用户。
- Linux / Docker：请自行 `chmod 600 machines.json`（见 §5）。
- 但**同权限进程**（被攻破的常驻服务、容器内其他程序）仍可能读到明文。
  所以强烈建议用下面任一方式避免明文落盘。

---

## 2. 三种凭据写法对比

| 写法 | Windows | Linux / Docker | 说明 |
| --- | --- | --- | --- |
| `env:变量名` | 可用 | 可用 | 密码不落盘，**推荐** |
| `file:/容器内路径` | 可用 | 可用 | 密码放独立文件，权限收紧 |
| `dpapi:串` | 可用 | **不可用** | Win 专属加密，仅本机当前用户可解 |
| `base64:...` | 可用 | 可用 | 弱混淆，仅防"扫一眼"，不防恶意读取 |
| 无前缀（明文） | 兼容 | 兼容 | 旧配置，不推荐 |

字段适用范围：`password` / `token` / `private_key` 均支持以上前缀。

---

## 3. Windows 宿主：dpapi / env

**方式 A：环境变量（推荐）**

```powershell
# 设置用户环境变量（永久）
setx SRV_SSH_PWD "MyPass123"
```

`machines.json`：

```json
{
  "name": "Linux服务器",
  "type": "ssh",
  "host": "srv.lan",
  "username": "root",
  "password": "env:SRV_SSH_PWD",
  "os": "linux"
}
```

> 注意：`setx` 只对新进程生效，需要重启 AstrBot 才能读到新变量。

**方式 B：DPAPI 加密串**

在群里对 AstrBot 发送：

```
/机器状态 加密串 MyPass123
```

插件返回：

```
已生成 DPAPI 密文（仅本机当前用户可解密），填入 machines.json 对应字段:
dpapi:AgAAAAMBCP...
```

把该串填入对应字段：

```json
{
  "type": "ssh",
  "host": "192.168.1.20",
  "username": "root",
  "password": "dpapi:AgAAAAMBCP...",
  "os": "linux"
}
```

注意事项：

- 此密文**换用户 / 换机器无法解密**，重装系统或迁移 D 盘用户目录后需重新生成。
- 群里发送的明文密码群友可见、日志可能记录——**正式环境别在群里发明文密码**，
  临时测试可用；正经用请走 `env:` 方式。

**方式 C：私钥（推荐远程 SSH 首选）**

```json
{
  "type": "ssh",
  "host": "srv.lan",
  "username": "root",
  "private_key": "C:/Users/you/.ssh/id_ed25519",
  "os": "linux"
}
```

或把私钥内容放环境变量/文件引用：

```json
"private_key": "env:SRV_SSH_KEY"
```

---

## 4. Linux 裸机（systemd 等）

```bash
# 写入环境变量（以 systemd 为例）
sudo systemctl edit astrbot
```

写入：

```ini
[Service]
Environment=SRV_SSH_PWD=MyPass123
HTTP_API_TOKEN=tok_xxx
```

`machines.json`：

```json
{
  "name": "Linux服务器",
  "type": "ssh",
  "host": "srv.lan",
  "username": "root",
  "password": "env:SRV_SSH_PWD",
  "os": "linux"
}
```

私钥：

```json
"private_key": "/home/ubuntu/.ssh/id_ed25519"
```

---

## 5. Docker 宿主（Linux + Docker，最常用）

### 方式 A：docker-compose 注入环境变量（推荐）

```yaml
# docker-compose.yml
services:
  astrbot:
    image: soulter/astrbot:latest
    environment:
      - SRV_SSH_PWD=MyPass123
      - SRV_WINRM_PWD=AnotherPass
      - HTTP_API_TOKEN=tok_xxx
    volumes:
      - ./data:/AstrBot/data
```

改完执行 `docker compose up -d`（环境变量重启容器后生效），再在群里 `/机器状态 reload`。

> 多个变量管理：可把秘密放进同目录 `.env` 文件，compose 会自动加载，
> 并配合 `./data` 卷内的 `machines.json` 引用变量名。

### 方式 B：密码文件 + volume 挂载

宿主机：

```bash
mkdir -p ./secrets && chmod 700 ./secrets
echo -n 'MyPass123' > ./secrets/ssh_pwd && chmod 600 ./secrets/ssh_pwd
```

compose 挂载进去：

```yaml
    volumes:
      - ./data:/AstrBot/data
      - ./secrets:/run/secrets:ro
```

`machines.json`（注意用**容器内路径**）：

```json
{
  "name": "Linux服务器",
  "type": "ssh",
  "host": "srv.lan",
  "username": "root",
  "password": "file:/run/secrets/ssh_pwd",
  "os": "linux"
}
```

### 方式 C：私钥 + volume 挂载

```yaml
    volumes:
      - ./data:/AstrBot/data
      - ~/.ssh:/root/.ssh:ro
```

```json
{
  "type": "ssh",
  "host": "srv.lan",
  "username": "root",
  "private_key": "/root/.ssh/id_ed25519",
  "os": "linux"
}
```

> Docker 部署要点：
> - `file:` 和 `private_key` 都填 **容器内路径**（按挂载点换算），不是宿主机路径。
> - 域名解析走容器 DNS（`/etc/resolv.conf`），`host` 直接填域名即可。
> - 容器网络访问宿主机/局域网其他机器用宿主机 IP 或局域网 IP。

---

## 6. 完整 machines.json 示例（含三种远程类型）

```json
{
  "machines": [
    {
      "name": "本机服务器",
      "type": "local",
      "os": "windows",
      "targets": [
        { "name": "opencode", "processes": ["opencode", "opencode.exe"] }
      ]
    },
    {
      "name": "Linux服务器",
      "type": "ssh",
      "host": "srv.lan",
      "username": "root",
      "password": "env:SRV_SSH_PWD",
      "os": "linux",
      "targets": [
        { "name": "opencode", "processes": ["opencode"] }
      ]
    },
    {
      "name": "Windows服务器",
      "type": "winrm",
      "host": "win.lan",
      "username": "Administrator",
      "password": "env:SRV_WINRM_PWD",
      "https": false,
      "targets": [
        { "name": "opencode", "processes": ["opencode.exe"] }
      ]
    },
    {
      "name": "API网关",
      "type": "http",
      "base_url": "http://10.0.0.5:9000",
      "token": "env:HTTP_API_TOKEN",
      "targets": [
        { "name": "opencode", "processes": ["opencode"] }
      ]
    }
  ]
}
```

---

## 7. 文件权限加固

**Windows**：插件启动/`reload` 时自动执行，无需手动：

```
icacls machines.json /inheritance:r /grant:r "当前用户:R" "SYSTEM:R"
```

**Linux / Docker**（手动）：

```bash
chmod 600 data/machines.json
chmod 700 data
# 私钥与密码文件同样收紧
chmod 600 ~/.ssh/id_ed25519 secrets/ssh_pwd
```

---

## 8. 生效与验证

1. 修改 `data/machines.json` 后，群里发送 `/机器状态 reload`。
2. 发送 `/机器状态` 查看概览，远程机器应显示目标程序运行状态；
   若显示"检测失败"可 `/机器状态 <机器名>` 看具体错误（凭据错误提示如
   "Authentication failed"、"未配置密码或私钥"）。
3. 环境变量方式修改后，**重启 AstrBot / 容器**才生效。

## 9. 最佳实践清单

- [ ] 远程凭据一律用 `env:`，其次 `file:` + 权限收紧，Windows 才用 `dpapi:`
- [ ] 私钥优先于密码（SSH 免密 + 安全）
- [ ] `data/` 目录保持 .gitignore（已配置，含机器配置的仓库不要上传）
- [ ] 定期轮换密码/token
- [ ] 别把明文密码发到群里