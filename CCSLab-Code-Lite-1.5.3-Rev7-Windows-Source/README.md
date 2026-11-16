# CCSLab Code Lite 1.5.3 Rev7 合并版

同一个客户端同时包含 V5 上下文修复和 V6 最近项目/历史会话删除功能。

- 上下文：压缩恢复后清除中间超限错误；区分当前估算、最近成功实测及统计不可用；保留输出预算、完整行分页和脱敏诊断。
- 最近项目“删除”：从当前账号的最近项目列表移除记录，保留磁盘项目文件及历史会话。
- 历史会话“删除”：确认后删除选中会话的原生 OpenCode 数据、客户端显示记录及草稿；运行任务时禁止删除。搜索框下有“历史会话”标题。

使用同名 Rev7 安装包，或将 Rev7 Portable 压缩包解压至一个独立的普通文件夹，启动 CCSLab Code Lite.exe。沿用既有用户数据目录。V5/V6 原包与源码均保留；本目录是明确命名的合并源码。

构建使用 Python 3.12 x64，安装 pyinstaller、markdown-it-py、urllib3 后执行 packaging/Build-Windows.ps1 -Python 你的Python路径。OpenCode 组件固定为 1.17.16，由 packaging/Fetch-OpenCode.py 下载，不修改生产 Gateway 或模型参数。

联合验证入口：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'app')
$env:CCSLAB_TEST_OPENSSL = '你的openssl.exe完整路径'
python -B -m unittest -v verify_context verify_context_recovery verify_context_budget verify_history verify_merged verify_account_diagnostics verify_account_transport verify_password_view verify_public_connection verify_role_access verify_direct verify_proxy_engine
```

验证和安装限制见同一交付中的 Rev7-合并与测试报告.md。协议 fixture 的 Token 为脚本值，不能作为实际 Qwen 分词结果。
