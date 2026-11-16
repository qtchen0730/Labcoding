# Rev12 Ubuntu 部署、迁移、备份与回滚手册

本文件是待管理员审核的操作方案，**本轮未执行生产命令**。完整 HTTP 与真实 llama.cpp 验收当前 BLOCKED，上线门槛尚未满足。旧 `Ubuntu部署说明.md` 和 `server/DEPLOYMENT.md` 保留历史背景，Rev12 以本文件为准。

## 1. 门槛与现场清点

先安装固定依赖，完成完整 HTTP 回归及 `REAL-MODEL-ACCEPTANCE.md`。任何计量或鉴权 FAIL/BLOCKED 均停止切换，不能仅因单元测试通过而上线。

管理员只读核对实际 Gateway 的 systemd unit、ExecStart、WorkingDirectory、运行账号、venv、EnvironmentFile、监听端口和源码 commit；不凭历史说明猜测 8443/8444 属于目标进程。记录当前健康 URL、路由、模型 ID、llama.cpp 构建、模板摘要、每槽上下文、并发和输出上限，保持这些参数和 Cloudflare 不变。

核对环境中的配置路径，不把密钥值写入终端/日志：`CCSLAB_DATABASE`、`CCSLAB_ADMIN_REGISTRATION_CODE_FILE`、`<数据库名>.open-webui.argon2`、`CCSLAB_LLAMA_API_KEY_FILE`、`CCSLAB_LLAMA_URL`、`CCSLAB_MODEL_ID`、`CCSLAB_OUTPUT_TOKEN_LIMIT`。授权码和 WebUI 验证文件必须独立。保留已有遥测/模型控制路径，不执行 apply 或重启模型。

## 2. 独立目录与依赖

将 Rev12-Server 包解压到独立版本目录，不覆盖旧版本。以普通专用服务账号建立 Python 3.12 venv。以下变量均须由管理员先填入审核后的绝对路径；示例不替代现场核查。

```sh
python3.12 -m venv "$CCS_NEW_VENV"
"$CCS_NEW_VENV/bin/python" -m pip install --cache-dir "$CCS_CACHE" -r server/requirements.txt
"$CCS_NEW_VENV/bin/python" -m pip check
"$CCS_NEW_VENV/bin/python" -m pip freeze --all > "$CCS_EVIDENCE/pip-freeze.txt"
```

固定顶层版本保持 FastAPI 0.163.1 / Uvicorn 0.54.0 / HTTPX 0.28.1 / argon2-cffi 25.1.0。下载受限时由可信联网环境生成含传递依赖的 wheelhouse、SHA256 与实际 freeze，再 `pip install --no-index --find-links <wheelhouse> -r server/requirements.txt`。不替换旧版本，不伪造传递依赖 lock。

在独立 SQLite、合成管理员/普通账号和 loopback 测试端口执行完整测试，设置可用 OpenSSL，HTTPS 测试不能因缺失工具而记为通过。在服务端目录运行：

```sh
"$CCS_NEW_VENV/bin/python" -B -m unittest discover -s server -t . -p 'test_*.py' -v
```

保存原始日志、退出码和起止时间。再按确认预算接入当前 llama.cpp 做真实计量。Mock 协议测试不能替代真实模型。本轮未取得依赖和真实模型配置，不宣称这些步骤已经通过。

## 3. 维护窗口一致性备份

另行获得生产授权后，仅排空并停止目标 **Gateway**，确认不再接收推理；不停止 llama.cpp、WebUI、并行 Gateway 或 Cloudflare。在途请求无最终 usage 时保留待核对，禁止当零退款。

由原服务账号 `umask 077`，建立全新权限 0700 备份目录，保存：当前代码/制品、commit/SHA256、freeze、unit/环境与启动参数；SQLite 一致性快照及 quick_check、外键检查、user_version、表/索引结构和关键表行数；同一停写窗口的授权码及 WebUI 验证文件。模型 Key 和 TLS 私钥如需备份，放受限存储且权限 0600，不进 Git、报告或普通附件。

