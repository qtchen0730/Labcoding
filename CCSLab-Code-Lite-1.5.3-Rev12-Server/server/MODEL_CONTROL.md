# Labcoding 模型参数控制

本功能让已登录管理员在 Labcoding 管理界面预览并确认新的 `total_context` 与 `slots`，由 Gateway 调用一个固定、受限的 root helper 更新 llama.cpp 配置并重启固定服务。它不改变账号、额度、审批或 OpenCode 功能。

## 固定行为与限制

- Gateway API 继续提供状态、预览、确认应用；预览确认票据绑定管理员、一次性使用且 90 秒过期。应用前等待推理结束，记录 SQLite 审计结果。
- 请求只接受 `total_context`（32768、65536、131072、262144 之一）和 `slots`（1–10）。客户端不能指定路径、命令或服务名。
- root helper 只管理 `/etc/ccslab/model-context.env` 和 `ccslab-llm.service`。环境文件只写 `LLAMA_ARG_CTX_SIZE` 和 `LLAMA_ARG_N_PARALLEL`。应用后核对新 PID、`/health`、`/slots` 和每槽 `n_ctx`；失败则恢复原文件并重启，恢复未验证时留下 pending 标记并阻止后续模型请求。
- llama.cpp 按 `total_context / slots` 分配每槽上下文并按 256 对齐。示例 `131072 / 5` 得到每槽 `n_ctx=26368`。这不是显存安全保证；较大上下文或槽数可能耗尽显存并导致重启失败。管理员预览需明确显示此风险。
- 受管配置必须来自环境文件。显式 `-c/--ctx-size` 或 `-np/--parallel` 会覆盖环境变量，因此部署时必须从 `ExecStart` 移除这两个参数。Gateway/helper 在预览与应用时均核实进程参数和环境一致。

## 权限模型

控制 spec 固定在 `/etc/ccslab/model-control.json`，初始 `enabled:false`，root 所有且不可由普通用户写入。唯一提权入口是 sudoers 对无参数 `/usr/local/sbin/ccslab-model-controller` 的精确授权；Gateway 以参数化 JSON 标准输入传递两个数值、旧值及 spec 摘要。helper 校验字段、整数范围、当前服务和配置，使用固定路径及固定 unit，不执行 shell。

**注意：** systemd 的 `NoNewPrivileges=yes` 会阻止 Gateway 通过 setuid sudo 获得 root。因此启用该精确 sudoers 规则时，Gateway 必须将 `NoNewPrivileges` 改为 `no`。这仅解除该进程的 setuid 提权阻断，sudoers 仍只允许固定 helper；Gateway 进程被攻陷后可调用此 helper，所以应由运维评估这一权限边界。若不接受该安全取舍，不部署控制写入功能，保持只读；无需引入 Socket 服务。

sudoers 示例（以真实服务账户替换 `server`；`""` 表示不接受任何命令行参数）：

```sudoers
server ALL=(root) NOPASSWD: /usr/local/sbin/ccslab-model-controller ""
```

建议所有配置和 helper 路径由 `root:root` 所有、父目录不可被 Gateway 写入。sudoers 用 `visudo -cf` 校验。不要授予通用 `systemctl`、shell、解释器或可写脚本目录权限。

## 初次部署（仅获批维护窗口执行）

部署前备份原 unit/drop-in、Gateway drop-in、环境文件及策略；记录服务状态、PID、API 健康、模型 ID、5 槽和每槽上下文。确认当前模型、llama.cpp 构建及 GPU 条件。**本仓库没有可靠的完整负载显存峰值预算，不得把空闲显存当作预算。**首次接入只维持现有 `131072 / 5`，不同时更改模型或 GPU 参数。

