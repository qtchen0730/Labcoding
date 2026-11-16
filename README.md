# CCSLab Code Lite

当前 Windows 客户端为 **1.5.3 Rev12 候选版**，继承当前 Rev11 的功能。默认薄荷绿，顶部太阳切换 8 种主题；项目结果默认执行记录；可选择加密保存登录；设置旁显示齿轮。管理员设置 Token 额度，普通用户显示余额百分比。客户端与服务端源码目录统一使用 Rev12 命名。

- [Windows 客户端源码与开发说明](CCSLab-Code-Lite-1.5.3-Rev12-Windows-Source/)：界面、OpenCode 桥接、工具和验证。
- [Gateway 服务端源码](CCSLab-Code-Lite-1.5.3-Rev12-Server/server/)：账号、模型转发、迁移、配置示例及测试。

## 主题

在顶部太阳按钮或 **设置 → 通用 → 外观 → 主题** 中选择：经典浅色、经典深色、暖沙米白、薄荷绿、晴空蓝、深海蓝、静谧墨绿、暮色紫。薄荷绿恢复原来的浅色配色，默认主题仍为薄荷绿；之后保留用户选择。

左侧历史会话支持 **右键 → 重命名**，也可选中后按 F2。名称按当前账号和项目保存在本机，搜索、刷新、重启后保留；不改变聊天内容、执行记录或项目文件。运行任务时暂不可重命名，删除会话同步清理名称。

登录页可选保存账号密码，使用 Windows 当前用户 DPAPI 加密，启动回填用户名及掩码密码并恢复登录。关闭软件或退出登录均保留已勾选保存的账号密码；退出登录只注销会话，本次运行不自动重新登录，取消勾选才清除保存。临时断网保留凭据并重试；不保存管理员授权码，管理员会话过期后仅恢复普通用户身份。

设置按钮内的齿轮紧随文字右侧。程序、窗口及安装器采用薄荷绿底的 C 字图标；矢量源为客户端 `app/assets/logo.svg`，构建自动生成 7 种尺寸的 ICO，生成文件在 `.work/assets/Rev12/`。

额度及免授权码降级需要 [配套 Gateway 更新](CCSLab-Code-Lite-1.5.3-Rev12-Server/REV9.md)。旧 Gateway 上额度显示“不可用”，不虚构余额。本轮只交付服务端源码，不改生产部署。

## 仓库与本地产物

只将源码、测试、第三方许可证、依赖清单及必要文档提交 Git。全部开发产物集中在 **`.work/`**，该目录整体已忽略，包括二进制下载、构建中间文件、发布包、测试数据、日志、Python 缓存和临时文件。

本轮验证报告和交付在 `.work/rev12/`，版本、基线和验收边界见 [REV12.md](REV12.md)。`app/vendor/` 仅保留第三方许可证文本。执行 `packaging/Fetch-OpenCode.py` 会将固定 1.17.16 引擎下载到 `.work/vendor/`，构建时再复制入软件。

## 开发与构建

在仓库根目录使用 Python 3.12 x64：

```powershell
python -m venv .work\venv
.\.work\venv\Scripts\python.exe -B -m pip install -r CCSLab-Code-Lite-1.5.3-Rev12-Windows-Source\requirements-build.txt
.\.work\venv\Scripts\python.exe -B CCSLab-Code-Lite-1.5.3-Rev12-Windows-Source\packaging\Fetch-OpenCode.py
.\.work\venv\Scripts\python.exe -B CCSLab-Code-Lite-1.5.3-Rev12-Windows-Source\app\app.py
.\CCSLab-Code-Lite-1.5.3-Rev12-Windows-Source\packaging\Build-Windows.ps1 -Python (Resolve-Path .work\venv\Scripts\python.exe).Path
```

软件正常用户配置和会话仍在原用户数据目录，不随开发产物清理。客户端测试命令见客户端 README，使用 `-B` 并自动在 `.work/tests/` 建立临时数据。

从仓库根目录进入 `CCSLab-Code-Lite-1.5.3-Rev12-Server/` 后，Gateway 使用原生入口 `python -m server`，依赖见该目录下的 `server/requirements.txt`。本轮没有修改生产 Gateway、数据库、Cloudflare 或模型参数，没有推送代码。原仓库 .git 只读，版本提交和可导入 bundle 在独立 Rev12 仓库保留。

## Rev12 验收边界

安装器增加短暂占用重试、持久事务及中断恢复。额度区分预留、待核对和耗尽，流式中间 usage 不作为最终账单。完整 Gateway HTTP 与真实 llama.cpp 联调当前为 **BLOCKED**，本地回归通过不代表生产验收通过。

部署前阅读 [Rev12 Ubuntu 部署与回滚手册](CCSLab-Code-Lite-1.5.3-Rev12-Server/DEPLOY-REV12.md)，验收门槛解除后由管理员授权执行，不自动部署。
