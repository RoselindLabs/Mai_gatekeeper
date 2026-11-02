"""麦麦守门人（Mai_gatekeeper）配置模型。"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSectionSettings(PluginConfigBase):
    """插件总开关（对应 config.toml 的 [plugin] 节）。"""

    __ui_label__ = "插件设置"
    __ui_order__ = 0

    config_version: str = Field(
        default="1.0.0",
        description="配置 schema 版本，请勿手动修改。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本"},
    )
    enabled: bool = Field(
        default=True,
        description="插件总开关：关闭后停止 OneBot 连接、不再拦截发言、工具与命令全部停用",
        json_schema_extra={
            "label": "启用插件",
            "hint": "总开关。关闭后断开 OneBot 连接、不再拦截被屏蔽用户的发言，planner 工具与管理命令全部停用。",
        },
    )


class AdminSettings(PluginConfigBase):
    """管理员配置。

    管理员可执行 /fr、/block 管理命令，三通道任一命中即放行：
    1. 插件配置 admin_ids（名单为空时该通道关闭，fail-closed）；
    2. 主程序级操作员：bot_config.toml [plugin].permission（格式 platform:id）；
    3. WebUI 本地控制台（命令上下文的 is_local_operator 标志）。
    """

    __ui_label__ = "管理员"
    __ui_icon__ = "manage_accounts"
    __ui_order__ = 1

    admin_ids: list[str] = Field(
        default_factory=list,
        description="管理员用户 ID（QQ 号）列表；为空时管理命令全部拒绝",
        json_schema_extra={
            "label": "管理员 QQ 列表",
            "hint": "可执行 /fr、/block 命令并接收审批推送的管理员。此外，主程序级操作员（bot_config.toml [plugin].permission）与 WebUI 控制台始终放行。",
            "placeholder": "例如：[\"123456789\"]",
        },
    )
    platform: str = Field(
        default="qq",
        description="平台标识（管理员推送与默认屏蔽记录使用的平台）",
        json_schema_extra={
            "label": "平台标识",
            "hint": "管理员推送与屏蔽记录默认归属的平台，一般保持 qq 即可。",
        },
    )


class ApproveSettings(PluginConfigBase):
    """好友申请审批策略。"""

    __ui_label__ = "好友审批"
    __ui_icon__ = "how_to_reg"
    __ui_order__ = 2

    mode: Literal["auto", "manual", "hybrid"] = Field(
        default="hybrid",
        description=(
            "审批模式：auto=LLM 全自动决定；manual=全部推送管理员人工审批；"
            "hybrid=LLM 先审，高置信直接执行，低置信或可疑时转管理员"
        ),
        json_schema_extra={
            "label": "审批模式",
            "hint": "auto：LLM 全自动决定，不打扰管理员；manual：全部推送管理员私聊人工审批；hybrid（推荐）：LLM 先审，高置信直接执行，低置信或可疑时转管理员。",
        },
    )
    confidence_threshold: float = Field(
        default=0.7,
        description="hybrid 模式下 LLM 置信度达到该值才直接执行，否则转管理员（0~1）",
        json_schema_extra={
            "label": "置信度阈值",
            "hint": "仅 hybrid 模式生效：LLM 给出的置信度达到该值才直接执行，否则转人工。范围 0~1，默认 0.7。",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
        },
    )
    extra_rules: str = Field(
        default="",
        description="附加给 LLM 审批员的补充规则（例如「熟人群友通常会在验证消息里提到 XX 群」）",
        json_schema_extra={
            "label": "LLM 补充规则",
            "hint": "附加给 LLM 审批员的判断依据。例如：「熟人群友通常会在验证消息里提到 XX 群」「拒绝一切提到兼职的申请」。留空则只用默认标准。",
            "placeholder": "可留空",
        },
    )
    notify_admin_on_decision: bool = Field(
        default=True,
        description="好友申请审批完成后（同意/拒绝，任何路径：LLM 自动、管理员命令、屏蔽自动拒绝）是否向全部管理员推送结果",
        json_schema_extra={
            "label": "审批结果通知管理员",
            "hint": "开启后任何路径完成好友申请审批（同意或拒绝）都会向全部管理员私聊推送结果记录。建议保持开启。",
        },
    )


class ObSettings(PluginConfigBase):
    """OneBot v11 正向 WebSocket 连接配置。

    连接 NapCat / SnowLuma 对端（两者均为标准 OneBot v11 协议），
    由用户手动填写对端的地址、端口与访问令牌。
    """

    __ui_label__ = "OneBot 连接"
    __ui_icon__ = "cable"
    __ui_order__ = 3

    server: str = Field(
        default="",
        description="OneBot 对端地址（必填，例如 NapCat/SnowLuma 所在主机地址）",
        json_schema_extra={
            "label": "对端地址",
            "hint": "OneBot v11 正向 WebSocket 服务端地址。本机部署通常填 127.0.0.1。未填写时插件不启动连接。",
            "placeholder": "例如：127.0.0.1",
        },
    )
    port: int = Field(
        default=0,
        description="OneBot 对端端口（必填，0 表示未配置，插件不启动连接）",
        json_schema_extra={
            "label": "对端端口",
            "hint": "OneBot v11 正向 WebSocket 监听端口，可在 NapCat/SnowLuma 的网络设置中查看。",
            "placeholder": "例如：7988",
        },
    )
    token: str = Field(
        default="",
        description="OneBot 访问令牌（对端未开启令牌校验时留空）",
        json_schema_extra={
            "label": "访问令牌",
            "hint": "与 NapCat/SnowLuma 网络设置中的 access token 一致；对端未开启令牌校验时留空。",
            "placeholder": "可留空",
        },
    )
    heartbeat_interval: float = Field(
        default=30.0,
        description="WebSocket 心跳间隔（秒）",
        json_schema_extra={
            "label": "心跳间隔（秒）",
            "hint": "WebSocket ping 心跳间隔，与适配器保持一致即可，默认 30。",
        },
    )
    reconnect_delay_sec: float = Field(
        default=5.0,
        description="断线重连间隔（秒）",
        json_schema_extra={
            "label": "重连间隔（秒）",
            "hint": "对端断开后自动重连的等待时间，默认 5 秒。",
        },
    )
    action_timeout_sec: float = Field(
        default=15.0,
        description="action 调用超时（秒）",
        json_schema_extra={
            "label": "action 超时（秒）",
            "hint": "调用对端动作（同意好友、删好友等）的超时时间，默认 15 秒。",
        },
    )


class BlockSettings(PluginConfigBase):
    """屏蔽与拦截设置。"""

    __ui_label__ = "屏蔽"
    __ui_icon__ = "block"
    __ui_order__ = 4

    intercept_enabled: bool = Field(
        default=True,
        description="本插件 chat.receive.before_process Hook 运行时 abort 被屏蔽者的新消息，不保证先于其他插件",
        json_schema_extra={
            "label": "拦截被屏蔽用户的发言",
            "hint": "开启后本插件 Hook 丢弃被屏蔽者的新消息；不撤销先前插件任务或共享 Focus 历史。关闭后名单仍保留，仅停止拦截。",
        },
    )
    max_duration_hours: float = Field(
        default=24.0,
        description="planner 群聊屏蔽的时长上限（小时），到期自动解除；0 表示不限制（永久屏蔽）",
        json_schema_extra={
            "label": "群聊屏蔽时长上限（小时）",
            "hint": "planner 通过 block_user 屏蔽群成员的最大时长，到期自动解除（默认一天）。planner 可指定更短的时长，但不能超过该上限。0 表示不限制。管理员 /block add 的屏蔽不受此限制（永久）。",
            "min": 0.0,
        },
    )
    protect_user_ids: list[str] = Field(
        default_factory=list,
        description="额外保护名单：这些用户不可被 planner 屏蔽或删除好友",
        json_schema_extra={
            "label": "保护名单",
            "hint": "这些用户不可被 planner 屏蔽、也不可被删除好友（管理员自动在保护名单内）。可填写群主、亲友等重要账号。",
            "placeholder": "例如：[\"10000\", \"20000\"]",
        },
    )
    allow_remove_friend: bool = Field(
        default=False,
        description="是否启用私聊删好友工具（remove_friend，目标固定为当前对话对象，不可指定他人；删好友不可逆，默认关闭）",
        json_schema_extra={
            "label": "启用删好友工具",
            "hint": "是否向 planner 开放私聊删好友工具。工具只能删除当前私聊的对方，无法指定其他用户。删好友不可逆，默认关闭；确认需要时再改为 true。",
        },
    )
    notify_blocked_user: bool = Field(
        default=True,
        description="planner 屏蔽用户后，是否在当前会话发送提示（告知对方已被屏蔽）",
        json_schema_extra={
            "label": "屏蔽后发送提示",
            "hint": "开启后 planner 屏蔽用户成功时，会在群里发一条提示，告知对方已被屏蔽、后续发言将被忽略。",
        },
    )
    blocked_tip_template: str = Field(
        default="⚠️ {user} 已被屏蔽（{duration}），期间麦麦将不再回应其发言。",
        description=(
            "屏蔽提示文案模板：{user}=被屏蔽者昵称或ID，{duration}=屏蔽时长（如 24 小时，永久屏蔽时为「永久」）；留空用默认"
        ),
        json_schema_extra={
            "label": "屏蔽提示文案",
            "hint": "屏蔽成功后发送到当前会话的提示文案。可用占位符：{user}=被屏蔽者昵称或ID，{duration}=时长（如 24 小时；永久屏蔽时为「永久」）。留空使用默认文案。",
            "placeholder": "⚠️ {user} 已被屏蔽（{duration}），期间麦麦将不再回应其发言。",
        },
    )
    notify_admin_on_block: bool = Field(
        default=False,
        description="planner 屏蔽用户后是否向管理员推送通知",
        json_schema_extra={
            "label": "屏蔽后通知管理员",
            "hint": "开启后每次 planner 屏蔽用户都会向管理员私聊推送通知（含原因与解除方法）。",
        },
    )
    notify_admin_on_remove: bool = Field(
        default=True,
        description="planner 删除好友后是否向管理员推送通知",
        json_schema_extra={
            "label": "删好友后通知管理员",
            "hint": "开启后每次 planner 删除好友都会向管理员私聊推送通知（删好友不可逆，建议保持开启）。",
        },
    )


class GateKeeperConfig(PluginConfigBase):
    """麦麦守门人完整配置。"""

    __ui_label__ = "麦麦守门人"
    __ui_icon__ = "shield"
    __ui_order__ = 0

    plugin: PluginSectionSettings = Field(default_factory=PluginSectionSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    approve: ApproveSettings = Field(default_factory=ApproveSettings)
    ob: ObSettings = Field(default_factory=ObSettings)
    block: BlockSettings = Field(default_factory=BlockSettings)
