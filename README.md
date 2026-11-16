# CCSLab Code Lite

当前 Windows 客户端为 **1.5.3 Rev8**：包含上下文管理修复、最近项目记录移除、历史会话删除，以及 8 种可保存的界面主题。源码目录名沿用 Rev7。

- [Windows 客户端源码与开发说明](CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source/)：界面、OpenCode 桥接、工具和验证。
- [Gateway 服务端源码](CCSLab-Code-Lite-1.5.3-Rev3-Server/server/)：账号、模型转发、迁移、配置示例及测试。目录中的 Rev3 为服务端版本。

## 主题

在 **设置 → 通用 → 外观 → 主题** 中选择：经典浅色、经典深色、暖沙米白、薄荷绿、晴空蓝、深海蓝、静谧墨绿、暮色紫。主题覆盖背景、侧栏、输入区、按钮、代码与 Diff 配色，并在下次启动恢复；原有 light/dark 配置继续有效。

## 仓库与本地产物

只将源码、测试、第三方许可证、依赖清单及必要文档提交 Git。全部开发产物集中在 **`.work/`**，该目录整体已忽略，包括二进制下载、构建中间文件、发布包、测试数据、日志、Python 缓存和临时文件。

本轮验证报告在 `.work/themes/report.md`，本地交付在 `.work/releases/`。`app/vendor/` 仅保留第三方许可证文本。执行 `packaging/Fetch-OpenCode.py` 会将固定 1.17.16 引擎下载到 `.work/vendor/`，构建时再复制入软件。

## 开发与构建

在仓库根目录使用 Python 3.12 x64：

```powershell
python -m venv .work\venv
.\.work\venv\Scripts\python.exe -B -m pip install -r CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source\requirements-build.txt
.\.work\venv\Scripts\python.exe -B CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source\packaging\Fetch-OpenCode.py
.\.work\venv\Scripts\python.exe -B CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source\app\app.py
.\CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source\packaging\Build-Windows.ps1 -Python (Resolve-Path .work\venv\Scripts\python.exe).Path
```

软件正常用户配置和会话仍在原用户数据目录，不随开发产物清理。客户端测试命令见客户端 README，使用 `-B` 并自动在 `.work/tests/` 建立临时数据。

Gateway 使用原生入口 `python -m server`，依赖见 `server/requirements.txt`，生产部署说明保持原状。本轮不修改生产 Gateway、数据库、Cloudflare 或模型参数，也不提交或推送代码。
