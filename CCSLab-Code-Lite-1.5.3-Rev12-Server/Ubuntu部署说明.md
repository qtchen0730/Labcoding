# CCSLab Gateway 1.5.3 Rev3 Ubuntu 升级与回滚

本文保留 Rev3 升级的历史背景，源码路径已统一为 Rev12。Rev12 的部署门槛、迁移与回滚以 [Rev12 Ubuntu 操作手册](DEPLOY-REV12.md) 为准。

以下操作由生产管理员在授权维护窗口执行。修订版仍使用 `https://ccslab-code.qtchen.cn` 和现有 `http://127.0.0.1:8444` 回源。升级对象是一个 Gateway 进程。Rev3 将本人改密的最低长度调整为 6 个字符，现有 SQLite 结构与 Rev2 兼容。账号 SQLite、管理员授权验证、模型 API Key、Open WebUI 3006、原 Gateway 8443 和模型服务继续使用现有配置。

## 1. 确认配置与一致性备份

在 Ubuntu 核对监听 8444 的进程由哪个 systemd 服务或脚本启动。记录工作目录、Python 虚拟环境、运行用户、环境文件和启动参数。保留运行在 8443 的原 Gateway 作为原有部署的一部分。命令只用于核对监听身份。

```bash
ss -ltnp '( sport = :8444 or sport = :8443 )'
```

以原 Gateway 用户执行下面的 SQLite 一致性备份。示例中的数据库路径来自当前部署。若启动环境设有其他 `CCSLAB_DATABASE` 路径，以实际配置为准。

```bash
umask 077
ccs_state="$HOME/.local/share/ccslab-gateway"
ccs_backup="$ccs_state/backups/pre-1.5.3-rev3-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$ccs_backup"
python3 - "$ccs_state/accounts.sqlite3" "$ccs_backup/accounts.sqlite3" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

source, target = map(Path, sys.argv[1:])
with target.open('xb'):
    pass
with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as origin:
    with closing(sqlite3.connect(target)) as backup:
        origin.backup(backup)
        if backup.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise SystemExit('SQLite backup quick_check failed')
PY
```

备份命令成功后，确认文件存在并保管好目录。保存原服务的启动配置，以及 `admin-registration.argon2` 和 `accounts.sqlite3.open-webui.argon2`（如已存在）的私有备份。模型 API Key 继续由原路径供 Gateway 读取。

## 2. 更新 8444 网关

1. 上传并解压 `CCSLab-Code-Lite-1.5.3-Rev12-Server.zip` 到 `/home/server/ccslab-gateway`，确认源码位于独立的 `CCSLab-Code-Lite-1.5.3-Rev12-Server/` 目录，其下包含 `server/`，旧目录供回滚。
2. 维护窗口内停止旧的 8444 Gateway。若由 systemd 管理，修改该 Gateway unit 的 `WorkingDirectory`，继续使用同一虚拟环境、服务账号和 `EnvironmentFile`。如改了 unit 文件，执行 `sudo systemctl daemon-reload` 后启动对应的 **Gateway** unit。终端前台运行时，先加载原环境变量，再从新目录运行。下面的示例沿用已有 Python 环境。

   ```bash
   cd /home/server/ccslab-gateway/CCSLab-Code-Lite-1.5.3-Rev12-Server
   /home/server/ccslab-gateway/CCSLab-Code-Lite-1.5.0/.venv/bin/python -m server serve \
     --bind 127.0.0.1 --port 8444
   ```

3. 从另一终端运行 `curl -fsS http://127.0.0.1:8444/health`，确认收到 `{"ok":true}`。在 Windows 运行 `curl.exe -fsS https://ccslab-code.qtchen.cn/health`，用系统证书验证公网路由，然后覆盖安装配套 EXE。用已有管理员验证入口，新进环境的默认最高权限密码是 `000425`。用指定测试账号验证恰好 6 个字符的新密码可以保存，以及会话注销逻辑，避免修改正式用户的密码来验收。

若 CLI 报找不到原管理员或原数据，先核对 `CCSLAB_DATABASE` 和原凭据文件路径，再启动服务。现有账号部署直接复用原文件。

## 3. 数据兼容与回滚

`server/migrations/004_self_password_audit.sql` 在 Gateway 初始化时幂等增加一张本人改密审计表。它记录操作账号和时间。保留旧版 `PRAGMA user_version=2`，便于旧版读回数据库。现有用户、密码哈希和会话可直接复用。用户主动改密后，只撤销该用户的会话。

最高权限密码验证文件位于 SQLite 同目录。首次缺少该文件时，Gateway 初始化 `000425` 的 Argon2id 验证值。已有验证文件继续沿用。当前入口密码修改后，新的验证值会写入该文件，之前所有入口授权随之失效。

回滚时停下 8444 的修订版 Gateway，恢复该进程的旧工作目录和启动参数，在原端口启动并验证健康接口。继续使用原数据库，保留升级期间发生的有效密码变更与审计。Windows 可配套恢复旧版客户端。数据库备份用于经确认的损坏恢复。若退回没有最高权限 API 的 1.5.0，修订版中的入口验证和自助改密会提示网关版本不匹配。
