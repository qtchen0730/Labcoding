# CCSLab Code Lite 1.5.3 Rev11

薄荷绿恢复原来的浅色配色，默认仍为薄荷绿，保留已保存的主题选择。顶部太阳切换 8 种主题，齿轮在“设置”文字右侧的按钮内部，项目结果默认执行记录。

左侧历史会话支持右键“重命名”及 F2，名称为 1–100 个字符。显示名称按当前账号和项目保存，刷新、搜索、离线查看与重启后保留；不会改写对话、执行记录或项目文件。任务运行时禁止重命名，取消及保存失败保持原名，删除会话同时清理名称元数据。名称保存在本机历史文件旁的 `.titles.json`，不混入聊天缓存。

登录页可选 Windows DPAPI 加密保存账号密码。关闭或退出登录均保留勾选保存的用户名和密码，登录页自动回填，密码始终显示掩码；退出登录只注销当前会话，本次运行不自动重新登录，取消勾选清除保存。临时断网保留凭据并重试，认证不通过停止自动重试并允许手动修改。保存文件绑定当前服务器，不向其他地址回填或发送。

程序、窗口、安装器及快捷方式使用薄荷绿底的艺术 C 图标，矢量源 `app/assets/logo.svg`。Windows 构建脚本使用系统 GDI+ 生成 16/24/32/48/64/128/256 像素图标，无字体或新增 Python 依赖。生成的 ICO/PNG 和图标编译器均在 `.work/`；源码直接运行可使用已生成的图标，安装包自带图标资源。

配套 Gateway 支持管理员在“用户审批 → 设置额度”选择不限额或输入 k Token；保存时重新发放所填额度（1k=1000 Token），输入和输出按上游实际 usage 扣减。普通用户左下角显示余额百分比，0% 时禁用任务并提示充值；补充额度后自动恢复，保留草稿。统计缺失会保留预留额并显示待核对，不伪造用量。

管理员降为普通用户不再弹授权码；升为管理员仍需验证。额度及降级需要配套服务端更新；详见 `../CCSLab-Code-Lite-1.5.3-Rev3-Server/REV9.md`。

最近项目“删除”只移除当前账号列表记录，保留磁盘项目文件及历史会话。历史会话“删除”经确认后删除该会话的 OpenCode 数据、客户端记录与草稿；运行任务时禁止删除。

## 本地工作目录

所有开发时的非源码产物统一放在仓库根目录 `.work/`，已由根 `.gitignore` 排除：

- `vendor/`：固定版本 OpenCode 二进制与下载的许可证。
- `build/`：PyInstaller、中间载荷与安装器编译文件。
- `releases/`：本地安装包、便携包、纯源码压缩包。
- `tests/`：验证临时项目、会话、证书和测试数据。
- `themes/`、`rev9/`、`rev10/`、`rev11/`：各轮验证日志与报告。
- `pycache/`、`tmp/`：Python 缓存与构建临时文件。

软件的正常用户设置和会话数据继续使用原用户数据目录，不写入仓库。

## 运行与构建

在仓库根目录使用 Python 3.12 x64：

```powershell
python -m venv .work\venv
.\.work\venv\Scripts\python.exe -B -m pip install -r CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source\requirements-build.txt
.\.work\venv\Scripts\python.exe -B CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source\packaging\Fetch-OpenCode.py
.\.work\venv\Scripts\python.exe -B CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source\app\app.py
.\CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source\packaging\Build-Windows.ps1 -Python (Resolve-Path .work\venv\Scripts\python.exe).Path
```

源码目录名沿用 Rev7，当前软件发布版本为 Rev11。OpenCode 固定为 1.17.16；源码运行从 `.work/vendor/` 读取，打包后仍使用软件安装目录内的 `vendor/`。

## 验证

在本客户端目录设置 `PYTHONPATH=app`，设置 `CCSLAB_TEST_OPENSSL` 为 OpenSSL 路径，运行：

```powershell
python -B -m unittest -v verify_saved_login verify_user_features verify_themes verify_context verify_context_recovery verify_context_budget verify_history verify_merged verify_account_diagnostics verify_account_transport verify_password_view verify_public_connection verify_role_access verify_direct verify_proxy_engine
```

验证临时文件自动落在 `.work/tests/`。使用 `-B` 避免在源码旁生成字节码；构建脚本会将 Python 字节码重定向到 `.work/pycache/`。协议 fixture 的 Token 数为脚本值，不代表实际模型分词结果。
