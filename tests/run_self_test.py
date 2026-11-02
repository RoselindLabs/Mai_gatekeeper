"""麦麦守门人自测脚本（mock ctx，不依赖真实 Host 与真实对端）。

覆盖：
- on_load 全流程（含数据目录创建、连接配置继承解析）——技能陷阱 #12：
  mock 注入配置不会执行 on_load，必须真跑
- 拦截 Hook（全局/群级屏蔽、未屏蔽放行、开关关闭放行）
- LLM 审批输出解析（纯净 JSON / 围栏 / 杂文 / 缺字段）
- 好友申请流：manual 推送、auto 直接执行、hybrid 低置信转人工、
  命中屏蔽自动拒绝、重复 flag 去重、flag 失效标记 expired
- planner 工具：block_user（message_id 反查/保护名单/重复屏蔽）、
  unblock_user、remove_friend（目标固定为当前对话对象、自动全局屏蔽）
- 管理命令：/fr list|ok|no、/block list|add|un、管理员判定（含 is_local_operator）
- 三元组返回值、chat_scope 顶层注入、工厂函数
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any
from maibot_sdk.context import PluginContext

SDK_PATH = os.environ.get("MAIBOT_SDK_PATH", r"D:\MaiBot\plugin\maibot-plugin-sdk-2.13.0")
PLUGIN_ROOT = r"D:\workdoc\plugin"

for p in (SDK_PATH, PLUGIN_ROOT):
    if p and p not in sys.path:
        sys.path.insert(0, p)


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[int, str]] = []

    def _log(self, level: int, msg: str) -> None:
        self.records.append((level, msg))

    def debug(self, msg: str, *args: Any, **kw: Any) -> None:
        self._log(logging.DEBUG, msg)

    def info(self, msg: str, *args: Any, **kw: Any) -> None:
        self._log(logging.INFO, msg)

    def warning(self, msg: str, *args: Any, **kw: Any) -> None:
        self._log(logging.WARNING, msg)

    def error(self, msg: str, *args: Any, **kw: Any) -> None:
        self._log(logging.ERROR, msg)


class FakeSend:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        self.sent.append({"text": text, "stream_id": stream_id})
        return True


class FakeConfigCap:
    """mock ctx.config：get_plugin 返回宿主原始结构；get 读主程序配置。"""

    def __init__(self, plugin_configs: dict[str, dict[str, Any]] | None = None) -> None:
        self.plugin_configs = plugin_configs or {}
        self.values: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    async def get_plugin(self, plugin_name: str | None = None) -> dict[str, Any]:
        value = self.plugin_configs.get(str(plugin_name))
        if value is None:
            return {"success": False, "value": None, "error": "未找到插件配置"}
        return {"success": True, "value": value}


class FakeLLM:
    def __init__(self, response: str = "", fail: bool = False) -> None:
        self.response = response
        self.fail = fail
        self.calls: list[Any] = []

    async def generate(self, prompt: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        if self.fail:
            return {"success": False, "response": ""}
        return {"success": True, "response": self.response}


class FakeChat:
    def __init__(self) -> None:
        self.sessions: dict[str, str] = {"10001": "stream-admin-10001"}

    async def get_stream_by_user_id(self, user_id: str, platform: str = "qq") -> dict[str, Any]:
        sid = self.sessions.get(user_id)
        if sid:
            return {"success": True, "stream": {"stream_id": sid, "account_id": "90000",
                    "platform": platform, "user_id": user_id, "chat_type": "private"}}
        return {"success": True, "stream": None}

    async def open_session(self, platform: str = "qq", chat_type: str = "private", *, user_id: str = "", **kw: Any) -> dict[str, Any]:
        sid = f"stream-{user_id}"
        self.sessions[user_id] = sid
        return {"success": True, "created": True, "stream": {"stream_id": sid, "account_id": kw.get("account_id"),
                "platform": platform, "user_id": user_id, "chat_type": "private"}, "stream_id": sid}


class FakeMessage:
    def __init__(self, messages: dict[str, dict[str, Any]] | None = None) -> None:
        self.messages = messages or {}

    async def get_by_id(self, message_id: str, **kwargs: Any) -> dict[str, Any]:
        msg = self.messages.get(str(message_id))
        if msg is None:
            return {"success": False, "error": "未找到消息"}
        return {"success": True, "message": msg}


class FakePaths:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class FakeCtx(PluginContext):
    def __init__(self, data_dir: Path, plugin_configs: dict[str, dict[str, Any]] | None = None) -> None:
        self.fixtures = {
            "send": FakeSend(), "config": FakeConfigCap(plugin_configs), "llm": FakeLLM(),
            "chat": FakeChat(), "message": FakeMessage(),
        }
        async def rpc(method, plugin_id, payload, **kwargs):
            section, name = payload["capability"].split(".", 1)
            args = payload.get("args") or {}
            if section == "config" and name == "get":
                return {"success": True, "value": await self.fixtures[section].get(**args)}
            if section == "send":
                await self.fixtures[section].text(**args)
                return {"success": True}
            return await getattr(self.fixtures[section], name)(**args)
        super().__init__("octmicy.mai-gatekeeper", rpc_call=rpc)
        self.send.sent = self.fixtures["send"].sent
        self.config.values = self.fixtures["config"].values
        self.config.values["plugin.permission"] = []
        self.paths = FakePaths(data_dir)


class FakeObClient:
    """假 OneBot 客户端：记录 action，按脚本返回。"""

    def __init__(self, ok: bool = True, fail_actions: set[str] | None = None) -> None:
        self.connected = True
        self.self_id = "90000"
        self.ok = ok
        self.fail_actions = fail_actions or set()
        self.actions: list[dict[str, Any]] = []

    async def stop(self) -> None:
        self.connected = False

    async def call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.actions.append({"action": action, "params": params})
        if action in self.fail_actions:
            return {"status": "failed", "retcode": 1200, "word": "FLAG已被处理或过期"}
        if action == "get_stranger_info":
            return {"status": "ok", "retcode": 0, "data": {"nickname": f"昵称{params.get('user_id')}"}}
        return {"status": "ok", "retcode": 0, "data": {}}


def base_config() -> dict[str, Any]:
    return {
        "plugin": {"enabled": True, "config_version": "1.0.0"},
        "admin": {"admin_ids": ["10001"], "platform": "qq"},
        "approve": {
            "mode": "manual",
            "confidence_threshold": 0.7,
            "extra_rules": "",
            "notify_admin_on_decision": True,
        },
        "ob": {
            "server": "",
            "port": 0,
            "token": "",
            "heartbeat_interval": 30.0,
            "reconnect_delay_sec": 0.1,
            "action_timeout_sec": 15.0,
        },
        "block": {
            "intercept_enabled": True,
            "max_duration_hours": 24.0,
            "protect_user_ids": ["20002"],
            "allow_remove_friend": True,  # 用例显式开启；代码默认值为 False（见 test_default_config_safety）
            "notify_blocked_user": True,
            "blocked_tip_template": "⚠️ {user} 已被屏蔽（{duration}），期间麦麦将不再回应其发言。",
            "notify_admin_on_block": False,
            "notify_admin_on_remove": True,
        },
    }


def make_plugin(overrides: dict[str, Any] | None = None) -> tuple[Any, FakeCtx]:
    from Mai_gatekeeper.config import GateKeeperConfig
    from Mai_gatekeeper.plugin import MaiGateKeeper

    cfg = base_config()
    for section, values in (overrides or {}).items():
        cfg.setdefault(section, {}).update(values)

    tmp_dir = Path(tempfile.mkdtemp(prefix="gatekeeper_test_"))
    ctx = FakeCtx(tmp_dir)

    plugin = MaiGateKeeper()
    plugin._set_context(ctx)
    plugin._plugin_config_instance = GateKeeperConfig(**cfg)
    # Synthetic Host bridge, not model kwargs. Production methods are tested unwrapped separately.
    for name in ("tool_block_user", "tool_unblock_user", "tool_remove_friend"):
        method = getattr(plugin, name)
        async def invoke(_method=method, _name=name, **kw):
            group = str(kw.get("group_id") or "")
            sid = str(kw.get("stream_id") or ("stream-g-" + group if group else "private"))
            kw.setdefault("_host_context", {
                "contract": "neko.tools.context.v1", "session_id": sid, "stream_id": sid,
                "platform": kw.get("platform", "qq"), "group_id": group,
                "user_id": kw.get("user_id", "") if _name == "tool_remove_friend" else "60000",
                "account_id": "90000",
            })
            return await _method(**kw)
        setattr(plugin, name, invoke)
    original_event = plugin._on_ob_event
    async def event(payload):
        return await original_event({"self_id": "90000", **payload})
    plugin._on_ob_event = event
    plugin.__test_tmp_dir = tmp_dir  # 便于用例清理
    return plugin, ctx


def make_hook_message(user_id: str, group_id: str = "", platform: str = "qq") -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_info": {
            "user_info": {"user_id": user_id, "user_nickname": f"用户{user_id}"},
        },
        "platform": platform,
    }
    if group_id:
        message["message_info"]["group_info"] = {"group_id": group_id, "group_name": "测试群"}
    return {"message": message}


def row_id_by_flag(store: Any, flag: str) -> int:
    row = store._conn.execute("SELECT id FROM friend_requests WHERE flag = ?", (flag,)).fetchone()
    return int(row["id"])


RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" —— {detail}" if detail and not condition else ""))


async def test_lifecycle_and_manual_config() -> None:
    """on_load 真跑 + 手动配置解析 + on_unload 干净退出 + 配置热更新补全。"""
    # 手动配置完整 → 客户端启动
    plugin, _ctx = make_plugin(overrides={"ob": {"server": "127.0.0.1", "port": 59998}})
    await plugin.on_load()
    check("on_load 后 store 已初始化", plugin._store is not None)
    check(
        "配置完整时启动客户端",
        plugin._client is not None and plugin._active_ob_config is not None and plugin._active_ob_config.port == 59998,
    )
    await asyncio.sleep(0.05)
    await plugin.on_unload()
    check("on_unload 后客户端与 store 已清理", plugin._client is None and plugin._store is None)

    # 配置不完整 → 不启动连接，仅告警
    plugin2, _ctx2 = make_plugin()
    await plugin2.on_load()
    check("配置不完整时不启动客户端", plugin2._client is None and plugin2._active_ob_config is None)
    check("store 仍正常初始化", plugin2._store is not None)
    await plugin2.on_unload()

    # 用户补全配置（热更新）→ 自动启动
    plugin3, _ctx3 = make_plugin()
    await plugin3.on_load()
    check("补全前客户端未启动", plugin3._client is None)
    plugin3._plugin_config_instance.ob.server = "127.0.0.1"
    plugin3._plugin_config_instance.ob.port = 59999
    plugin3._plugin_config_instance.ob.token = "t3"
    await plugin3.on_config_update("self", {}, "1.0.0")
    check(
        "配置热更新补全后启动客户端",
        plugin3._client is not None and plugin3._active_ob_config.port == 59999 and plugin3._active_ob_config.token == "t3",
    )

    # 连接参数变更 → 重启连接（配置对象更新）
    plugin3._plugin_config_instance.ob.port = 59997
    await plugin3.on_config_update("self", {}, "1.0.0")
    check("连接参数变更后配置已更新", plugin3._active_ob_config.port == 59997)
    await plugin3.on_unload()


async def test_hook_intercept() -> None:
    """拦截 Hook 的作用域与开关。"""
    plugin, _ctx = make_plugin()
    await plugin.on_load()
    store = plugin._require_store()

    store.add_blocked("qq", "30001", "global", "测试全局", "test")
    result = await plugin.intercept_blocked_message(**make_hook_message("30001", group_id="70001"))
    check("全局屏蔽：群聊消息被 abort", result == {"action": "abort"})
    result = await plugin.intercept_blocked_message(**make_hook_message("30001"))
    check("全局屏蔽：私聊消息被 abort", result == {"action": "abort"})

    store.add_blocked("qq", "30002", "group:70001", "测试群级", "test")
    check(
        "群级屏蔽：本群消息被 abort",
        (await plugin.intercept_blocked_message(**make_hook_message("30002", group_id="70001"))) == {"action": "abort"},
    )
    check(
        "群级屏蔽：其他群放行",
        (await plugin.intercept_blocked_message(**make_hook_message("30002", group_id="70002"))) is None,
    )

    check("未屏蔽用户放行", (await plugin.intercept_blocked_message(**make_hook_message("30003"))) is None)
    check("无 message 的载荷放行", (await plugin.intercept_blocked_message(**{"other": 1})) is None)

    # 开关关闭
    plugin._plugin_config_instance.block.intercept_enabled = False
    check("intercept_enabled=False 时放行", (await plugin.intercept_blocked_message(**make_hook_message("30001"))) is None)
    plugin._plugin_config_instance.block.intercept_enabled = True
    await plugin.on_unload()


def test_parse_review() -> None:
    """LLM 输出解析。"""
    from Mai_gatekeeper.plugin import MaiGateKeeper

    ok = MaiGateKeeper._parse_review('{"approve": true, "confidence": 0.9, "reason": "正常申请"}')
    check("纯净 JSON 解析", ok is not None and ok["approve"] is True and abs(ok["confidence"] - 0.9) < 1e-6)

    fenced = MaiGateKeeper._parse_review('```json\n{"approve": false, "confidence": 0.95, "reason": "广告"}\n```')
    check("围栏 JSON 解析", fenced is not None and fenced["approve"] is False)

    noisy = MaiGateKeeper._parse_review('好的，我的判断如下：\n{"approve": true, "confidence": 0.8, "reason": "朋友"}\n以上。')
    check("前后杂文解析", noisy is not None and noisy["approve"] is True)

    check("缺 approve 字段返回 None", MaiGateKeeper._parse_review('{"confidence": 0.9}') is None)
    check("纯文本返回 None", MaiGateKeeper._parse_review("我建议同意") is None)
    check("confidence 越界被夹取", MaiGateKeeper._parse_review('{"approve": true, "confidence": 1.7}')["confidence"] == 1.0)


async def _setup_approval(mode: str, llm: FakeLLM | None = None) -> tuple[Any, FakeCtx, FakeObClient]:
    plugin, ctx = make_plugin(overrides={"approve": {"mode": mode}})
    await plugin.on_load()
    client = FakeObClient()
    plugin._client = client
    if llm is not None:
        ctx.fixtures["llm"] = llm
    return plugin, ctx, client


async def test_friend_request_flows() -> None:
    """好友申请：manual / auto / hybrid / 屏蔽命中 / 去重 / flag 失效。"""
    # manual：推送管理员，pending
    plugin, ctx, client = await _setup_approval("manual")
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-1", "user_id": 40001, "comment": "我是群友"}
    )
    store = plugin._require_store()
    reqs = store.list_friend_requests("pending")
    check("manual：申请落库 pending", len(reqs) == 1 and reqs[0]["flag"] == "FLAG-1")
    check(
        "manual：管理员收到推送卡片（含命令提示）",
        any("FLAG-1" not in s["text"] and "/fr ok" in s["text"] and "40001" in s["text"] for s in ctx.send.sent),
    )
    check(
        "get_stranger_info 拉取昵称成功",
        client.actions[0]["action"] == "get_stranger_info" and reqs[0]["nickname"].startswith("昵称"),
    )

    # 重复 flag 去重
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-1", "user_id": 40001, "comment": "重复"}
    )
    check("重复 flag 不再落库", len(store.list_friend_requests("pending")) == 1)

    # 管理员 ok：action 正确
    manual_row = row_id_by_flag(store, "FLAG-1")
    reply = await plugin._handle_fr_command(f"/fr ok {manual_row}", "10001")
    check("管理员同意后状态 approved", "已同意" in reply and store.get_friend_request(manual_row)["status"] == "approved")
    check(
        "set_friend_add_request 参数正确",
        any(
            a["action"] == "set_friend_add_request" and a["params"]["flag"] == "FLAG-1" and a["params"]["approve"] is True
            for a in client.actions
        ),
    )

    # 审批结果推送：管理员命令路径也通知（推送到管理员会话，区别于命令回复流 s1）
    check(
        "/fr ok 后管理员会话收到结果推送",
        any("已同意" in s["text"] and s["stream_id"] == "stream-admin-10001" for s in ctx.send.sent),
    )

    # 非管理员命令被拒（管理员判定在 cmd_fr 层）
    result = await plugin.cmd_fr(text=f"/fr ok {manual_row}", stream_id="s1", user_id="99999")
    check("非管理员被拒绝", "仅管理员" in result[1])
    await plugin.on_unload()

    # auto：LLM 高置信直接同意
    plugin, ctx, client = await _setup_approval(
        "auto", FakeLLM('{"approve": true, "confidence": 0.92, "reason": "正常"}')
    )
    store = plugin._require_store()
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-2", "user_id": "40002", "comment": "你好"}
    )
    auto_row = row_id_by_flag(store, "FLAG-2")
    check("auto：直接 approved", store.get_friend_request(auto_row)["status"] == "approved")
    check("auto：推送结果摘要", any("已同意" in s["text"] for s in ctx.send.sent))
    # 关闭审批结果推送后不再推送
    plugin._plugin_config_instance.approve.notify_admin_on_decision = False
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-2B", "user_id": "40012", "comment": "hi"}
    )
    sent_before = len(ctx.send.sent)
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-2C", "user_id": "40013", "comment": "yo"}
    )
    check("关闭审批结果推送后不再推送", len(ctx.send.sent) == sent_before)
    plugin._plugin_config_instance.approve.notify_admin_on_decision = True
    await plugin.on_unload()

    # hybrid 低置信：转人工 pending
    plugin, ctx, client = await _setup_approval(
        "hybrid", FakeLLM('{"approve": true, "confidence": 0.4, "reason": "说不清"}')
    )
    store = plugin._require_store()
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-3", "user_id": "40003", "comment": "?"}
    )
    hybrid_row = row_id_by_flag(store, "FLAG-3")
    check("hybrid 低置信：保持 pending", store.get_friend_request(hybrid_row)["status"] == "pending")
    check("hybrid 低置信：推送含 LLM 建议的卡片", any("LLM 建议" in s["text"] and "0.40" in s["text"] for s in ctx.send.sent))
    await plugin.on_unload()

    # hybrid 高置信：直接执行
    plugin, ctx, client = await _setup_approval(
        "hybrid", FakeLLM('{"approve": false, "confidence": 0.88, "reason": "广告"}')
    )
    store = plugin._require_store()
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-4", "user_id": "40004", "comment": "加好友领福利"}
    )
    hybrid2_row = row_id_by_flag(store, "FLAG-4")
    check("hybrid 高置信：直接 rejected", store.get_friend_request(hybrid2_row)["status"] == "rejected")
    await plugin.on_unload()

    # 命中屏蔽名单：自动拒绝
    plugin, ctx, client = await _setup_approval("manual")
    store = plugin._require_store()
    store.add_blocked("qq", "40005", "global", "骚扰", "test")
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-5", "user_id": "40005", "comment": "再给我一次机会"}
    )
    blocked_row = row_id_by_flag(store, "FLAG-5")
    check("屏蔽用户申请被自动拒绝", store.get_friend_request(blocked_row)["status"] == "rejected")
    check("屏蔽用户申请拒绝有通知", any("自动拒绝" in s["text"] for s in ctx.send.sent))
    await plugin.on_unload()

    # flag 失效 → expired
    plugin, ctx, client = await _setup_approval("manual")
    store = plugin._require_store()
    client.fail_actions = {"set_friend_add_request"}
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "friend", "flag": "FLAG-6", "user_id": "40006", "comment": "hi"}
    )
    expired_row = row_id_by_flag(store, "FLAG-6")
    reply = await plugin._handle_fr_command(f"/fr ok {expired_row}", "10001")
    check("对端失败不能证明过期，持久 failed 禁重放", store.get_friend_request(expired_row)["status"] == "failed" and "不推断" in reply)
    await plugin.on_unload()

    # 非 friend 的 request（加群）与普通事件：忽略
    plugin, ctx, client = await _setup_approval("manual")
    store = plugin._require_store()
    await plugin._on_ob_event(
        {"post_type": "request", "request_type": "group", "flag": "G-1", "user_id": "40007", "sub_type": "add"}
    )
    await plugin._on_ob_event({"post_type": "message", "message_type": "group"})
    check("加群请求与普通事件被忽略", len(store.list_friend_requests("pending")) == 0)
    await plugin.on_unload()


async def test_tools() -> None:
    """planner 工具：block / unblock / remove_friend。"""
    plugin, ctx = make_plugin()
    await plugin.on_load()
    store = plugin._require_store()
    client = FakeObClient()
    plugin._client = client
    ctx.fixtures["message"] = FakeMessage(
        {
            "m-1": {
                "message_id": "m-1",
                "session_id": "stream-g-70001", "account_id": "90000", "platform": "qq",
                "message_info": {
                    "user_info": {"user_id": "50001", "user_nickname": "捣蛋鬼"},
                    "group_info": {"group_id": "70001"},
                }
            }
        }
    )

    # message_id 反查（带 stream_id，验证屏蔽提示）
    result = await plugin.tool_block_user(
        message_id="m-1", reason="刷屏", group_id="70001", user_id="60000", platform="qq", stream_id="stream-g-70001"
    )
    check("block_user(message_id) 成功", result["success"] is True and "捣蛋鬼" in result["content"])
    check(
        "屏蔽记录为群级",
        store.is_blocked("qq", "50001", "70001") and not store.is_blocked("qq", "50001", "70002")
        and not store.is_blocked("qq", "50001"),
    )
    check(
        "屏蔽后向当前会话发送提示",
        any("已被屏蔽" in s["text"] and "捣蛋鬼" in s["text"] and s["stream_id"] == "stream-g-70001" for s in ctx.send.sent),
    )

    # 重复屏蔽（不重复发提示）
    sent_before = len(ctx.send.sent)
    result = await plugin.tool_block_user(
        message_id="m-1", group_id="70001", user_id="60000", platform="qq", stream_id="stream-g-70001"
    )
    check("重复屏蔽返回失败提示", result["success"] is False)
    check("重复屏蔽不发送提示", len(ctx.send.sent) == sent_before)

    # 私聊调用被拒（block_user 仅群聊）
    result = await plugin.tool_block_user(user_id="50002", reason="私聊", group_id="", platform="qq")
    check("私聊调用 block_user 被拒", result["success"] is False and "仅限群聊" in result["content"])
    check("私聊被拒后不入库", not store.is_blocked("qq", "50002", ""))

    # 关闭提示开关后屏蔽成功但不发提示
    plugin._plugin_config_instance.block.notify_blocked_user = False
    result = await plugin.tool_block_user(
        user_id="50003", group_id="70001", platform="qq", stream_id="stream-g-70001"
    )
    check("关闭开关后屏蔽成功", result["success"] is True)
    check("关闭开关后不发送提示", len(ctx.send.sent) == sent_before)
    plugin._plugin_config_instance.block.notify_blocked_user = True

    # 保护名单
    result = await plugin.tool_block_user(user_id="20002", group_id="70001", platform="qq")
    check("保护名单不可屏蔽", result["success"] is False and "保护名单" in result["content"])
    result = await plugin.tool_block_user(user_id="10001", group_id="70001", platform="qq")
    check("管理员不可屏蔽", result["success"] is False)

    # 缺参数
    result = await plugin.tool_block_user(group_id="70001", platform="qq")
    check("缺定位参数返回提示", result["success"] is False)

    # unblock：私聊拒绝 + 群内解除
    result = await plugin.tool_unblock_user(user_id="50003", group_id="", platform="qq")
    check("私聊调用 unblock_user 被拒", result["success"] is False)
    result = await plugin.tool_unblock_user(user_id="50001", group_id="70001", platform="qq")
    check("unblock_user 解除群级屏蔽", result["success"] is True and not store.is_blocked("qq", "50001", "70001"))

    # remove_friend：默认直接删 + 自动全局屏蔽（user_id 经 kwargs 注入 = 当前对话对象）
    result = await plugin.tool_remove_friend(reason="骚扰", user_id="50002", platform="qq")
    check("remove_friend 成功", result["success"] is True)
    check(
        "delete_friend action 参数正确",
        any(a["action"] == "delete_friend" and a["params"]["user_id"] == 50002 for a in client.actions),
    )
    check("删除好友后自动全局屏蔽", store.is_blocked("qq", "50002", ""))

    # 无法确定当前对话对象时拒绝（fail-closed）
    result = await plugin.tool_remove_friend(reason="x", platform="qq")
    check("缺当前对话对象时拒绝删除", result["success"] is False and "拒绝" in result["content"])

    # 删好友转告管理员（默认开启；create_task 异步推送，等一拍）
    await asyncio.sleep(0.1)
    check(
        "删好友转告管理员",
        any("删除好友" in s["text"] and "50002" in s["text"] for s in ctx.send.sent),
    )
    # 关闭开关后不再转告
    plugin._plugin_config_instance.block.notify_admin_on_remove = False
    sent_before = len(ctx.send.sent)
    result = await plugin.tool_remove_friend(user_id="50021", platform="qq")
    await asyncio.sleep(0.1)
    check("关闭开关后删好友不再转告", result["success"] is True and len(ctx.send.sent) == sent_before)
    plugin._plugin_config_instance.block.notify_admin_on_remove = True

    # 开关关闭
    plugin._plugin_config_instance.block.allow_remove_friend = False
    result = await plugin.tool_remove_friend(user_id="50004", platform="qq")
    check("allow_remove_friend=False 时拒绝", result["success"] is False)

    # 保护名单
    plugin._plugin_config_instance.block.allow_remove_friend = True
    result = await plugin.tool_remove_friend(user_id="20002", platform="qq")
    check("保护名单不可删好友", result["success"] is False)

    # ---- 时长上限 ----
    # planner 传超长时长被钳制到上限（默认 24h）
    result = await plugin.tool_block_user(
        user_id="50012", duration_hours=999, group_id="70001", platform="qq", stream_id="stream-g-70001"
    )
    check("时长被钳制到上限", result["success"] is True and "24 小时" in result["content"])
    check("提示文本带时长", any("已被屏蔽（24 小时）" in s["text"] for s in ctx.send.sent))
    entries = {e["user_id"]: e for e in store.list_blocked()}
    check("限时屏蔽写入到期时间", entries.get("50012", {}).get("expires_at", "") != "")

    # 极短上限（0.0001h ≈ 0.36s）：到期自动失效并可重新屏蔽
    plugin._plugin_config_instance.block.max_duration_hours = 0.0001
    result = await plugin.tool_block_user(user_id="50013", group_id="70001", platform="qq")
    check("短时效屏蔽立即生效", result["success"] is True and store.is_blocked("qq", "50013", "70001"))
    await asyncio.sleep(0.5)
    check("到期后自动失效", not store.is_blocked("qq", "50013", "70001"))
    result = await plugin.tool_block_user(user_id="50013", group_id="70001", platform="qq")
    check("到期后可重新屏蔽", result["success"] is True)
    plugin._plugin_config_instance.block.max_duration_hours = 24.0

    # 上限为 0 = 永久屏蔽
    plugin._plugin_config_instance.block.max_duration_hours = 0
    result = await plugin.tool_block_user(user_id="50014", group_id="70001", platform="qq", stream_id="stream-g-70001")
    check("上限0时按永久屏蔽", result["success"] is True)
    entries = {e["user_id"]: e for e in store.list_blocked()}
    check("永久屏蔽无到期时间", entries.get("50014", {}).get("expires_at", "") == "")
    plugin._plugin_config_instance.block.max_duration_hours = 24.0

    # ---- 提示文案模板 ----
    # 永久屏蔽时 {duration} 为「永久」
    check(
        "永久屏蔽提示带「永久」",
        any("50014" in s["text"] and "永久" in s["text"] for s in ctx.send.sent),
    )

    # 自定义模板
    plugin._plugin_config_instance.block.blocked_tip_template = "再见 {user}，{duration} 内麦麦不理你"
    result = await plugin.tool_block_user(
        user_id="50015", duration_hours=2, group_id="70001", platform="qq", stream_id="stream-g-70001"
    )
    check(
        "自定义提示文案生效",
        result["success"] is True
        and any("再见 50015" in s["text"] and "2 小时" in s["text"] for s in ctx.send.sent),
    )

    # 模板留空回退默认文案
    plugin._plugin_config_instance.block.blocked_tip_template = ""
    result = await plugin.tool_block_user(user_id="50016", group_id="70001", platform="qq", stream_id="stream-g-70001")
    check(
        "留空回退默认文案",
        result["success"] is True and any("已被屏蔽" in s["text"] and "50016" in s["text"] for s in ctx.send.sent),
    )
    await plugin.on_unload()


async def test_commands() -> None:
    """/block 与 /fr 命令及管理员判定。"""
    plugin, ctx = make_plugin()
    await plugin.on_load()
    store = plugin._require_store()

    # 非管理员
    result = await plugin.cmd_block(text="/block list", stream_id="s1", user_id="99999", platform="qq")
    check("非管理员 /block 被拒且返回三元组", result[2] == 1 and "仅管理员" in result[1])
    check("拒绝信息已发送到会话", any("仅管理员" in s["text"] for s in ctx.send.sent))

    # 主程序级操作员通道：bot_config [plugin].permission 命中即放行（不在 admin_ids 中）
    ctx.config.values["plugin.permission"] = ["qq:88888"]
    result = await plugin.cmd_block(text="/block list", stream_id="s1", user_id="88888", platform="qq")
    check("主程序操作员（plugin.permission）放行", "屏蔽名单" in result[1] or "为空" in result[1])
    # 同号但平台不匹配 → 拒绝
    result = await plugin.cmd_block(text="/block list", stream_id="s1", user_id="88888", platform="telegram")
    check("操作员平台不匹配被拒", "仅管理员" in result[1])
    ctx.config.values["plugin.permission"] = []

    # is_local_operator 放行
    result = await plugin.cmd_block(
        text="/block add 50010 骚扰", stream_id="s1", user_id="99999", is_local_operator=True, group_id="70001", platform="qq"
    )
    check(
        "is_local_operator 可执行且群内默认群级",
        "已屏蔽" in result[1] and store.is_blocked("qq", "50010", "70001") and not store.is_blocked("qq", "50010"),
    )

    # 管理员 add global
    result = await plugin.cmd_block(
        text="/block add 50011 global 广告号", stream_id="s1", user_id="10001", platform="qq"
    )
    check("/block add global 生效", store.is_blocked("qq", "50011"))

    # list
    result = await plugin.cmd_block(text="/block list", stream_id="s1", user_id="10001", platform="qq")
    check("/block list 展示条目", "50010" in result[1] and "50011" in result[1])

    # un 按记录序号 / 按用户ID
    entries = store.list_blocked()
    target_id = next(e["id"] for e in entries if e["user_id"] == "50011")
    result = await plugin.cmd_block(text=f"/block un {target_id}", stream_id="s1", user_id="10001", platform="qq")
    check("/block un 序号 解除", "已删除" in result[1] and not store.is_blocked("qq", "50011"))
    result = await plugin.cmd_block(text="/block un 50010", stream_id="s1", user_id="10001", platform="qq")
    check("/block un 用户ID 解除", "已解除" in result[1] and not store.is_blocked("qq", "50010", "70001"))

    # /fr list 空列表
    result = await plugin.cmd_fr(text="/fr list", stream_id="s1", user_id="10001")
    check("/fr 空列表提示", "没有待处理" in result[1] and isinstance(result, tuple) and len(result) == 3)

    # 命令带参数解析
    result = await plugin.cmd_fr(text="/fr ok", stream_id="s1", user_id="10001")
    check("/fr ok 缺序号给用法", "用法" in result[1])
    await plugin.on_unload()


async def test_store_expiry() -> None:
    """store 层到期语义、清扫与旧库迁移。"""
    import sqlite3
    from datetime import datetime, timedelta

    from Mai_gatekeeper.store import GateKeeperStore

    tmp = Path(tempfile.mkdtemp(prefix="gk_store_"))
    store = GateKeeperStore(tmp / "t.db")
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")

    store.add_blocked("qq", "1", "global", expires_at=past)
    check("过期条目不生效", not store.is_blocked("qq", "1"))
    check("清扫删除过期条目", store.purge_expired() == 1 and not store.is_blocked_any_scope("qq", "1"))

    store.add_blocked("qq", "2", "group:9", expires_at=future)
    check("未过期条目生效", store.is_blocked("qq", "2", "9"))
    check("未过期条目在其他群不生效", not store.is_blocked("qq", "2", "8"))

    store.add_blocked("qq", "3", "global")
    check("无到期时间为永久", store.is_blocked("qq", "3"))
    store.close()

    # 旧库迁移：缺 expires_at 列的既有库自动补列
    old_db = tmp / "old.db"
    conn = sqlite3.connect(str(old_db))
    conn.execute(
        "CREATE TABLE blocked_users (platform TEXT, user_id TEXT, scope TEXT,"
        " reason TEXT DEFAULT '', blocked_by TEXT DEFAULT '', blocked_at TEXT,"
        " PRIMARY KEY (platform, user_id, scope))"
    )
    conn.commit()
    conn.close()
    old_store = GateKeeperStore(old_db)
    check("旧库自动补 expires_at 列", old_store.add_blocked("qq", "9", "global") and old_store.is_blocked("qq", "9"))
    old_store.close()


def test_components_scope() -> None:
    """chat_scope 顶层注入与工厂函数。"""
    from Mai_gatekeeper.plugin import MaiGateKeeper, create_plugin

    plugin = create_plugin()
    check("工厂函数返回 MaiGateKeeper 实例", isinstance(plugin, MaiGateKeeper))
    plugin._plugin_config_instance = None  # get_components 不应依赖配置
    components = plugin.get_components()
    by_name = {c.get("name"): c for c in components}
    check("组件包含三个工具与两个命令", {"block_user", "unblock_user", "remove_friend", "fr", "block"} <= set(by_name))
    check("remove_friend 为 private", by_name.get("remove_friend", {}).get("chat_scope") == "private")
    check("block_user 为 group", by_name.get("block_user", {}).get("chat_scope") == "group")
    check("unblock_user 为 group", by_name.get("unblock_user", {}).get("chat_scope") == "group")
    check("拦截 Hook 已注册", any(str(c.get("type") or "").upper() == "HOOK_HANDLER" for c in components))
    import inspect
    from Mai_gatekeeper.plugin import MaiGateKeeper as _GK
    sig = inspect.signature(_GK.tool_remove_friend)
    check("remove_friend 无 user_id 形参（不可指定目标）", "user_id" not in sig.parameters)


async def test_default_config_safety() -> None:
    """出厂默认配置的安全姿态：删好友工具默认关闭。"""
    from Mai_gatekeeper.config import GateKeeperConfig

    defaults = GateKeeperConfig()
    check("默认 allow_remove_friend=False", defaults.block.allow_remove_friend is False)
    check("默认拦截开启", defaults.block.intercept_enabled is True)
    check("默认审批 hybrid", defaults.approve.mode == "hybrid")
    check("默认屏蔽时长上限 24h", defaults.block.max_duration_hours == 24.0)
    check("默认管理员名单为空（fail-closed）", defaults.admin.admin_ids == [])
    check("默认审批结果通知开启", defaults.approve.notify_admin_on_decision is True)
    check("默认删好友通知开启", defaults.block.notify_admin_on_remove is True)

    # 默认配置下（未开启开关）删除被拒
    plugin, _ctx = make_plugin(overrides={"block": {"allow_remove_friend": False}})
    await plugin.on_load()
    client = FakeObClient()
    plugin._client = client
    result = await plugin.tool_remove_friend(reason="x", user_id="50090", platform="qq")
    check("默认关闭时删除被拒", result["success"] is False and "关闭" in result["content"])
    check("未产生 delete_friend action", not any(a["action"] == "delete_friend" for a in client.actions))
    await plugin.on_unload()


async def main() -> None:
    tests = [
        ("生命周期与手动配置", test_lifecycle_and_manual_config),
        ("拦截 Hook", test_hook_intercept),
        ("LLM 输出解析", test_parse_review),
        ("好友申请流", test_friend_request_flows),
        ("planner 工具", test_tools),
        ("管理命令", test_commands),
        ("store 到期与迁移", test_store_expiry),
        ("组件作用域与工厂", test_components_scope),
        ("出厂默认配置安全姿态", test_default_config_safety),
    ]
    for name, fn in tests:
        print(f"\n===== {name} =====")
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            check(f"{name}（未抛异常）", False, traceback.format_exc(limit=3))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n===== 结果：{passed}/{len(RESULTS)} 通过 =====")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"FAIL: {name}\n{detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
