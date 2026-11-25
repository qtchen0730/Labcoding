# Labcoding 模型上下文与受控维护

## 2.0.5 启动显存样本热修复（源码与增量包完成，尚未部署）

用户已确认2.0.5生命周期功能生产部署；真实停止成功，Gateway持续可用。此次只读调查确认：旧helper将完整`systemctl show ExecStart`显示字符串（含PID、start_time、stop_time、code、status）计入`unit_profile`，停止后的记账字段变化使持久样本失配；UI估算没有检查该指纹，因而仍显示需求。不是样本过期或停止事务删除样本。

新版使用稳定的实际启动配置身份，关联模型/量化、权重与后端文件身份、GPU UUID/型号/驱动、上下文/槽位及环境配置。正常停止前必须采样并原子保存；除原生命周期记录外，增加固定root私有`/etc/ccslab/model-service-sample.json`（0600）。停机/Gateway重启/时间经过不会使匹配样本失效，实质配置变化则拒绝旧样本。页面与真实启动预检共用固定helper的只读`inspect`及同一匹配/余量函数：历史实测进程显存加`max(1024 MiB, 3%)`，与当前驱动free比较。刷新不写样本、不推理、不占槽、不读取模型API Key。Windows2.0.5接口兼容，客户端源码及现有安装包未改。

当前root停止记录仍保留真实29102 MiB样本。只在内存还原同一次执行的停止记账后，旧SHA与记录精确一致；模型/配置及文件时间检查也通过。候选代码只读计算需求30126 MiB，现场free32083 MiB。未把人工提供数值写成样本；只在后续另行确认的管理员启动事务中迁移该可信旧记录，并记录`sample_recovered`审计。若原状态样本缺失，可恢复匹配的root私有副本；两份记录都缺失或证据无法验证时仍拒绝，不提供永久跳过预检。当前不需要冷启动绕过方案。

增量包：`.work/releases/Labcoding-Server-Start-Sample-Hotfix-20261125.tar.gz`，35222字节，SHA-256 `cd89fb5ae41ccb9a515704e75653d66493c8031c5a030e0fee85814160985423`；已上传并解压至服务器cache。仅替换三个Server模块及对应root副本/固定helper，共六个固定目标，无迁移。复用原备份/原子替换/失败恢复机制，适配当前停机状态；与helper共用事务锁，验证并保留离线记录、原直连屏障、上下文/槽位、账号/额度/会话及操作审计。只重启Gateway，绝不自动启动模型。此次仅执行非root只读预检；root私有drop-in及nft完整检查需下面的sudo检查，不宣称已执行。

server终端，另行确认后执行：
```bash
cd /home/server/.cache/labcoding-server-updates && \
sha256sum -c Labcoding-Server-Start-Sample-Hotfix-20261125.tar.gz.sha256 && \
cd start-sample-hotfix-20261125 && \
sha256sum -c SHA256SUMS && \
sudo python3 -B update.py --check
```
```bash
sudo python3 -B update.py --deploy && sudo python3 -B update.py --verify
```
`--deploy`在本机终端提示既有Gateway管理员凭据；不发送凭据给他人，不扩大sudoers。备份在`/var/backups/labcoding-start-sample-hotfix-20261125`。失败自动恢复旧代码；恢复失败保留HOLD，不手工删除屏障。回滚命令：
```bash
cd /home/server/.cache/labcoding-server-updates/start-sample-hotfix-20261125 && \
sudo python3 -B update.py --rollback
```
回滚仅适用于该停机基线未发生新生命周期/配置事务时；拒绝覆盖后续状态、样本及代码，不回灌数据库。部署后先刷新Windows管理页核对匹配历史需求和当前free；实际模型启动仍须用户另行明确确认，再由管理员按钮执行，失败保持离线保护。不直接`systemctl start`绕开预检。

验证：26项生命周期/样本专项、2项相关上下文/共享租约回归通过；三类隔离部署仿真覆盖成功/手动回滚、健康失败自动恢复、恢复失败保留HOLD后重试。补充回归初次发现旧测试夹具未隔离MODEL_MAINTENANCE，读到现场只读屏障并返回503；改为临时路径后通过，未修改生产标记。90安装文件/33冻结模块与现有Windows2.0.5源码一致。原始日志在`.work/tests/model-service-2.0.5/start-hotfix/`。生产热修复部署和真实恢复启动仍未执行，估算不是冷启动峰值保证。

## Windows2.0.5 模型服务生命周期（原交付记录；生产部署已由用户确认）

