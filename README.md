# Mai_gatekeeper 麦麦守门人

MaiBot 插件：好友申请审批（LLM 自动 / 管理员人工 / 混合）+ 用户屏蔽（发言拦截、私聊删好友）。

本仓库为 [octmicy 原作](https://github.com/octmicy/Mai_gatekeeper) 的定向维护 fork（0.6.10），原 MIT 作者与历史保留，见 [NOTICE.md](NOTICE.md)。插件 ID 仍为 `octmicy.mai-gatekeeper`，不是另一份插件。

三个管理工具仅支持 QQ，声明 `platforms="qq"`，在支持平台过滤的 Host 上不向 TG 会话展示或执行。通用入站屏蔽 Hook 仍按平台隔离记录并跨平台拦截。共享 Focus 中保留 QQ 群聊屏蔽/解封和 QQ 私聊删除分支；原权限、名单及 unknown 账本不变。

离线平台合成验证：在安装真实 Host 依赖的环境运行 `python scripts/verify_platform_scope.py --host-source <Host源码目录>`。可用 `--plugin-source <插件目录>`复核其它插件的 QQ 工具。已验证 SDK2.22.1 / Host `f53828ef` 的真实注册、过滤和混合平台 Focus 路由；只替换权限配置、文件服务及 RPC 传输，不调用模型或真实平台。标准 CI 继续验证最低 SDK 与独立源码包。

### 异常好友申请

身份明确、首次收到的申请沿原审批模式处理。UIN=0/身份未知或重复flag无法确认新一轮时，管理员用 `/fr review` 查看原因，并在QQ客户端处理。异常不调用模型或审批动作，也不自动通知刷屏；日志有简短提示。

独立异常展示表只保最近128条（同账号/flag合并），满后淘汰最旧展示记录，不暂停其他申请人。对应旧pending永久转review，淘汰展示记录也不能恢复审批；原终态、unknown及动作账本不删除。已发出的动作按原结果落账，不取消重放。未知身份的展示记录被淘汰后，若将来首次取得可信QQ号且没有原审批记录，按正常首次申请处理。`/fr review`仅显示当前账号最近10条，不可通过`ok/no`执行。

> 配置示例见 [`config.example.toml`](config.example.toml)：复制为 `config.toml` 后按需修改（首次加载也会自动生成默认配置）。更新历史见 [CHANGELOG.md](CHANGELOG.md)。

## 环境要求

- 当前验证目标 MaiBot 1.3.19 / SDK 2.20.0，Python 3.12+；需要宿主现有 `neko.tools.context.v1` 可信工具身份桥。缺桥或缺可信账号/会话身份时敏感工具拒绝执行，不信任模型传入的普通 kwargs。
- 共享Focus无锚点时，群工具须给当前群真实message_id，经现有message.get_by_id核实会话/群/平台/账号与已登录OneBot一致；仅user_id不能补全未知账号。私聊可用现有查流接口核验真实目标，不创建新流补身份，不增加能力权限。
- 对端为标准 OneBot v11 正向 WebSocket 服务端（NapCat / SnowLuma 均可），且允许多个客户端同时连接

## 安装

1. 保留现有安装目录 `plugins/octmicy_mai-gatekeeper/`、配置和数据目录，**不要同时安装第二份**。源码准备/CI 成功不等于已部署；发布应使用部署环境已有的受控插件更新流程。
2. 已有实例不得覆盖 `config.toml` 或重建数据库；新实例才按 `config.example.toml` 配置。升级不要求改 mode、阈值、管理员名单或开启删好友。
3. 需要授权后热重载并检查新加载日志。WS 连上不等于账号身份确认或真实审批送达；插件通过严格成功的 `get_login_info` 建立账号归属，之后才接受匹配 `self_id` 的事件。

## 功能

| 功能 | 入口 | 说明 |
|------|------|------|
| 好友申请审批 | 自动（WS 事件） | `approve.mode`：`auto`（LLM 全自动）/ `manual`（推送管理员）/ `hybrid`（LLM 先审，低置信转人工，默认）；命中屏蔽名单的申请人一律直接拒绝 |
| 屏蔽用户 | planner 工具 `block_user`（**仅群聊**） | 屏蔽作用域为可信上下文的本群，受时长上限约束（默认 24h）；消息在本插件 `chat.receive.before_process` Hook 运行时可 abort，**不保证先于其他第三方 Hook**。管理员、Host 操作员、机器人自己和保护名单不可被 planner 屏蔽/删除 |
| 解除屏蔽 | planner 工具 `unblock_user`（**仅群聊**）/ `/block un` | |
| 删除好友 | planner 工具 `remove_friend`（**仅私聊**，**默认关闭**） | 私聊中 planner 唯一可用的守门人工具；**目标固定为当前对话对象，工具不接受指定其他用户**（防诱导删除任意好友）；删除后自动全局屏蔽并通知管理员。需在配置中显式开启（`allow_remove_friend`） |
| 管理命令 | `/fr list\|ok\|no`、`/block list\|un\|add` | 管理员 = `[admin].admin_ids` ∪ 主程序级操作员（bot_config `[plugin].permission`，格式 `platform:id`）∪ WebUI 本地控制台；admin_ids 为空时仅后两者可用 |

## 配置（config.toml）

```toml
[admin]
admin_ids = ["你的QQ号"]   # 管理员；为空时管理命令全部拒绝

[approve]
mode = "hybrid"            # auto / manual / hybrid
confidence_threshold = 0.7 # hybrid 下 LLM 置信度阈值
extra_rules = ""           # 给 LLM 审批员的补充规则
notify_admin_on_decision = true # 任何路径完成审批后向全部管理员推送结果

[ob]
server = "127.0.0.1"      # OneBot v11 对端地址（NapCat / SnowLuma 通用，必填）
port = 7988               # 对端正向 WebSocket 端口（必填）
token = ""                # 访问令牌，与对端网络设置一致；未开启校验时留空

[block]
intercept_enabled = true
max_duration_hours = 24.0   # planner 群聊屏蔽的时长上限（小时），到期自动解除；0 = 不限制
protect_user_ids = []      # 额外保护名单（不可被屏蔽/删好友）
allow_remove_friend = false  # 删好友不可逆，默认关闭；确认需要时再开启
notify_blocked_user = true # 屏蔽成功后向当前会话发送提示
# 提示文案模板：{user}=昵称或ID，{duration}=时长（如 24 小时；永久屏蔽时为「永久」）；留空用默认
blocked_tip_template = "⚠️ {user} 已被屏蔽（{duration}），期间麦麦将不再回应其发言。"
notify_admin_on_block = false
notify_admin_on_remove = true  # 删好友后转告管理员（不可逆操作，建议开启）
```

## 数据

持久化在统一插件数据目录 `data/plugins/octmicy.mai-gatekeeper/gatekeeper.db`（SQLite）：

- `blocked_users`：屏蔽名单（platform / user_id / scope / reason / blocked_by / blocked_at）
- `friend_requests`：好友申请审批记录及 `account_id`。状态为 pending → executing → approved/rejected/failed/unknown；旧 expired 记录保留。
- `action_ledger`：不可逆删除的持久 claim；按平台/账号/用户去重，成功、失败或未知均不自动重放。

首次迁移前先做 SQLite 完整性检查，既有库用 SQLite backup 保存为 `gatekeeper.db.pre-0.6.8.bak`（已有备份不覆盖），事务增量迁移 `user_version=1`，不删 blocked/pending/历史账本。损坏或未知版本直接失败，不创建替代空库。进程重载时 executing 改为 unknown；超时/断连/取消或格式矛盾的响应也不能证明对端未执行。

旧 pending 的账号归属为空时保持原记录，不自动认领、重放或借用当前连接身份；需要管理员对照对端记录人工核验/处理。`/fr list unknown`、`/fr list failed` 可查看异常结果；本版不提供自动清 ledger/重试接口。换回旧代码时**不能让旧代码打开已迁移库**；仅在停止插件、人工核验所有外部动作后，另行评审备份恢复方案。恢复旧库也可能丢失已发送动作的记录，不是安全的自动回滚办法。

## 命令示例

```
/fr                    # 查看待处理好友申请（含序号）
/fr ok 3               # 同意 3 号申请
/fr no 3               # 拒绝 3 号申请
/fr list unknown       # 查看未知结果，核验前禁止重放
/fr list failed        # 明确失败，不自动推断为 flag 过期
/block                 # 查看屏蔽名单
/block un 5            # 按记录序号解除
/block un 123456       # 按用户ID解除全部
/block add 123456 global 广告号
/block add 123456 group:987654 仅此群
```

## 测试

```
python -m pip install "maibot-plugin-sdk==2.20.0" "aiohttp>=3.12,<4"
python -X utf8 scripts/check.py    # 继承 105 项断言 + 定向回归；真实 SDK，底层 RPC/SQLite/动作均为合成 fixture
python -X utf8 scripts/package.py  # 白名单确定性源码包；隔离子进程中真实 SDK 发现组件与临时实例生命周期
```

## 连接配置说明

`[ob]` 由用户手动填写 OneBot v11 对端（NapCat / SnowLuma 均为标准协议）的正向 WebSocket 地址、端口与访问令牌——与适配器所连的是同一个对端，插件以第二个客户端身份并存连接。独立连接用于接收好友申请事件，有合理用途，不强迁适配器。清空 `server`/`port` 会关闭旧连接；配置变化与外部动作受生命周期锁协调。已发送动作不能被配置撤销；仍在 LLM 审核或等待执行的旧 generation 不得继续发送。

离线 gate 禁止真实网络（仅允许 Windows stdlib socketpair 初始化事件循环），不运行原上游 `run_ob_client_test.py` 的回环 WS 集成入口，不接生产 WS、QQ 或 LLM。CI 两组为 Linux/Python3.14.4 与 Windows/Python3.12、SDK2.20，实际执行 Run tests / Verify source package，并设 ci-gate / publish-gate；没有生产 secrets、自动部署或 Release。隔离打包验证不是实际 Host/Runner 进程、QQ 投递或真实模型质量证明。

## 已知边界

- QQ 好友申请 flag 有时效，但一般 action 失败/超时不代表过期；本版保守记录 failed/unknown，禁止未确认重放；动作结果先落库，通知失败不重做动作。
- 对端需允许多个 WS 客户端同时连接（NapCat 支持；snowluma-adapter 本身保持连接不冲突）；
- 被屏蔽者发送的命令同样会被拦截（拦截点在命令链之前）。
- 原GIF<=0.2.0与本插件同为BLOCKING/EARLY，同槽排序令GIF先运行；维护实例后续已独立热更GIF0.2.1到NORMAL，门卫EARLY可先abort，真实Host Dispatcher/Runner合成验证屏蔽消息不进GIF。此协调不修改插件ID/宿主、不保证拦截所有更早第三方副作用，也不合并原生表情标签与GIF描述的模型费用。
- 屏蔽只过滤未来入站消息，不删除共享 Focus 旧历史，也不取消其他插件已启动的任务。工具使用共享 Focus 已路由成员的真实 session/account，不伪造 session；当前可信信封没有账号身份时保守拒绝工具。
- 独立 OneBot 网络动作不是 Host capability 沙箱，manifest 权限不能约束所有传输副作用。LLM 自报 confidence 不是身份验证，验证消息/昵称是不可信输入，仍可能提示注入；本版不改变用户选择的 hybrid/0.7 策略。
- 最多 16 个并发事件任务、32 个待响应动作；过载丢弃新自动审核工作并告警，需管理员检查对端，不能宣称永不丢事件。验证消息截断 2000 字符、昵称 100、补充规则 4000、LLM 输出 8192，审核超时 60s。日志只记序号/状态/异常类型，不记录 flag、token、验证消息、模型回复或异常原文；管理员通知仍包含处理所需信息。