SQLite 用 backup API，不只复制运行中 WAL 模式主库。源/目标由管理员确认，目标必须不存在：

```sh
"$CCS_NEW_VENV/bin/python" - "$CCS_DATABASE" "$CCS_BACKUP/accounts.sqlite3" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3, sys
source, target = map(Path, sys.argv[1:])
with target.open('xb'):
    pass
with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as origin:
    with closing(sqlite3.connect(target)) as backup:
        origin.backup(backup)
        assert backup.execute('PRAGMA quick_check').fetchone() == ('ok',)
        assert backup.execute('PRAGMA foreign_key_check').fetchall() == []
print('SQLite backup verified')
PY
```

先在备份副本演练迁移与恢复，核对用户、哈希、会话和审计；备份可恢复后才能触碰生产库。数据库与验证文件须来自同一停写窗口。

## 4. 迁移与切换

Rev12 相对当前实际 Rev11 **没有新增结构迁移**。001_accounts、003_session_roles 保持 `user_version=2`；002_model_control 单独幂等迁移；004 增加本人改密审计；005 增加额度、请求账本、设置审计及 pending 索引。旧用户缺少额度行时默认不限额，保留旧哈希、会话和审计。

004/005 没有提升 user_version，旧代码能读不代表仍执行额度策略。较早生产库必须在副本上跑真实 Accounts.migrate 两次及 CLI 入口，比较原记录、integrity/foreign keys。本机该完整路径因 Argon2 缺失 BLOCKED，不以静态 SQL 代替验收。

```sh
"$CCS_NEW_VENV/bin/python" -m server --database "$CCS_DATABASE" migrate
```

不重新 init-admin、不重置已有授权码或真实用户密码。沿用原环境、数据库、Key 和监听/TLS 参数，仅改变已确认的目标 Gateway 代码/venv 路径。CLI 无 TLS 时只监听 127.0.0.1，公网继续可信 HTTPS；保持单 worker，不为验收改变生产 worker 或模型参数。

经批准修改 unit 后才 daemon-reload/start 目标 Gateway。分别验证原回源和公网健康，用独立账号验收权限、空码降级、授权码升级、旧令牌失效、额度发放/扣减/归零拒绝/恢复和流式最终统计。负载必须在已确认预算内；计量或鉴权异常立即停止新请求并回滚。

## 5. 回滚

停止新 Gateway，保留失败日志、当前数据库一致性备份、WAL 状态、未结算预留和验证文件。恢复记录的旧代码/venv/unit 路径及原参数。

- 回到精确 Rev11：结构兼容，但有已确认的流式计量缺陷；先停止限额推理或提供明确的兼容补丁/核账方案，再决定恢复流量。
- 回到没有额度的旧 Gateway：会忽略额度表，**不得向要求限额的用户开放模型流量**。保持 Gateway 停止或既有受控入口维护隔离。健康检查恢复不等于计费安全。
- DB 未损坏时保留升级期间有效密码、发放、消费及审计，不因代码回滚而盲目恢复旧库。
- 必须恢复损坏 DB 时，先停写并归档当前 DB/-wal/-shm，独立验证恢复副本及同窗口验证文件。旧快照会丢失窗口后消费/密码变更，需额外明确批准，先核账记录影响。禁止把新库遗留 WAL 应用于旧快照，禁止手工清零预留。

回滚后用独立账号重验会话、权限和计量。保留原备份、失败版本和账本，按既有保留策略清理。

## 6. Windows 配套

先用便携包或独立路径验证安装。原地升级前退出应用，程序目录含自建文件时先移出，不能强制删除。用户数据目录保持原样。安装父目录中的 `.transaction`、`.ccslab-setup-<guid>.old` 是恢复工件；失败时关闭占用进程后重试，保留唯一有效副本，不改安全设置。持久空 `.lock` 存在本身不代表仍有安装进程。

旧客户端忽略新增 quota 字段，额度执行以 Gateway 为准；不认识 `token_quota_reserved` 时仍收到 409 和可读 detail。新版连接缺少 quota 的旧 Gateway 显示统计不可用，不能当作有余额。门槛解除前不覆盖正式客户端。