模型页“模型服务控制”位于参数设置上方；仅管理员可预览/确认启动、停止、重启。新增`/api/admin/model/service/preview`和`/api/admin/model/service/apply`，沿用已有认证、单次确认票据和版本保护。刷新为只读。`model_service`字段是旧API的兼容扩展，缺少配套Gateway时新按钮保持禁用。

操作与参数变更共用Gateway事务门控及原固定无参数helper的flock；客户端只能提交预定义操作，不能指定unit、路径或命令。新增root管理的`/etc/ccslab/model-service-state.json`保存离线意图、阶段、版本及显存历史，Gateway仅读取。helper预检开始前即保存维护意图，Gateway重启不自动启动模型。停止前检查所有全生命周期租约（含工具、审批及等待请求）和实际推理请求，复用原固定端口直连屏障，再复核空闲，只stop `ccslab-llm.service`。核验原PID退出后重新采样GPU；停止期间保留屏障及离线状态。显示的上次停止GPU已用显存差值为驱动总量的前后变化，其他进程同时分配显存可影响该数值；不能冒充模型独占释放量。

启动只读取既有上下文/槽位配置，不改权重、量化、GPU或Key。需有同一policy、配置、GPU和启动配置指纹的历史模型进程显存样本，预检需求为该样本加`max(1024 MiB, 3%)`余量；这是估算，不能保证后续科研进程不抢占显存。无可信样本、采样失败或实际free不足时拒绝启动；首次部署应在模型正常运行时启用功能，首次安全停止会记录样本。重启先评估仅本模型可回收的显存，停止后再按实际free复核。不结束任何训练进程。模型PID、健康、保存配置、实际槽位和每槽上下文核验通过后才解除屏障；失败保持离线保护、正常停止未就绪模型并明确报告，不无限重试。独立OpenCode不强制重载的既有限制不变。

离线时实际上下文、槽位和模型进程显存不沿用历史；已保存参数仍可查看。参数预览/应用和重新同步均由服务端拒绝，避免意外拉起模型。登录、账号、额度和Gateway健康保持可用，推理/新租约返回503，保留旧`detail`及新增`error.code=model_paused`，拒绝发生在额度预留之前。新003迁移仅新增服务操作审计表，不改账号/额度/历史数据。状态或维护屏障不可信、异常断电或强制终止helper留下未完成事务时保持关闭，需运维只读核查后恢复，不能盲目删除标记。

后续部署须另行确认：使用现有增量备份/原子安装/回滚机制，配套更新Gateway的model_control/model_service/model_host/gateway、003迁移及root库中的model_control/model_service与固定helper；不使用旧首次接入脚本或陈旧SHA载荷。无需扩大sudoers、改变systemd模型参数或重启llama.cpp；Gateway需重启加载代码。Windows2.0.5已完成正式构建及隔离覆盖升级验证。本轮只在.cache隔离目录运行模拟服务/GPU与本地Gateway测试，未执行生产stop/start/restart或部署，不修改Ubuntu客户端仓库。


### 2.0.5正式交付记录

服务字段已按模型部署、运行状态、显存评估、时间分组，每行最多2–3字段；复用原主题和布局组件。客户端x64 PE ProductVersion/FileVersion均2.0.5，sequence8；33个冻结模块和90个安装文件与最终源码/构建一致，2.0.4→2.0.5覆盖升级、隔离启动与历史/项目样本保留通过。普通用户在客户端三个动作均不发送请求，直接API三个预览和应用均403；停机状态隔离Gateway健康/账号/额度仍可用，推理/租约503且不扣费，参数同步和重建Gateway不唤醒模型。复用17+40项结果，本次只补分组原生UI、两项权限/暂停保护和三类部署恢复仿真。

产物均保留于仓库根`.work/releases/`：

- `CCSLab-Labcoding-Win-2.0.5-Setup-x64.exe`：76847104字节，SHA-256 `a0bc14f399f2115dfbe16ccd65e85f10b8183972257efefd90a14ff4b0b5fa46`。
- `Labcoding-Server-Lifecycle-2.0.5-20261125.tar.gz`：39687字节，SHA-256 `3b764e0536780ac47bcd3d1f9d4bfc3e6b756f15f9500b69b489917797dd6fd2`。

