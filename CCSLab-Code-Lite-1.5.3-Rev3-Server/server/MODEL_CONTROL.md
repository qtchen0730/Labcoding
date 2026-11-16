# 模型状态与可选受控重配置

## 只读模式（默认）

账号服务可先单独部署。读取模型信息无需设置 sudo、替换服务单元或重启模型。由 Ubuntu 运维把以下值加入账号服务的环境配置。

| 参数 | 用途 |
| --- | --- |
| `CCSLAB_LLAMA_URL` | Ubuntu 本机可达的内部 llama.cpp 路由，默认是 `http://172.17.0.1:8000`。 |
| `CCSLAB_LLAMA_API_KEY_FILE` | 仅 Ubuntu 读取的 Bearer Token 文件。 |
| `CCSLAB_MODEL_RUNTIME_UNIT` | 可选，填写现有 llama.cpp 的准确 systemd 单元名。用于定位当前运行的 llama-server 进程和其已生效的数字启动参数。 |
| `CCSLAB_MODEL_GPU_INDEX` | 可选，默认 0。网关运行账号需要具备读取 `/usr/bin/nvidia-smi` 遥测所需的设备权限。 |

健康状态和模型身份来自 `/health` 与 `/v1/models`。运行槽位来自 `/slots` 或 `/props`。每槽上下文来自 `/slots[].n_ctx`，取报告槽位的最小值。没有逐槽数据时可使用 `/props.default_generation_settings.n_ctx`。总上下文优先取已运行进程的 `-c` 或 `--ctx-size`，兼容版本明确报告总上下文时也可读取。已正确安装控制适配器时，读取专用的上下文配置文件还要求识别现有运行进程。字段缺少可核实证据时返回空值。总上下文和实际每槽上限可能因 KV 布局或对齐而不同。 [llama.cpp 服务接口](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

## 可选写入适配器

以下步骤供 Ubuntu 运维在明确授权的维护窗口内实施。账号服务的常规部署可以保持只读。现有部署的准确 unit、启动参数、显存占用和备用组合尚未在目标服务器核验。

1. 确认 unit 的 MainPID 确为服务端 `llama-server`，API 路由、模型 ID、GPU UUID 对应该服务。先保存原 unit、drop-in 和当前有效启动参数。只启用同一 UUID 上运行的单个 llama.cpp 服务。正式验收前需确认 `/slots` 可读、能报告每槽实际 `n_ctx` 和是否正在处理请求。
2. 仅在已授权的服务器改造中，为现有 llama.cpp unit 配置 `EnvironmentFile=/etc/ccslab/model-context.env`。让现有 `ExecStart` 继续使用现有模型、设备、网络、认证及其他参数，仅移除会覆盖环境变量的 `-c` 与 `-np`。落实前检查已部署构建支持 `LLAMA_ARG_CTX_SIZE` 和 `LLAMA_ARG_N_PARALLEL`。复制当前有效的两个值创建 root 所有的文件，内容格式精确为两行数字配置和结尾换行，顺序如下。其中的示例值应与进程实测配置一致。

   ```text
   LLAMA_ARG_CTX_SIZE=131072
   LLAMA_ARG_N_PARALLEL=5
   ```

   在批准的初始维护中用其启动一次模型服务，验证原值、模型 ID、槽位数及每槽上限均维持预期。不要为启用管理接口同时调整模型权重、GPU 分配或其他服务。

3. 评估备选组合。运维在同一模型文件、llama.cpp 构建、KV 类型、GPU 与负载条件下测量每个组合。核准文件用 `model-control.example.json` 起步，至少记录当前组合和一个备选。每个 `profiles` 条目包含整数 `total_context`、`slots`、`slot_context` 和 `peak_model_mib`。最后两项分别为实测每槽上限和进程完整负载显存峰值预算，预算要覆盖误差。换模型或构建后重新核准。配置写入前后检查 `n_ctx_train`、实际槽位、显存容量、安全余量和 llama-server 进程显存。
4. 将 `model-control.json`、`model-context.env` 保存在 `/etc/ccslab`，目录和文件由 root 所有，普通账号不可写，网关可读取。由运维把同一个上游 Token 文件置于 `/etc/ccslab/llama-api-key`，所有者为 root、受权组可读，建议模式 0640。网关使用 `CCSLAB_LLAMA_API_KEY_FILE=/etc/ccslab/llama-api-key`，并配置 `CCSLAB_MODEL_CONTROL_SPEC=/etc/ccslab/model-control.json`。
5. 安装固定的受控组件。以下代码块假设独立网关 Unix 账户名为 `ccslab`，从源码根目录执行。辅助程序与 Python 模块的整个目录链均必须由 root 所有且其他用户不可写。

   ```sh
   sudo install -d -o root -g root -m 0755 /usr/local/lib/ccslab-model-control
   sudo install -o root -g root -m 0644 server/model_control.py /usr/local/lib/ccslab-model-control/model_control.py
   sudo install -o root -g root -m 0755 server/model_host.py /usr/local/sbin/ccslab-model-controller
   sudo visudo -f /etc/sudoers.d/ccslab-model-controller
   ```

   `sudoers` 规则应精确限制为没有命令行参数的辅助程序。网关的 systemd 设置需要允许这一固定 sudo 调用。如果网关服务用了 `NoNewPrivileges=yes`，保持只读；由运维另行评审是否对独立网关启用这个组件。

   ```sudoers
   ccslab ALL=(root) NOPASSWD: /usr/local/sbin/ccslab-model-controller ""
   ```

   网关只运行单个 worker，保证模型请求和变更操作共享同一请求门控。组件将预设值传给固定路径的辅助进程。组件只接受两个已核准的数字组合和策略指纹，然后改写上述专用文件并重启固定 unit。
6. 只有明确获准并确认其他调用者已停止向模型发送请求时，Ubuntu 运维才为本次操作开启短期窗口。暂停 Open WebUI 及绕过网关的本地调用，保持当前模型进程运行，等待所有槽位空闲。该窗口由 root 在 `/etc/ccslab/model-maintenance-until` 写入未来不超过 15 分钟的 Unix 秒时间戳，文件须由 root 所有且普通用户不可写。运维可用 `date -d '+10 minutes' +%s` 生成值。管理员此时可在客户端获取预览并二次确认。网关在应用开始前阻止已有推理与变更并发；辅助程序再读取模型槽位。维护窗只对配置变更授权，其他直连调用必须由运维保持停止。

## 审计与恢复

SQLite `model_configuration_audit` 保存操作者、时间、原参数、新参数、应用结果与故障原因。辅助程序另外记录不含密钥的系统日志。应用前保存 `/etc/ccslab/model-context.previous` 和事务标记。新的 PID、健康状态、配置、模型 ID、运行槽位及显存检查均通过后才提交。失败时恢复原配置并重启同一 unit，再次核对。若恢复失败或辅助程序异常中断，`model-change.pending` 将保留，网关会拒绝新的模型请求与配置写入。运维需要检查日志和实际进程，按保存的配置手工恢复已批准的模型服务，核验健康和原槽位后再清除标记。

可在源码根目录运行 `python -m unittest server.test_model_control -v`。测试覆盖运行遥测映射、精确参数与显存校验、确认令牌绑定及过期、活跃槽位拒绝、审计，以及隔离夹具中的成功变更、失败恢复和恢复失败。隔离测试使用临时文件与可控健康响应。