1. 将审查版 `server/model_control.py` 部署到 Gateway 应用树的 `server/model_control.py`，并安装同版至 root 所有的 `/usr/local/lib/ccslab-model-control/model_control.py`；安装 `server/model_host.py` 为 `/usr/local/sbin/ccslab-model-controller`，保持 Python shebang `#!/usr/bin/python3 -I`。为 root helper 目录设 `0755`、模块 `0644`、helper `0755`；确认路径链不可被 `server` 写入。Gateway 应用代码替换后需重启 Gateway 才会加载。
2. 在 `/etc/ccslab` 安装 root 所有的 `model-control.json`（格式参见 `model-control.example.json`），设置初始值 `enabled:false`，填入既有模型 API URL、ID、GPU index 和现有 API key 文件的固定路径。不要把密钥写进 JSON、命令行或日志。保留现有 API key 文件和 Gateway 配置。
3. 创建 root 所有的 `/etc/ccslab/model-context.env`，初始内容严格为：

   ```text
   LLAMA_ARG_CTX_SIZE=131072
   LLAMA_ARG_N_PARALLEL=5
   ```

   模式 `0644`，只含以上两行及结尾换行。
4. 修改 `ccslab-llm.service` drop-in：保留原 `EnvironmentFile=/etc/ccslab/llm.env` 及现有模型、网络、认证和其他参数；追加 `/etc/ccslab/model-context.env`，用完整 `ExecStart` 覆盖原启动行，但只移除显式 `-c 131072 -np 5`。检查最终 unit 仅有一组上下文来源且没有后续 `UnsetEnvironment` 或 CLI 覆盖。
5. 创建 sudoers 文件，仅授予上述 helper 空参数调用，使用 `visudo -cf` 验证。Gateway drop-in 设置 `CCSLAB_MODEL_CONTROL_SPEC=/etc/ccslab/model-control.json` 和 `NoNewPrivileges=no`；保留其他服务设置。验证 sudo 规则只能运行 helper，不能附加参数、任意命令或 shell。
6. `systemd-analyze verify` 后，在维护窗口 `daemon-reload` 并先重启模型服务。确认服务、模型 API、原模型 ID、`131072 / 5`、每槽 `n_ctx=26368`、进程参数/环境及客户端连接恢复。再重启 Gateway 使其加载配置，核验原 API、登录会话、账号/额度数据及客户端连接。上述两个重启都会造成短暂中断。
7. 在管理员界面查看状态与预览，确认风险提示和目标值正确。首次接入期间不执行参数变更；只有另行获批后才启用 spec，并由管理员确认应用。

生产现状的已知基线（2026-11-19 前次只读核验，部署前必须重新检查）：模型 `131072 / 5`，启动行含 `-c 131072 -np 5`，Gateway `NoNewPrivileges=yes`；模型控制 helper/spec/环境文件及 `CCSLAB_MODEL_CONTROL_SPEC` 尚未安装。若现状变化，以维护窗口重新读取的实际 unit、服务状态和 `/slots` 为准。

## 应用失败与回滚

helper 在改写前以原子写入保存原环境快照并落盘 pending 标记。新配置启动后，只有健康、模型、PID、启动环境/参数和实际槽数/每槽上下文全部核验通过才返回成功。启动失败或核验超时会恢复快照并重启同一固定模型服务；回滚核验失败时保留 pending、阻止新模型请求并返回失败，需运维介入，不能伪报成功。

维护窗口中的整体验证失败回滚步骤：

1. 保留 Gateway/模型日志、SQLite 模型审计和 `/etc/ccslab/model-change.pending`；不要在回滚不明时手工清除 pending。
2. 将 `model-context.env` 恢复为已备份的 `131072 / 5`，恢复原模型 unit drop-in/`ExecStart`（含 `-c 131072 -np 5`），`daemon-reload` 后重启 `ccslab-llm.service`。确认 `/health`、模型 ID、`/slots` 五槽、每槽上下文及实际启动参数。
3. 移除本次 Gateway 配置 spec 路径和精确 sudoers 文件，恢复 Gateway drop-in 原值（包括 `NoNewPrivileges=yes`），恢复原 Gateway 代码版本（若代码已部署），再 `daemon-reload` 并重启 Gateway。确认 `/health`、登录、既有 API/客户端连接、账号和额度数据。
4. 仅在原模型服务和 Gateway 均验证恢复、事务状态经检查后，由运维处理 pending/备份文件。不要删除其他服务 drop-in、轮换凭据或改写账号数据库。

## 生产只读复核（2026-11-19 16:24 CST）