服务端已上传并解压至`/home/server/.cache/labcoding-server-updates/service-lifecycle-2.0.5-20261125`，update.py SHA-256 `e2910c27b23009fcd4092af981e8ed2795840306ede0122fa6cc8d67e6be4dc9`。只读现场实测为160K/1槽、实际每槽163840；Ubuntu仓库为ubuntu/desktop-port、1c34e07b8c1157851ce43cd45cb4eee329d3367a且干净，与历史b42f9ae不同，不回退或修改；安装元数据仍2.0.4（用户标识2.0.4+x11fix2）。这些实测值均锁入新鲜基线。Gateway1648127、模型1660936及生产文件SHA在交付期间未变。

部署前检查（已只读验证，不改变模型）：
```bash
cd /home/server/.cache/labcoding-server-updates
sha256sum -c Labcoding-Server-Lifecycle-2.0.5-20261125.tar.gz.sha256
cd service-lifecycle-2.0.5-20261125
sha256sum -c SHA256SUMS
python3 -B update.py --check
```
正式更新及更新后核验（尚未执行，另行确认后由server终端执行）：
```bash
sudo python3 -B update.py --deploy
sudo python3 -B update.py --verify
```
手动代码回滚（单独执行）：
```bash
cd /home/server/.cache/labcoding-server-updates/service-lifecycle-2.0.5-20261125
sudo python3 -B update.py --rollback
```
更新/回滚只重启Gateway，安装root helper但不调用它；备份位于`/var/backups/labcoding-service-lifecycle-2.0.5-20261125`，仅手动部署时创建。失败自动恢复代码并核验；恢复失败保留HOLD。003仅新增服务审计表，回滚保留表和记录，不回灌旧数据库。若后续已暂停模型或有未完成生命周期/参数事务，禁止退回不能识别持久离线状态的旧代码，需先另行授权受控恢复。部署基线漂移或活动任务/直连请求存在会拒绝更新，不绕过检查。推荐先更新配套服务端并核验，再安装Windows2.0.5；旧Windows/Ubuntu原API保持兼容。

本次未部署生产、未启停真实模型、未扩sudoers、未变更生产上下文/槽位/数据库内容或Ubuntu客户端，未提交/推送。真实GPU启停、释放及冷启动峰值仍待用户明确确认；安装包和隔离测试通过不替代生产验收。

## Windows Labcoding 2.0.4 正式归档

Windows版本为2.0.4 / stable / release_sequence 7；包含六区模型管理页、双列同步状态、GPU资源及高级详情、六档上下文/并发槽位管理和内置OpenCode主动同步。Gateway配套源码包含受控维护降容、事务/版本/恢复、独立OpenCode文件同步及最终运行时核验热修复。Windows与Ubuntu客户端继续共享账号、认证、额度和全局任务槽位，本地项目及会话独立。

2026-11-25用户确认已完成生产部署及核心功能实机验收（本次归档不重新执行生产操作）：Windows2.0.4覆盖升级/启动、Gateway增量包及热修复部署、内置OpenCode配置与运行值、独立OpenCode未启动识别及正常重启后无推理的运行核验、管理页总体同步状态均正常。热修复部署未重启llama.cpp，账号及额度记录保持不变。

| 已验收总上下文 | 槽位 | 已核验每槽上下文 / OpenCode预算 |
| --- | --- | --- |
| 128K（131072） | 1 | 131072 |
| 160K（163840） | 1 | 163840 |
| 160K（163840） | 2 | 81920 |

当前生产为160K、2槽。本次仅本地Git归档，不改变现场。双平台真实并发任务、第三任务超额保护及槽位释放的生产联调仍待验收；已有隔离测试不能代替这三项实机证据。满上下文压力测试亦不作通过声明。独立OpenCode不强制自动重载的安全限制保留。

现有交付文件保留在仓库根目录`.work/releases/`，不纳入Git、不重新构建：

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| CCSLab-Labcoding-Win-2.0.4-Setup-x64.exe | 76907520 | b84e16f9ba7002b5910b8ffae8ff348c7fa0255670c8dddd1a82970d419d9500 |
| Labcoding-Server-Active-Sync-2.0.4-20261125.tar.gz | 25551 | e6de01b0183e744ca3c094ecb18659f8fc10828f5f73f8bfbe9a7d45c83f132f |
| Labcoding-Server-OpenCode-Runtime-Hotfix-20261125.tar.gz | 14413 | f0260f2abff3a2b8c432332260226c4cf47c173d26c9121cf9ed734c87ef0511 |

