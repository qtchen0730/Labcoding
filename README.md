# CCSLab Code Lite

当前 Windows 客户端为 **1.5.3 Rev7**，包含 Coding Agent 上下文管理修复、最近项目记录移除及历史会话删除。62 项客户端联合回归已通过。

上传 GitHub 可直接使用 `GitHub纯源码/` 文件夹，或解压 `releases/CCSLab-Code-Lite-Rev7-GitHub-Source.zip`；只包含前后端源码、测试、许可证和必要配置，不含运行引擎、Python、安装包或缓存。

## 目录

- [Windows 客户端源码](CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source/)：界面、OpenCode 桥接、工具及自动化测试。
- [Gateway 服务端源码](CCSLab-Code-Lite-1.5.3-Rev3-Server/server/)：账号、模型转发、迁移、配置示例与测试。目录中的 Rev3 是服务端版本，保留的是唯一服务端源码。
- `releases/Rev7/`：本地最新安装包、便携包、源码归档、校验和与测试报告；已从 Git 排除，可另行作为 GitHub Release 附件上传。
- `releases/Gateway-Compatibility/`：可选服务端兼容补丁的独立交付。

本机 Python 构建环境保存在 `.local-tools/python312/`；历史验证记录保存在 `.local-archive/`。两者均不纳入 Git。旧版源码、旧发布包与构建缓存已整理到回收站；当前运行程序在本仓库之外。

## 开发与构建

使用 Python 3.12 x64。在客户端目录创建自己的虚拟环境并安装依赖：

```powershell
cd CCSLab-Code-Lite-1.5.3-Rev7-Windows-Source
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe packaging\Fetch-OpenCode.py
.\.venv\Scripts\python.exe app\app.py
```

OpenCode 固定为 1.17.16。`app/vendor/opencode.exe` 是第三方运行组件，不属于源码；本机副本保存在 `.local-tools/opencode-1.17.16/`，新克隆可通过 `Fetch-OpenCode.py` 下载并验证。第三方许可证文本保留在源码中。

构建安装包：

```powershell
.\packaging\Build-Windows.ps1 -Python (Resolve-Path .\.venv\Scripts\python.exe).Path
```

当前工作区的标准安装测试曾出现目录重命名被拒；已验证 Rev7 便携包解压启动。完整记录位于本地 `releases/Rev7/Rev7-合并与测试报告.md`。

## 测试

在客户端目录设置 `PYTHONPATH=app`，设置 `CCSLAB_TEST_OPENSSL` 为 OpenSSL 路径，然后运行客户端 README 中的联合测试命令。测试中的 Token usage 为协议 fixture 值，不代表实际模型分词结果。

Gateway 从其外层目录安装 `server/requirements.txt` 后，使用原生入口 `python -m server`；部署配置见服务端说明。生产配置、数据库、凭据、日志、安装包及本机构建目录均不提交到 Git。本轮未更改生产环境，也未创建远程仓库、提交或推送。
