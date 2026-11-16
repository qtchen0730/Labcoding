# Ubuntu 账号与模型网关部署

由实验室运维执行。以下步骤部署独立的账号网关，复用现有 llama.cpp 的内部接口与 Bearer 密钥。网关在普通服务账户下运行。现有推理服务配置保持原状。

## 1. 准备

- 使用 Python 3.12，在源码根目录安装 `server/requirements.txt`，将源码目录部署为专用服务账户可读。账号数据库和 TLS 私钥应仅授予必要的文件访问权限。
- 使用直连 HTTPS 时，为 `192.168.1.107` 准备受客户端信任的服务证书。如果使用默认 IP，证书的 SAN 必须包含该 IP。使用内部 DNS 时可改为 DNS SAN，客户端填同一个 HTTPS 地址。通过实验室管理员核验并下发 CA，采用 Windows 信任的证书链。
- 使用直连 HTTPS 时，仅对实验室受授信的客户端开放网关 TCP 8443。确认网关进程可以访问 `http://172.17.0.1:8000`。保持 llama.cpp 的 8000 端口在内部网络。
- 把已经授权的 llama.cpp Bearer Token 放到服务账户可读的权限为 `0400` 的文件。密钥文件、数据库和证书私钥都应位于源码目录之外。把密钥写入文件时使用安全编辑器，避免在 Shell 历史、命令行参数或文档中录入。

## 2. 创建数据库和管理员

下列命令以专用服务账户运行。数据库目录需事先由运维分配给该账户。

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r server/requirements.txt
python -m server --database /var/lib/ccslab-code/accounts.sqlite3 migrate
```

为管理员登录、切换身份和客户端管理员注册配置同一份授权密码验证文件。已有该文件的部署可继续使用它。在终端提示时输入实验室约定的 6 位授权密码，并重复确认。

```sh
python -m server set-admin-code --file /var/lib/ccslab-code/admin-registration.argon2
export CCSLAB_ADMIN_REGISTRATION_CODE_FILE=/var/lib/ccslab-code/admin-registration.argon2
```

交互输入不回显。文件仅保存 Argon2id 验证值，Linux 权限为 `0600`。客户端选择「管理员注册」并提交有效的授权密码后，服务端创建已激活的管理员账号。「普通用户注册」只填写用户名和登录密码，账号等待审批。管理员授权密码验证按所有管理入口共享限流，每 15 分钟每来源最多 3 次失败，全局最多 5 次失败，计数随数据库保留。撤下验证文件会停用管理员登录、身份切换和管理员注册。使用同一条终端命令可更换授权密码。

若部署通过服务器终端创建首位管理员，可运行下面的初始化命令。已有管理员时会拒绝再次初始化。终端初始化创建的是登录账号，其管理会话也需使用上面的授权密码。

```sh
python -m server --database /var/lib/ccslab-code/accounts.sqlite3 init-admin --username lab-admin
```

账号密码使用带独立盐值的 Argon2id 哈希。会话令牌采用随机数，数据库保存 SHA-256 摘要。会话有效期为 8 小时，可以退出撤销。默认登录建立普通会话，已获管理员身份的账号可提交授权密码获取管理会话。切换角色时验证授权密码并轮换会话令牌，保留原有效期。

## 3. 启动和验证

### 直连 HTTPS

```sh
export CCSLAB_LLAMA_API_KEY_FILE=/var/lib/ccslab-code/llama-api-key
export CCSLAB_LLAMA_URL=http://172.17.0.1:8000
export CCSLAB_MODEL_ID=lab-general
python -m server --database /var/lib/ccslab-code/accounts.sqlite3 serve \
  --bind 192.168.1.107 --port 8443 \
  --tls-cert /srv/ccslab/tls/server-chain.pem --tls-key /srv/ccslab/tls/server-key.pem
```

若启用客户端修改授权密码，专用运行账户需可读写其验证文件所在的私有目录，应用会通过原子替换保存新的验证值。可另外设置 `CCSLAB_MODEL_RUNTIME_UNIT` 为运维确认的真实 llama.cpp systemd 单元名，以从当前进程读回配置。GPU 监控默认使用 `CCSLAB_MODEL_GPU_INDEX=0`。当所需运行数据不可读时客户端显示不可用。配置管理需另行部署受控组件，见 [模型控制组件说明](MODEL_CONTROL.md)。网关固定一个 Uvicorn worker，用同一个进程的屏障协调推理和模型变更。

确认 `https://192.168.1.107:8443/health` 返回 `{"ok":true}`，且连接在 Windows 上证书验证通过。然后通过桌面客户端注册一个测试账号，登录工作台查看待激活状态，由管理员激活后发起只读任务。待审批、被拒绝和被禁用的账号可登录查看自身状态，模型调用权限按实时状态校验。

### Cloudflare Tunnel 本机回源

沿用上述环境变量，选择下面的启动命令。TLS 参数须成对提供。省略两项时，HTTP 监听仅限 `127.0.0.1`。

```sh
python -m server --database /var/lib/ccslab-code/accounts.sqlite3 serve \
  --bind 127.0.0.1 --port 8443
```

在 Ubuntu 本机运行 `curl -fsS http://127.0.0.1:8443/health`，预期返回 `{"ok":true}`。同一主机上受信任的 `cloudflared` 使用 `http://127.0.0.1:8443` 作为 Service URL。公网客户端通过 HTTPS 访问该主机名。请在 Cloudflare 边缘为该主机名设置 HTTP 到 HTTPS 的重定向。

网关忽略转发头，以实际连接来源进行限流。同一连接器转发的登录请求共享 `127.0.0.1` 的来源额度。

## 4. 运维范围

- 对外提供账号 API 和受账号状态保护的 `/v1/models`、`/v1/chat/completions`。普通用户无需 SSH、共享模型 API Key 或推理参数。
- SQLite 数据库包括账户、会话、密码更改、审批与模型配置审计。升级前备份数据库及授权密码验证文件，运行 `migrate` 后启动新版网关。原有登录会话按普通权限保留。
- 模型 API Key 仅在 Ubuntu 本地文件和网关进程中使用。服务默认关闭请求访问日志。部署 TLS 时保留单一可信证书入口。
- 管理员禁用账号后，新推理请求立即拒绝；正在流出的响应在网关检测到状态变化后终止。批准执行的 Windows 本地命令属于客户端执行范围。
- 管理员可在「设置 → 账号密码」重置用户登录密码。重置后目标账号的所有会话失效，激活状态原样保留。更改管理员授权密码后，所有管理会话失效，普通会话继续有效。