后两个包的服务器归档位置为`/home/server/.cache/labcoding-server-updates/`，解压目录分别为`active-sync-2.0.4-20261125`和`runtime-config-hotfix-20261125`。它们绑定各自部署前基线，不应在当前160K/2现场重复部署。Windows源码在热修复期间未额外修改，现有安装包继续对应最终Windows源码；服务端最终源码等于增量包加运行时热修复。

## 主动同步与运行核验

Windows 保存/重新同步成功后，复用现有生命周期重建已启动且空闲的内置引擎，等待真实/config读回再刷新模型页；同预算只读回，不重复建进程。未启动的引擎显示配置已就绪，不为状态展示启动常驻进程；活动任务/工具/审批继续受既有安全机制保护。刷新仅GET元数据及/config，不创建任务、不占槽、不发送模型请求。

Gateway的opencode_runtime.py仅观察固定server用户和固定独立二进制的实例。无关sshd/systemd等进程的exe权限失败不再中止扫描；目标候选进程不可读时仍报告尚未核验。识别父子进程及serve --stdio的真实HTTP监听，PID和端口动态发现，不硬编码。核对配置来源、PID/启动身份、同网络命名空间与socket归属后，只访问该实例的127.0.0.1监听。v2.3.0使用HTTP Basic Authentication，用户名opencode，优先读取子进程OPENCODE_PASSWORD，仅在内存使用，不返回客户端或写入日志。

只调用GET /api/info、/api/debug/location、/api/config和/api/model：枚举全部已加载位置，逐位置解析/api/config的List响应及每个document的info.providers.ccslab.models.lab-general.limit.context，按低到高优先级处理全部来源，不假定数组索引0；directory项仅为发现目录。再与/api/model实际构造值交叉核验，前后检查加载位置、进程身份和监听归属。多个实例或位置未全部核验时不以单个实例冒充总体成功。无代理、不跟随重定向，不启动/停止/重载实例，不产生推理或全局占槽调用。文件正确且无实例为ready，全部实际运行值一致才为synced；权限、认证、格式、退出重启或不一致均提供具体原因。保持原helper文件核验入口和降容退出条件不变。

v2.3.0的真实同实例测试确认：JSON写入后立即读回可能仍为旧值，原生reload（POST /api/location/reload）可以加载新值；其他运行实例也可能经原生异步配置观察自行更新。显式同步会短时只读复核，始终以实际API值为准。reload命令重建所有已加载位置，会取消待审批和表单，运行会话在下一步边界换服务；接口没有原子“仅空闲时重载”参数。先GET空闲再POST存在新任务竞态，Gateway因此不自动调用它。交互式私有/stdio实例无法取得可信HTTP运行值时，明确提示正常退出重启；不得将文件成功视为运行同步。

需要Gateway自动识别重新启动的独立实例时，沿用下文已验证的认证loopback serve + --server接入方式，在启动serve前明确配置：
```bash
export OPENCODE_CONFIG=/home/server/.config/opencode/opencode.json
```
在原项目目录启动，不公开端口或密码。Gateway也能识别正常交互式OpenCode的受认证HTTP子进程，自动枚举所有加载位置；无法完整读取时仍为尚未核验，不伪报成功。权限受限、认证不可取、未知版本、PID变化及配置来源不符均返回具体原因。正常重启或管理员在确认无任务/审批/表单且无并发操作时手动对同一实例执行原生reload后，刷新/重新同步会重新GET实际值（下方14096仅为人工启动示例端口，不是生产发现规则）：
```bash
/home/server/.opencode/bin/opencode --server http://127.0.0.1:14096 reload
```
此命令只能由管理员在上述安全条件满足时手动执行；私有交互实例优先正常退出重启，不自动取消审批。运行核验为只读，实际后台降容仍必须满足既有helper退出/直连维护保护。最终Gateway运行时热修复已由用户确认部署，归档保留原增量包和热修复包，不重复执行部署。

Windows Labcoding 2.0.4使用同一Gateway管理模型；当前Ubuntu Labcoding为2.0.4+x11fix2，其仓库/home/server/coding-agent/Labcoding的ubuntu/desktop-port分支及b42f9ae基线不修改、不构建、不升级。既有Ubuntu2.0.5兼容性验证作为历史证据保留，不将两个客户端版本混同。公共账号、审批、权限、认证会话、额度结算和全局任务租约继续共享；本地项目、会话及执行记录保持独立。

## 配置与兼容性

总上下文合法值为32768、65536、131072、147456、163840、196608（32/64/128/144/160/192K，1K=1024）。128K只用于初始化或恢复默认草稿，不能覆盖服务器现存160K。槽位仍为1–10；OpenCode使用已核验的实际每槽容量。