通过 SSH 只读检查：Gateway 与 `ccslab-llm.service` 均 active；模型 PID 4492，`/health` 正常，`/slots` 报告 5 槽且每槽 `n_ctx=26368`，进程启动参数仍为 `-c 131072 -np 5`。Gateway 为 `server:server`（UID/GID 1002），`NoNewPrivileges=yes`；没有 Gateway/model drop-in，未设置 `CCSLAB_MODEL_CONTROL_SPEC`。`/etc/ccslab` 为 `root:server 0750`，仅有原 `llm.env`（`root:server 0640`）；spec、受管环境、helper 及 pending 均不存在。

`sudo -n -l` 对 `server` 返回拒绝，`/etc/sudoers.d` 仅有 README。已有模型备份 `ccslab-llm-128k.backup` 内容特征包含 `-c 131072 -np 1`，与当前 5 槽基线不符；`ccslab-llm.service.save` 存在但当前账号不可读，也没有 Gateway 的现成 unit 备份。维护窗口必须先新建并验证当前服务、环境文件及相关 drop-in 的备份，不能直接用旧模型备份回退。以上均为只读核验，无生产文件变更或服务重启。

## 本地验证

```powershell
& ..\.work\verify-desktop-venv\Scripts\python.exe -B -m unittest -v server.test_model_control server.test_gateway
```

以上自动化用例在隔离临时目录中模拟进程遥测、sudo/systemd 和 HTTP 健康检查，不会重启真实服务，也不能替代维护窗口的真实权限、GPU 峰值和重启验收。

## 临时受限部署入口（首次接入准备）

本地 `server/model_control_deploy.py` 是一次性、固定无参数入口；远端安装位置为 `/usr/local/sbin/ccslab-model-control-deploy`。它只接受 `server` 用户经精确 sudoers 授权发起的无参数调用，脚本固定锁定两个 Gateway 源文件的 SHA-256，检查当前服务器、服务基线和运行态后才备份并部署。载荷由入口直接打开并校验哈希，随后固定到 `/run` 下 root 私有暂存目录并再次校验；root 侧验收只从 root 管理的 `/usr/local/lib/ccslab-model-control/model_control.py` 导入，不导入 `/home/server` 下可写的 Gateway 源码。首次接入保持 `131072 / 5`，不调用管理员 apply；成功或失败退出时均尝试删除临时 sudoers 授权和入口脚本，永久模型 helper 的独立授权仅在部署成功时保留。部署前必须从当前可信工作树重新生成并核验部署载荷，不可复用过期载荷。

用户在确认维护窗口并人工取得 root 会话后，先将 `ccslab-model-control-deploy` 放到 `/home/server/.cache/ccslab-model-control-deploy/`，将本次审查的 `model_control.py`、`model_host.py` 放到其 `payload/` 子目录；入口安装为 `root:root 0755`，载荷目录 `server:server 0700`、文件 `server:server 0644`。然后人工执行：

```sh
sudo install -o root -g root -m 0755 /home/server/.cache/ccslab-model-control-deploy/ccslab-model-control-deploy /usr/local/sbin/ccslab-model-control-deploy
sudo visudo -f /etc/sudoers.d/ccslab-model-control-deploy
```

在 visudo 编辑器中只写入：

```sudoers
server ALL=(root) NOPASSWD: /usr/local/sbin/ccslab-model-control-deploy ""
```

保存后执行 `sudo chown root:root /etc/sudoers.d/ccslab-model-control-deploy && sudo chmod 0440 /etc/sudoers.d/ccslab-model-control-deploy && sudo visudo -cf /etc/sudoers`，并核对入口、临时授权的属主和模式。之后由获授权的自动化客户端执行：

```sh
ssh server@192.168.1.107 'sudo -n -- /usr/local/sbin/ccslab-model-control-deploy'
```

正常流程会自行撤销临时授权。若入口中断或清理失败，人工撤销命令为 `sudo rm -f /etc/sudoers.d/ccslab-model-control-deploy /usr/local/sbin/ccslab-model-control-deploy && sudo visudo -cf /etc/sudoers`。不得用该临时入口替代或放宽永久 helper 规则。自动部署及真实服务重启仍必须按前文维护窗口、备份和验收条件执行。
