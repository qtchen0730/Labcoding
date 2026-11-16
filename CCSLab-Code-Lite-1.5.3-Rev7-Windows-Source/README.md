# CCSLab Code Lite 1.5.3 Rev8

本版在保留上下文修复、最近项目记录及历史会话删除的基础上，新增 6 组背景配色。进入 **设置 → 通用 → 外观 → 主题**，可选择经典浅色、经典深色、暖沙米白、薄荷绿、晴空蓝、深海蓝、静谧墨绿、暮色紫；选择后自动保存，兼容旧版 light/dark 配置。

最近项目“删除”只移除当前账号列表记录，保留磁盘项目文件及历史会话。历史会话“删除”经确认后删除该会话的 OpenCode 数据、客户端记录与草稿；运行任务时禁止删除。

## 本地工作目录

所有开发时的非源码产物统一放在仓库根目录 `.work/`，已由根 `.gitignore` 排除：

- `vendor/`：固定版本 OpenCode 二进制与下载的许可证。
- `build/`：PyInstaller、中间载荷与安装器编译文件。
- `releases/`：本地安装包、便携包、纯源码压缩包。
- `tests/`：验证临时项目、会话、证书和测试数据。
- `themes/`：本轮验证日志与报告。
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

源码目录名沿用 Rev7，当前软件发布版本为 Rev8。OpenCode 固定为 1.17.16；源码运行从 `.work/vendor/` 读取，打包后仍使用软件安装目录内的 `vendor/`。

## 验证

在本客户端目录设置 `PYTHONPATH=app`，设置 `CCSLAB_TEST_OPENSSL` 为 OpenSSL 路径，运行：

```powershell
python -B -m unittest -v verify_themes verify_context verify_context_recovery verify_context_budget verify_history verify_merged verify_account_diagnostics verify_account_transport verify_password_view verify_public_connection verify_role_access verify_direct verify_proxy_engine
```

验证临时文件自动落在 `.work/tests/`。使用 `-B` 避免在源码旁生成字节码；构建脚本会将 Python 字节码重定向到 `.work/pycache/`。协议 fixture 的 Token 数为脚本值，不代表实际模型分词结果。