保留旧API路径、请求/响应语义及认证方式；新增状态字段为兼容扩展。旧Ubuntu管理页不提供新档位，使用Windows管理员页管理；旧版256K提交受六档白名单拒绝。其新引擎能读取128K/160K，原输出4096上限保留。旧会话预算过大时由Gateway拒绝并提示重连/重启及压缩。

Gateway对所有账号使用运行模型的模板和分词器核验“实际输入tokens+输出预算≤实际每槽容量”；不按字符估算，不使用旧显示预算。实际槽位容量或分词服务未知时返回503；超容量返回兼容的context_length_exceeded/400且不发送推理、不扣费。额度统计仍按实际usage结算。

## 安全降容操作

1. 等待所有平台任务、工具执行、审批和实际模型请求结束。
2. 正常退出服务器独立OpenCode，包括它的私有serve子进程。如果helper仍检测到旧Ubuntu Labcoding内置OpenCode，请正常退出该客户端；不修改安装或强杀进程。
3. Windows管理员页刷新，读取真实总上下文、槽位及每槽值；若独立文件与当前每槽值不一致，先点“重新同步”补齐当前值，此操作不重启模型。
4. 按实际每槽容量判断降容（例如160K/1→128K/1）；选择目标后点“保存服务器设置”，阅读维护说明后“确认应用”。不能仅依据总上下文判断增减容。
5. Gateway关闭新任务入口，复核租约/版本/PID/实际请求。固定root helper核实server用户的opencode进程已退出，建立临时网络屏障，再次核验空闲，才改后端。
6. helper重启固定模型服务，核验新PID、健康、环境、槽位和实际每槽n_ctx；以server身份只更新独立JSON的context，读回核验。原input、output、API Key、压缩和其他字段保留，0600与所有者保持，变更前保存同目录备份。
7. 后端与文件成功后解除屏障和维护状态，Gateway发布实际值，Windows沿用已有机制重建空闲引擎/读回加载值，新任务在获取租约后重新读取预算；刷新GPU及采样时间。
8. 正常重新启动独立OpenCode和需要重连的旧客户端。独立文件已写入不等于外部实例已加载；Gateway自动重新发现并可信读回后才标记运行时已同步。恢复原配置同样通过Windows保存；同目标重新同步不调用helper、不重复重启模型。

未退出进程、未知进程权限、活动请求、文件缺失/权限/基线不一致、nft不可用等分别返回原因，配置不改或失败后恢复。进程扫描仅检查PID、UID、exe，不发送信号、不读取会话。

## 固定安全边界

helper仍是原固定无参数sudo入口，只接受old_context/old_slots/new_context/new_slots/policy五字段。客户端不能选择命令、服务、UID或路径；不新增sudoers规则。

root配置 /etc/ccslab/model-control.json 新增可选字段：
```json
{"managed_opencode_config":"/home/server/.config/opencode/opencode.json"}
```
这是对已有JSON的单字段扩展，不能用上面片段覆盖完整文件。现有部署脚本保留原字段并添加它；helper只允许此固定路径。Gateway持久环境仍为CCSLAB_MANAGED_OPENCODE_CONFIG指向同一文件。

纳管后的每次后端参数变更均启用临时inet表ccslab_context_maintenance：拒绝本机非root到本机TCP8000、以及外部到TCP8000的请求（含已有连接、IPv4/IPv6），保留root本机健康探针和其他端口。降容额外核实OpenCode已退出并管理文件恢复；增容允许较小预算的空闲实例保留，文件补同步语义不变。同目标同步不创建屏障、不重启模型。规则不改Cloudflare、既有防火墙表或持久网络配置。固定模型API仍为http://172.17.0.1:8000。

/run/ccslab-model-context-maintenance 与既有pending阻止新Gateway任务。降容时helper负责后端和独立文件的同一事务；增容仍保留健康后端+文件部分完成的原重试机制。文件操作先降为server UID，再调用root安装的同版output_settings模块；root不以特权改写用户目录。

任何降容应用失败均尝试恢复后端和独立context；两者均核验成功才解除屏障。恢复失败保留pending与屏障，明确报告恢复失败，禁止盲目删除标记、修改sudoers或假报成功。运维需依据/etc/ccslab/model-context.previous及pending中的independent_context_before恢复核验；模型事务未恢复前，代码部署脚本拒绝回退控制组件。

## 历史增量部署：原160K/1基线，不重启模型

