# Mai_gatekeeper 麦麦守门人

MaiBot 插件：好友申请审批（LLM 自动 / 管理员人工 / 混合）+ 用户屏蔽（发言拦截、私聊删好友）。

> 配置示例见 [`config.example.toml`](config.example.toml)：复制为 `config.toml` 后按需修改（首次加载也会自动生成默认配置）。更新历史见 [CHANGELOG.md](CHANGELOG.md)。

## 环境要求

- MaiBot ≥ 1.3.0（插件 SDK 2.6.0 ~ 2.91.99，见 `_manifest.json`）
- 对端为标准 OneBot v11 正向 WebSocket 服务端（NapCat / SnowLuma 均可），且允许多个客户端同时连接

## 安装

1. 把本目录放入 MaiBot 的 `plugins/` 目录（OneKey 桌面版为 `<模块根>\plugins\Mai_gatekeeper\`），或通过 WebUI 插件市场安装 zip；
2. 复制 `config.example.toml` 为 `config.toml`（或保持缺省，首次加载自动生成默认配置），填写 `[admin].admin_ids` 与 `[ob]` 的对端地址/端口/令牌；
3. 重启 MaiBot 或在 WebUI 重载插件；日志出现「守门人已连接 OneBot 对端」即部署成功。

## 功能

| 功能 | 入口 | 说明 |
|------|------|------|
| 好友申请审批 | 自动（WS 事件） | `approve.mode`：`auto`（LLM 全自动）/ `manual`（推送管理员）/ `hybrid`（LLM 先审，低置信转人工，默认）；命中屏蔽名单的申请人一律直接拒绝 |
| 屏蔽用户 | planner 工具 `block_user`（**仅群聊**） | 屏蔽作用域为本群，**受时长上限约束**（`max_duration_hours`，默认一天，到期自动解除；planner 可指定更短时长）；屏蔽成功后会在群里发一条提示告知对方；被屏蔽者消息在 `chat.receive.before_process` 阶段被完全丢弃（不入库、不回复、不计频率） |
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
- `friend_requests`：好友申请审批记录（flag / user_id / nickname / comment / status / decision）

## 命令示例

```
/fr                    # 查看待处理好友申请（含序号）
/fr ok 3               # 同意 3 号申请
/fr no 3               # 拒绝 3 号申请
/block                 # 查看屏蔽名单
/block un 5            # 按记录序号解除
/block un 123456       # 按用户ID解除全部
/block add 123456 global 广告号
/block add 123456 group:987654 仅此群
```

## 测试

```
tests/run_self_test.py       # 插件逻辑 mock 自测（105 项，含 on_load 全流程、审批/屏蔽/命令全链路与出厂默认配置安全姿态）
tests/run_ob_client_test.py  # OneBotClient 对本地模拟对端的集成测试（12 项：鉴权/echo/事件/重连）
```

## 连接配置说明

`[ob]` 由用户手动填写 OneBot v11 对端（NapCat / SnowLuma 均为标准协议）的正向 WebSocket 地址、端口与访问令牌——与适配器所连的是同一个对端，插件以第二个客户端身份并存连接。`server`/`port` 未填写时插件不启动连接（仅告警一次）；补全并保存配置后会自动热更新并连接。

## 已知边界

- QQ 好友申请 flag 有时效，管理员晚回复可能过期（命令会提示并标记 expired）；
- 对端需允许多个 WS 客户端同时连接（NapCat 支持；snowluma-adapter 本身保持连接不冲突）；
- 被屏蔽者发送的命令同样会被拦截（拦截点在命令链之前）。
