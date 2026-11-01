# 更新日志

## 0.6.7（2026-11-01）

- README 补充环境要求小节（MaiBot ≥ 1.3.0、SDK 2.6.0 ~ 2.91.99）；功能表标注 `remove_friend` 默认关闭；自测项数更正

## 0.6.6（2026-10-26）

- 放宽 SDK 兼容上限至 2.91.99

## 0.6.5（2026-10-25）

- 安全默认值：`block.allow_remove_friend` 代码默认值改为 **false**（删好友不可逆；此前运行时默认生成的配置与 config.example.toml 不一致，未复制示例文件的用户开箱即是开启状态）。需要该功能的用户显式改为 true
- 新增"出厂默认配置安全姿态"自测：断言默认关闭删好友、拦截/审批/通知的默认值符合预期

## 0.6.4（2026-10-24）

- 移除"两步防误删"功能（`block.remove_requires_block` 配置项及检查逻辑）——v0.6.3 起 `remove_friend` 目标已固定为当前私聊对象，该开关不再必要；旧配置中的残留键会被自动忽略

## 0.6.3（2026-10-24）

- **安全修复（remove_friend 任意目标删除）**：移除工具的 `user_id` 参数——此前 LLM 传入同名参数会覆盖 Host 注入的真实发送者（工具参数优先的合并规则），等于私聊对象可诱导机器人删除号主好友列表中的任意一人，且删后自动全局屏蔽、其后续好友申请被自动拒绝。现在目标**固定为 Host 注入的当前私聊对象**，LLM 无任何途径指定他人；无法确定对话对象时拒绝执行（fail-closed）
- 两步防误删提示与配置说明同步更新（开启后需管理员先 `/block add`）

## 0.6.2（2026-10-24）

- 新增：好友申请审批结果统一通知——任何路径（LLM 自动 / 管理员 `/fr` 命令 / 命中屏蔽自动拒绝）完成同意或拒绝后，向**全部**管理员推送结果记录（`approve.notify_admin_on_decision`，默认开启；替代原 `push_admin_on_auto`，其只覆盖 LLM 自动路径）
- planner 删好友的通知（`block.notify_admin_on_remove`）自 0.6.0 起已有，保持不变

## 0.6.1（2026-10-24）

- 修复：管理员鉴权补齐主程序级操作员通道——`is_local_operator` 只标记 WebUI 控制台消息，QQ 侧操作员现按 bot_config `[plugin].permission`（`platform:id`）比对放行；命令解析依赖的 `text` / `is_local_operator` kwargs 已对照 Host 1.3.4 源码核实为实际下发（官方文档滞后）
- 修复：README 移除已废弃的配置继承说明（0.2.0 起 `[ob]` 全部手填）
- 插件类型改为 `management`（管理/权限类插件）
- manifest 新增 `config.get` 能力声明（读取主程序操作员配置）

## 0.6.0（2026-10-24）

- 新增：删好友后转告管理员（`block.notify_admin_on_remove`，默认开启）
- 仓库整理：`config.example.toml` 配置示例、`.gitignore`、`LICENSE`、本更新日志

## 0.5.0（2026-10-24）

- 新增：屏蔽提示文案可自定义模板（`block.blocked_tip_template`，占位符 `{user}` / `{duration}`），留空回退默认
- 修复：屏蔽到期时间截断到秒导致短时长屏蔽立即过期的精度问题（统一微秒精度）

## 0.4.0（2026-10-24）

- 新增：群聊屏蔽时长上限（`block.max_duration_hours`，默认一天，到期自动解除；`block_user` 可指定更短时长，被钳制在上限内；0 = 不限制）
- `/block list` 显示每条屏蔽的到期时间；后台每 5 分钟清扫到期记录；旧库自动迁移 `expires_at` 列

## 0.3.0（2026-10-24）

- 工具作用域收紧：`block_user` / `unblock_user` 仅群聊（屏蔽本群），`remove_friend` 仅私聊
- 新增：屏蔽成功后向当前会话发送提示（`block.notify_blocked_user` + `blocked_tip_template` 前身）

## 0.2.0（2026-10-24）

- 移除 snowluma-adapter 配置自动继承，OneBot 连接改为用户手动填写（`[ob]` server/port/token）

## 0.1.0（2026-10-24）

- 首个版本：好友申请审批（LLM 自动 / 管理员人工 / 混合三种模式）、用户屏蔽（发言拦截、私聊删好友）、管理命令 `/fr` 与 `/block`
- 自建 OneBot v11 正向 WebSocket 直连 NapCat / SnowLuma 对端，不修改任何适配器