本节保留初次受控维护部署的历史入口，不是当前160K/2现场的操作指令。本次归档不访问或修改Ubuntu客户端仓库，不执行下列历史命令。后续配套更新及最终热修复已使用仓库外的.cache目录，见上方交付记录。

现有载荷目录：
/home/server/coding-agent/Labcoding/.work/context-sync-20261125/release-final/

```bash
cd /home/server/coding-agent/Labcoding/.work/context-sync-20261125/release-final
sha256sum -c SHA256SUMS
python3 -I -B update.py --check
sudo -v
sudo /usr/bin/python3 -I -B update.py --deploy
```

部署脚本核对原16项备份和现场，管理员认证后占用全局槽位并心跳；备份root私有drop-in、Gateway/helper/策略/配置和新鲜SQLite快照。只安装必要模块、固定helper、root库中的output_settings、纳管环境及策略字段。仅重启Gateway并用部署维护标记保护验证窗口，保持模型PID与160K/1，核验账号/额度/有效会话行；不恢复旧数据库、不改变模型权重/量化/KV/GPU/API Key。

正式部署前源码/备份/哈希不匹配时停止，不用旧131072/5首次接入脚本替代增量部署。正常登录/注销产生既有认证会话写入，不手工改写账号额度。

代码回滚（需先结束模型事务，模型回到160K/1；后续合法切换产生的新PID会被保留）：
```bash
cd /home/server/coding-agent/Labcoding/.work/context-sync-20261125/release-final
sha256sum -c SHA256SUMS
sudo -v
sudo /usr/bin/python3 -I -B update.py --rollback
```

回滚使用/var/backups/labcoding-context-sync-20261125-final，恢复原策略字段与代码，仅移除本轮新增且哈希仍匹配的文件，拒绝覆盖后续编辑；不重启模型、不回灌数据库。

## 独立OpenCode v2.3.0的同实例核验

等待任务结束，空闲前台用Ctrl+C正常退出并返回shell。若使用独立后台服务，确认空闲后使用原生opencode service stop正常停止；不删除会话。恢复私有模式：
```bash
/home/server/.opencode/bin/opencode --standalone
```

另一次--standalone api启动的是另一实例，不能证明先前交互实例已加载。需要同实例核验时，在原项目目录使用明确地址：
```bash
# 终端A，先确认14096空闲
read -rsp 'OpenCode local password: ' OPENCODE_PASSWORD; printf '\n'
export OPENCODE_PASSWORD
export OPENCODE_SERVER_PASSWORD="$OPENCODE_PASSWORD"
/home/server/.opencode/bin/opencode serve --hostname 127.0.0.1 --port 14096
```
```bash
# 终端B，同一项目目录、同一本机访问密码
read -rsp 'Same OpenCode local password: ' OPENCODE_PASSWORD; printf '\n'
export OPENCODE_PASSWORD
/home/server/.opencode/bin/opencode --server http://127.0.0.1:14096 api GET /api/model | python3 -c 'import json,sys; m=next(x for x in json.load(sys.stdin)["data"] if x["providerID"]=="ccslab" and x["id"]=="lab-general"); print({"model":m["id"],"limit":m["limit"]})'
/home/server/.opencode/bin/opencode --server http://127.0.0.1:14096
```
128K应读到131072，160K应读到163840；若项目配置覆盖全局文件，先核查来源。不要公开完整配置中的Key。此访问密码不改变模型API Key，Gateway也不会把人工核验冒充自动运行值上报。

GPU只读Ubuntu指定GPU的nvidia-smi，展示编号/型号/used/total/free/使用率/模型进程显存及服务器采样时间。free、reserved与total-used-free差额分别理解；读取失败为未知。GPU失败不回滚已健康应用的配置。

## 验证边界

隔离HTTP/SQLite/模型遥测覆盖160K→128K→160K、同目标免重启、并发/租约、文件/启动/恢复/屏障失败及旧客户端容量保护；POSIX验证server身份写入及0600。独立QEMU VM的网络命名空间实测本机/远端/IPv6/已有连接的阻断和解除，不在生产试写规则。旧Ubuntu2.0.5冻结接口与其真实内置OpenCode继续隔离验证，安装文件保持不动。

隔离证据仅证明软件行为，不能代替生产联调。上方列明的核心生产验收依据用户本次明确确认归档；双平台真实并发、第三任务超额保护及槽位释放仍待生产验收，满上下文压力测试未声明通过。后续生产变更仍须另行授权。
