# Rev9 用户 Token 额度及角色降级

独立服务端源码交付，未部署、未修改生产数据库或模型参数。Windows 界面及加密登录可独立使用；额度及免授权码降级需要配套更新 Gateway。全量 HTTP 测试需要安装 `server/requirements.txt`。

**Partially Verified**：本机 SQLite 额度账本、并发预留、用量解析与上下文错误共 9 项检查通过，现有模型管理 8 项检查通过；FastAPI、HTTPX、Argon2 等依赖缺失且下载受环境限制，新增额度 HTTP 测试及完整账号/角色联调尚未执行通过。生产原生分词与计费一致性也未验证，不能据此声明服务端已完成验收。

## 接口及兼容

- `/api/me`、登录返回的 `user` 和审批列表用户增加 `quota` 对象。旧客户端忽略新字段，限额仍由服务端执行。旧用户默认不限额，不返回虚假的百分比。
- 新接口：`POST /api/admin/users/{id}/quota`，仅当前有效管理员、仅普通用户。请求 `{"mode":"limited","limit_tokens":100000}` 或 `{"mode":"unlimited","limit_tokens":null}`。客户端输入 100k 对应 100000 Token，支持三位小数及 0。
- **保存即重新发放所填额度**，当前批次已用量从零开始；之前请求及管理员设置保留审计。运行中的旧请求按旧批次结算，不消耗新批次额度。
- 幂等迁移 `005_token_quotas.sql` 新增额度、请求账本、设置审计三张表及索引，保持 `user_version=2`，不重建原账号及会话。本轮只对隔离测试库执行，生产迁移应先备份。
- `/api/session/role` 仍接受 `role`、`authorization_code` 两字段。目标 `user` 接受空授权码，验证会话及账号后降级并轮换会话；目标 `admin` 继续验证授权码。普通账号不能提升权限。
- 新客户端连接旧 Gateway：缺少 quota 时显示“额度不可用”；额度接口 404/405 显示升级提示。旧 Gateway 仍可能拒绝空授权码降级，客户端不重新弹授权码或跳过检查。

## 计量及错误

所有 `/v1/chat/completions` 请求（含工具后续轮及压缩请求）统一计量。只按实际 `usage.prompt_tokens + usage.completion_tokens` 扣减；缓存命中数包含在输入中，不重复加算。账本不记录对话、文件内容、密码或令牌。

限额请求先调用同一运行模型的 `/apply-template`、`/tokenize`，计算含系统、历史、工具和项目内容的完整提示，原子预留输入加输出上限。分词接口不可用时明确拒绝限额请求，不使用字符估算；不限额请求保持原路径。预留不足时拒绝，不截断代码或指令，不缩减输出上限；正余额也可能不足以覆盖当前请求。

流式请求包含 usage 并保留 usage-only 帧；非流式读取 usage。已确认用量结算后释放多余预留。明确的上游参数拒绝释放预留；取消、断线、未知服务错误、缺失或冲突统计保留“待核对”预留并阻止限额新请求。管理员核对后可重新发放额度。服务重启不自动退还未结算请求，不将预留伪装为实际消费。

额度错误与上下文错误区分，带脱敏 request_id、阶段、已取得的输入计数及输出预留；未知值不填造数字。客户端约每 4.5 秒及模型请求/任务结束时刷新，断网显示待刷新。百分比包含活动请求预留，待核对单独说明，正余额小于 0.1% 时不会显示为 0%。

原生接口依据：[llama.cpp 官方文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。生产版本的模板/分词及 usage 一致性仍需隔离联调，不能用协议 fixture 代替真实模型验证。

## 验证及回滚

仓库根创建 `.work/venv` 后，在本服务端目录运行：

```powershell
..\.work\venv\Scripts\python.exe -B -m pip install --cache-dir ..\.work\pip-cache -r server\requirements.txt
..\.work\venv\Scripts\python.exe -B -m unittest -v server.test_gateway server.test_quotas server.test_quota_ledger server.test_context_errors server.test_open_webui_access server.test_self_password server.test_model_control server.test_serve
```

测试临时库、证书和配置放在 `.work/tests/`。本轮执行证据见仓库 `.work/rev9/`，脚本 usage 是协议 fixture，不能称为真实模型 Token 统计。生产验收还需覆盖设置额度、归零拒绝、充值恢复、本机原生分词及流式/中止行为。

回滚保留数据库备份及新增账本。旧 Gateway 会忽略额度表，**恢复旧服务也会停止强制额度校验**，不能在仍要求限额的环境中视为等价替代。
