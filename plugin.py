"""麦麦守门人（Mai_gatekeeper）插件入口。

功能：
- 好友申请审批：自建 OneBot v11 正向 WebSocket（ob_client）直连对端
  （NapCat / SnowLuma 通用），接收 ``post_type == "request"`` 好友申请事件。
  按配置的 approve.mode 分三路：LLM 全自动 / 推送管理员人工 / 混合
  （LLM 先审，低置信或可疑时转管理员）。审批动作经同一连接发
  ``set_friend_add_request``；命中屏蔽名单的申请人一律直接拒绝。
- 用户屏蔽：planner 工具 ``block_user`` / ``unblock_user``（仅群聊，
  屏蔽作用域为本群且受 ``block.max_duration_hours`` 时长上限约束——
  默认一天，到期自动解除；屏蔽成功后向当前会话发送提示告知对方）
  + ``remove_friend``（仅私聊删好友）。被屏蔽用户的
   发言在本插件的 ``chat.receive.before_process`` Hook 处 abort；
   不保证先于同槽第三方 Hook，不撤销已启动的任务或共享 Focus 历史。
- 管理命令：/fr（好友申请 list/ok/no）、/block（屏蔽名单 list/un/add）。
  管理员为 config [admin].admin_ids（空名单 fail-closed 全拒），
  主程序级操作员（is_local_operator）放行。

边界（不越权）：
- 不修改 snowluma-adapter，仅以第二客户端身份连接同一对端；
- 拦截仅丢弃被屏蔽用户的消息，不修改其他任何消息；
- manifest 只声明实际使用的能力，无越权能力。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .config import GateKeeperConfig
from .ob_client import OneBotClient, OneBotConnectionConfig
from .store import GLOBAL_SCOPE, GateKeeperStore, group_scope

LOGGER = logging.getLogger("MaiGateKeeper")

# 屏蔽提示默认文案（config.block.blocked_tip_template 留空时的回退值）
DEFAULT_BLOCKED_TIP = "⚠️ {user} 已被屏蔽（{duration}），期间麦麦将不再回应其发言。"

# LLM 审批员输出 JSON 的解析正则（容忍 ```json 围栏与前后杂文）
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_LLM_SYSTEM_PROMPT = (
    "你是QQ机器人麦麦的好友申请审查员。根据申请人的QQ号、昵称与验证消息，"
    "判断是否同意添加其为好友。\n"
    "判断标准：\n"
    "- 正常、礼貌、说明来意的申请 → 同意；\n"
    "- 广告、引流、赌博、色情、辱骂、可疑空白验证 → 拒绝；\n"
    "- 无法判断 → 给出低置信度（低于 0.7），交由人工处理。\n"
    '只输出 JSON，不要输出其他内容：{"approve": true或false, '
    '"confidence": 0到1的小数, "reason": "20字内的理由"}'
)


class MaiGateKeeper(MaiBotPlugin):
    """守门人插件：好友审批 + 用户屏蔽。"""

    config_model = GateKeeperConfig

    _SWEEP_INTERVAL_SEC = 300.0  # 到期屏蔽的周期清扫间隔

    _store: GateKeeperStore | None = None
    _client: OneBotClient | None = None
    _active_ob_config: OneBotConnectionConfig | None = None
    _block_sweep_task: asyncio.Task[None] | None = None

    def __init__(self) -> None:
        super().__init__()
        self._lifecycle_lock = asyncio.Lock()
        self._generation = 0
        self._closing = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self._closing = False
        if not self.config.plugin.enabled:
            LOGGER.info("守门人插件已在配置中停用，跳过初始化")
            return

        data_dir = self._resolve_data_dir()
        self._store = GateKeeperStore(data_dir / "gatekeeper.db")
        LOGGER.info(f"守门人数据目录: {data_dir}")

        # 清理加载前遗留的到期屏蔽（含旧库迁移后的数据）
        expired = self._store.purge_expired()
        if expired:
            LOGGER.info(f"守门人启动时清理了 {expired} 条到期屏蔽")

        self._block_sweep_task = asyncio.create_task(
            self._block_sweep_loop(), name="gatekeeper.block_sweep"
        )
        await self._start_ob_client()

    async def _block_sweep_loop(self) -> None:
        """周期清理到期的群聊屏蔽（拦截判定本身已按时间失效，此处仅做数据清理）。"""
        while True:
            await asyncio.sleep(self._SWEEP_INTERVAL_SEC)
            try:
                if self._store is not None:
                    removed = self._store.purge_expired()
                    if removed:
                        LOGGER.info(f"守门人自动解除 {removed} 条到期屏蔽")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.debug("守门人屏蔽到期清理失败 (%s)", type(exc).__name__)

    async def on_unload(self) -> None:
        self._generation += 1
        self._closing = True
        async with self._lifecycle_lock:
            await self._unload_locked()

    async def _unload_locked(self) -> None:
        if self._block_sweep_task is not None:
            self._block_sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._block_sweep_task
            self._block_sweep_task = None
        if self._client is not None:
            await self._client.stop()
            self._client = None
        self._active_ob_config = None
        if self._store is not None:
            self._store.close()
            self._store = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return
        self._generation += 1
        async with self._lifecycle_lock:
            await self._config_update_locked()

    async def _config_update_locked(self) -> None:
        if not self.config.plugin.enabled:
            if self._block_sweep_task is not None:
                self._block_sweep_task.cancel()
                self._block_sweep_task = None
            if self._client is not None:
                await self._client.stop()
                self._client = None
            self._active_ob_config = None
            LOGGER.info("守门人插件已被停用")
            return
        if self._store is None:
            await self.on_load()
            return
        await self._start_ob_client(restart_if_changed=True)

    def _resolve_data_dir(self) -> Path:
        """持久化目录：优先 ctx.paths.data_dir（SDK 2.6.0+）。"""
        paths = getattr(self.ctx, "paths", None)
        data_dir = getattr(paths, "data_dir", None) if paths is not None else None
        if data_dir:
            return Path(str(data_dir))
        return Path(__file__).parent / "data"

    async def _start_ob_client(self, restart_if_changed: bool = False) -> None:
        """解析连接配置并启动（或按需重启）OneBot 客户端。

        配置不完整（[ob] 的 server/port 未填写）时不启动连接，仅告警一次；
        用户补全配置后会触发 on_config_update，届时自动重试。
        """
        new_config = await self._resolve_ob_config()
        if new_config is None:
            if self._client is not None:
                await self._client.stop()
                self._client = None
            self._active_ob_config = None
            LOGGER.warning(
                "守门人 OneBot 连接配置不完整：请在插件配置的 [ob] 中填写对端地址（server）"
                "与端口（port）。填写保存后将自动连接。"
            )
            return

        if self._client is not None:
            if restart_if_changed and self._active_ob_config != new_config:
                self._client.configure(new_config)
                self._active_ob_config = new_config
                await self._client.restart()
                LOGGER.info("守门人 OneBot 连接配置已变更，已重启连接")
            return

        self._client = OneBotClient(on_event=self._on_ob_event, logger=LOGGER)
        self._client.configure(new_config)
        self._active_ob_config = new_config
        await self._client.start()

    async def _resolve_ob_config(self) -> OneBotConnectionConfig | None:
        """从插件配置解析 OneBot 连接参数（未填写完整时返回 None）。"""
        ob = self.config.ob
        server = ob.server.strip()
        port = int(ob.port or 0)
        token = ob.token.strip()
        if not server or not port:
            return None
        return OneBotConnectionConfig(
            server=server,
            port=port,
            token=token,
            heartbeat_interval=ob.heartbeat_interval,
            reconnect_delay_sec=ob.reconnect_delay_sec,
            action_timeout_sec=ob.action_timeout_sec,
        )

    # ------------------------------------------------------------------
    # OneBot 事件 → 好友申请审批流
    # ------------------------------------------------------------------

    async def _on_ob_event(self, payload: dict[str, Any]) -> None:
        """处理 OneBot 事件（当前只关心好友申请）。"""
        if not self._tool_ready():
            return
        if str(payload.get("post_type") or "") != "request":
            return
        if str(payload.get("request_type") or "") != "friend":
            return
        flag = str(payload.get("flag") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        comment = str(payload.get("comment") or "").strip()[:2000]
        account = str(getattr(self._client, "self_id", "") or "")
        if (not flag or len(flag) > 1024 or len(user_id) > 64
                or not account or str(payload.get("self_id") or "") != account):
            return

        store = self._require_store()
        if not user_id.isascii() or not user_id.isdigit() or int(user_id) == 0:
            store.observe_friend_request(flag, account, "", "unresolved_user")
            LOGGER.info("守门人：申请人身份未确认，请查看 /fr review 并在QQ客户端处理")
            return
        if store.get_friend_request_by_flag(flag) is not None or store.has_friend_observation(flag, account):
            store.observe_friend_request(flag, account, user_id, "repeated_flag")
            LOGGER.info("守门人：重复申请标识，请查看 /fr review 并在QQ客户端处理")
            return
        # Reserve before nickname/LLM awaits so a repeated event fences this work.
        row_id = store.add_friend_request(flag, user_id, "", comment, account)
        if row_id is None:
            return
        owner = self._client
        generation = self._generation
        nickname = await self._fetch_nickname(user_id)
        if (owner is not self._client or generation != self._generation or not self._tool_ready()
                or str(getattr(self._client, "self_id", "") or "") != account):
            return
        if not store.set_friend_request_nickname(row_id, nickname):
            return
        LOGGER.info("守门人收到好友申请 #%s", row_id)
        await self._dispatch_friend_request(row_id, user_id, nickname, comment)

    async def _dispatch_friend_request(
        self, row_id: int, user_id: str, nickname: str, comment: str
    ) -> None:
        """按审批模式分发好友申请。"""
        approve_cfg = self.config.approve
        generation = self._generation
        owner = self._client
        store = self._require_store()
        platform = self.config.admin.platform

        if store.is_blocked_any_scope(platform, user_id):
            # 命中屏蔽名单：任何模式都直接拒绝（结果推送由 _execute_decision 统一发出）
            await self._execute_decision(row_id, False, "auto:blocked", "命中屏蔽名单，自动拒绝",
                                         generation=generation, owner=owner)
            return

        if approve_cfg.mode == "manual":
            await self._push_request_card(row_id, user_id, nickname, comment, llm_summary=None)
            return

        review = await self._llm_review(user_id, nickname, comment)
        if generation != self._generation or owner is not self._client or not self._tool_ready():
            return  # retained pending; do not execute stale policy or notify a new account
        approve_cfg = self.config.approve
        if review is None:
            note = "（LLM 审批失败，转人工）" if approve_cfg.mode == "hybrid" else "（LLM 审批失败）"
            await self._push_request_card(row_id, user_id, nickname, comment, llm_summary=note)
            return

        approve = bool(review["approve"])
        confidence = float(review["confidence"])
        reason = str(review["reason"])
        direct_execute = approve_cfg.mode == "auto" or (
            approve_cfg.mode == "hybrid" and confidence >= approve_cfg.confidence_threshold
        )

        if direct_execute:
            decided_by = f"llm:auto(conf={confidence:.2f})"
            await self._execute_decision(row_id, approve, decided_by, reason,
                                         generation=generation, owner=owner, confidence=confidence)
        else:
            llm_summary = (
                f"LLM 建议{'同意' if approve else '拒绝'}（置信 {confidence:.2f}，低于阈值，"
                f"待人工确认）。理由：{reason}"
            )
            await self._push_request_card(row_id, user_id, nickname, comment, llm_summary=llm_summary)

    async def _llm_review(self, user_id: str, nickname: str, comment: str) -> dict[str, Any] | None:
        """调用 LLM 审查好友申请；失败返回 None（调用方负责降级转人工）。"""
        user_content = (
            f"申请人QQ：{user_id[:64]}\n昵称：{nickname[:100] or '（未知）'}\n验证消息：{comment[:2000] or '（空）'}"
        )
        extra_rules = self.config.approve.extra_rules.strip()
        if extra_rules:
            user_content += f"\n补充规则：{extra_rules[:4000]}"

        try:
            result = await asyncio.wait_for(self.ctx.llm.generate(
                prompt=[
                    {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                task_name="utils",
            ), timeout=60)
        except Exception as exc:
            LOGGER.warning("守门人 LLM 审批调用失败 (%s)", type(exc).__name__)
            return None
        if not isinstance(result, dict) or not result.get("success"):
            LOGGER.warning("守门人 LLM 审批未成功")
            return None
        return self._parse_review(str(result.get("response") or ""))

    @staticmethod
    def _parse_review(text: str) -> dict[str, Any] | None:
        """从 LLM 回复中解析审批 JSON；解析失败返回 None。"""
        if len(text) > 8192:
            return None
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("approve"), bool):
            return None
        try:
            confidence = float(data.get("confidence", 0.0))
            if not math.isfinite(confidence):
                return None
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(data.get("reason") or "").strip()[:80]
        return {"approve": data["approve"], "confidence": confidence, "reason": reason}

    async def _execute_decision(
        self, row_id: int, approve: bool, decided_by: str, reason: str,
        *, generation: int | None = None, owner: Any = None, confidence: float = 0,
    ) -> str:
        """Persist intent before dispatch; any uncertain result is terminal unknown."""
        async with self._lifecycle_lock:
            if not self._tool_ready():
                return "offline"
            if generation is not None:
                if generation != self._generation or owner is not self._client:
                    return "policy_changed"
                cfg = self.config.approve
                if decided_by.startswith("llm:") and not (
                    cfg.mode == "auto" or cfg.mode == "hybrid" and confidence >= cfg.confidence_threshold
                ):
                    return "policy_changed"
            status, request = await self._decision_locked(row_id, approve, decided_by, reason)
        # Notification is not part of the action transaction; never retry an action for it.
        if status in ("approved", "rejected") and self.config.approve.notify_admin_on_decision:
            try:
                await self._push_admins(
                    f"【守门人】好友申请 #{row_id} {request['user_id']} 已{'同意' if approve else '拒绝'}"
                    f"（{decided_by}）：{reason[:80]}", account_id=request["account_id"]
                )
            except Exception:
                LOGGER.warning("守门人审批结果已落库，通知失败；不会重放动作")
        return status

    async def _decision_locked(self, row_id: int, approve: bool, decided_by: str, reason: str) -> tuple[str, Any]:
        store = self._require_store()
        request = store.get_friend_request(row_id)
        if request is None or request["status"] != "pending":
            if request and request["status"] == "review":
                return "review", request
            return "not_pending", request

        client = self._client
        if client is None or not client.connected:
            LOGGER.warning(f"守门人处理申请 #{row_id} 时 OneBot 对端未连接，保持 pending")
            return "offline", request
        account = str(getattr(client, "self_id", "") or "")
        if not account or request["account_id"] != account:
            return "unscoped", request
        if not store.claim_request(row_id, account, decided_by, reason[:80]):
            return "not_pending", request

        try:
            response = await client.call_action(
                "set_friend_add_request", {"flag": request["flag"], "approve": bool(approve)}
            )
        except BaseException as exc:
            store.finish_request(row_id, "unknown")
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            LOGGER.warning("守门人审批 #%s 结果未知 (%s)，禁止重放", row_id, type(exc).__name__)
            return "unknown", request
        status = ("approved" if approve else "rejected") if self._action_ok(response) else (
            "failed" if self._action_failed(response) else "unknown"
        )
        store.finish_request(row_id, status)
        LOGGER.info("守门人审批 #%s: %s", row_id, status)
        return status, request

    @staticmethod
    def _action_ok(response: Any) -> bool:
        return (isinstance(response, dict) and response.get("status") == "ok"
                and type(response.get("retcode")) is int and response["retcode"] == 0)

    @staticmethod
    def _action_failed(response: Any) -> bool:
        return (isinstance(response, dict) and response.get("status") == "failed"
                and type(response.get("retcode")) is int and response["retcode"] != 0)

    async def _push_request_card(
        self,
        row_id: int,
        user_id: str,
        nickname: str,
        comment: str,
        llm_summary: str | None,
    ) -> None:
        """向管理员推送好友申请卡片。"""
        lines = [f"【好友申请 #{row_id}】", f"QQ：{user_id}（{nickname or '未知昵称'}）"]
        if comment:
            lines.append(f"验证消息：{comment}")
        if llm_summary:
            lines.append(llm_summary)
        lines.append(f"回复 /fr ok {row_id} 同意 · /fr no {row_id} 拒绝")
        request = self._store.get_friend_request(row_id) if self._store else None
        if request and request["account_id"]:
            await self._push_admins("\n".join(lines), account_id=request["account_id"])

    async def _push_admins(self, text: str, *, account_id: str = "") -> None:
        """把文本推送到所有管理员的私聊会话。"""
        owner = self._client
        account = str(getattr(owner, "self_id", "") or "")
        generation = self._generation
        if not account or account_id and account_id != account:
            return
        admin_ids = [str(a).strip() for a in self.config.admin.admin_ids if str(a).strip()]
        if not admin_ids:
            LOGGER.info("守门人无管理员可推送")
            return
        for admin_id in admin_ids:
            try:
                stream_id = await self._resolve_admin_stream(admin_id)
                if (owner is not self._client or generation != self._generation
                        or str(getattr(owner, "self_id", "") or "") != account or not self._tool_ready()):
                    return
                if not stream_id:
                    LOGGER.warning("守门人无法定位管理员私聊会话")
                    continue
                await self.ctx.send.text(text, stream_id)
            except Exception as exc:
                LOGGER.warning("守门人推送管理员失败 (%s)", type(exc).__name__)

    async def _resolve_admin_stream(self, admin_id: str) -> str | None:
        """定位（必要时创建）管理员私聊会话，返回 stream_id。"""
        platform = self.config.admin.platform
        client = self._client
        account = str(getattr(client, "self_id", "") or "")
        if not account:
            return None

        def current() -> bool:
            return client is self._client and str(getattr(client, "self_id", "") or "") == account

        def match(stream: Any) -> str | None:
            if (isinstance(stream, dict) and str(stream.get("account_id") or "") == account
                    and stream.get("platform") == platform and str(stream.get("user_id") or "") == admin_id
                    and not stream.get("group_id") and stream.get("chat_type") == "private"):
                return str(stream.get("stream_id") or stream.get("session_id") or "") or None
            return None

        try:
            result = await self.ctx.chat.get_stream_by_user_id(admin_id, platform=platform)
            stream_id = match(result)  # SDK2.20 already unwraps chat lookup results.
            if stream_id and current():
                return stream_id
        except Exception as exc:
            LOGGER.warning("守门人查询管理员会话失败 (%s)", type(exc).__name__)

        try:
            if not current():
                return None
            result = await self.ctx.chat.open_session(
                platform=platform, chat_type="private", user_id=admin_id, account_id=account
            )
            if isinstance(result, dict) and result.get("success"):
                stream_id = match(result.get("stream") or result)
                if stream_id and current():
                    return stream_id
        except Exception as exc:
            LOGGER.warning("守门人创建管理员会话失败 (%s)", type(exc).__name__)
        return None

    async def _fetch_nickname(self, user_id: str) -> str:
        """经 OneBot get_stranger_info 拉取申请人昵称（失败返回空）。"""
        client = self._client
        if client is None or not client.connected:
            return ""
        try:
            response = await client.call_action(
                "get_stranger_info", {"user_id": self._to_int_or_str(user_id)}
            )
            data = response.get("data") if isinstance(response, dict) else None
            if isinstance(data, dict):
                return str(data.get("nickname") or "")[:100]
        except Exception as exc:
            LOGGER.debug("守门人获取昵称失败 (%s)", type(exc).__name__)
        return ""

    @staticmethod
    def _to_int_or_str(value: str) -> int | str:
        """QQ 号统一转 int（OneBot 动作参数惯例），非数字保持字符串。"""
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            return text

    def _require_store(self) -> GateKeeperStore:
        if self._store is None:
            raise RuntimeError("守门人存储未初始化（插件未加载或已停用）")
        return self._store

    # ------------------------------------------------------------------
    # 拦截 Hook
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.before_process",
        name="gatekeeper_intercept",
        description="本 Hook 运行时拦截被屏蔽用户发言；不撤销先前第三方 Hook 的副作用",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def intercept_blocked_message(self, **kwargs: Any) -> dict[str, Any] | None:
        """入站消息预处理前检查发送者是否被屏蔽。"""
        if not self.config.plugin.enabled or not self.config.block.intercept_enabled:
            return None
        store = self._store
        if store is None:
            return None

        message = kwargs.get("message")
        if not isinstance(message, dict):
            return None
        info = message.get("message_info") or {}
        user_info = info.get("user_info") or {}
        user_id = str(user_info.get("user_id") or "").strip()
        if not user_id:
            return None

        platform = str(message.get("platform") or self.config.admin.platform)
        group_info = info.get("group_info") or {}
        group_id = str((group_info or {}).get("group_id") or "").strip()

        if store.is_blocked(platform, user_id, group_id):
            LOGGER.debug("守门人拦截被屏蔽用户发言")
            return {"action": "abort"}
        return None

    # ------------------------------------------------------------------
    # planner 工具
    # ------------------------------------------------------------------

    @Tool(
        name="block_user",
        platforms="qq",
        trusted_context="neko.tools.context.v1",
        description=(
            "屏蔽当前QQ群的目标用户，让机器人忽略其本群发言。仅用于持续骚扰、刷屏或恶意行为，不用于正常用户。"
            "默认最长24小时，到期自动解除；轻微冒犯可缩短时长。管理员与保护名单用户不可屏蔽。"
        ),
        brief_description="屏蔽当前QQ群的目标用户，让机器人忽略其本群发言。仅用于持续骚扰、刷屏或恶意行为，不用于正常用户。默认最长24小时，到期自动解除；轻微冒犯可缩短时长。管理员与保护名单用户不可屏蔽。",
        detailed_description=(
            "屏蔽后该用户的新消息在本插件入站 Hook 处被丢弃，并向群里发送一条提示；不保证先于其他插件。"
            "优先通过目标用户在当前目标群的真实 message_id 精确定位发送者；共享Focus缺账号锚点时必须提供。"
            "仅身份完整且用户ID已知时可直接给 user_id，缺证据不要猜。duration_hours 可指定更短时长，最大不超过配置的上限"
            "（默认 24 小时），到期自动解除。管理员与保护名单用户不可被屏蔽。"
        ),
        parameters=[
            ToolParameterInfo(
                name="message_id",
                param_type=ToolParamType.STRING,
                description="目标用户在当前目标群的真实消息ID；优先使用，共享Focus缺账号锚点时必需，不猜编号",
                required=False,
            ),
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="已知的目标用户ID；仅可信身份完整时可单独使用，不能替代缺失的消息/账号证据",
                required=False,
            ),
            ToolParameterInfo(
                name="duration_hours",
                param_type=ToolParamType.NUMBER,
                description="屏蔽时长（小时），可留空使用配置的上限；不会超过配置的时长上限",
                required=False,
            ),
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="屏蔽原因（简短，例如：刷屏骚扰）",
                required=False,
            ),
        ],
    )
    async def tool_block_user(
        self,
        message_id: str = "",
        user_id: str = "",
        duration_hours: float = 0,
        reason: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """planner 屏蔽用户工具（仅群聊，受配置的时长上限约束）。"""
        if not self._tool_ready():
            return {"success": False, "content": "守门人插件未就绪"}
        identity = await self._resolve_tool_identity(kwargs, message_id=message_id)
        if identity is None:
            return {"success": False, "content": "缺少可信会话或账号身份，拒绝执行"}
        kwargs = identity
        generation = self._generation
        group_id = str(kwargs.get("group_id") or "").strip()
        if not group_id:
            # 双保险：chat_scope 应已限制工具只在群聊出现
            return {"success": False, "content": "该工具仅限群聊使用（私聊请使用 remove_friend）"}
        store = self._require_store()

        target, nickname = await self._resolve_target(message_id, user_id, identity)
        if not target:
            return {"success": False, "content": "需要 message_id 或 user_id 之一来定位要屏蔽的用户"}

        platform = str(kwargs.get("platform") or self.config.admin.platform)

        if await self._is_protected(target, identity):
            return {"success": False, "content": f"用户 {target} 在保护名单中，不可屏蔽"}
        if generation != self._generation or not self._tool_ready():
            return {"success": False, "content": "策略已变化，拒绝执行"}

        # 时长上限：>0 时屏蔽带到期时间；planner 指定的时长被钳制到上限内
        cap_hours = float(self.config.block.max_duration_hours or 0)
        if not math.isfinite(cap_hours) or cap_hours < 0:
            return {"success": False, "content": "屏蔽时长配置无效，拒绝执行"}
        expires_at = ""
        duration_text = ""
        if cap_hours > 0:
            try:
                hours = float(duration_hours or 0)
            except (TypeError, ValueError):
                hours = 0
            if not math.isfinite(hours) or hours <= 0 or hours > cap_hours:
                hours = cap_hours
            # 保留微秒精度：截断到秒会让短时长屏蔽立即过期
            expires_at = (datetime.now() + timedelta(hours=hours)).isoformat()
            duration_text = f"{hours:g} 小时"

        scope = group_scope(group_id)
        caller = str(kwargs.get("user_id") or "")
        added = store.add_blocked(
            platform, target, scope, reason.strip(), blocked_by=f"planner:{caller}", expires_at=expires_at
        )
        if not added:
            return {"success": False, "content": f"用户 {nickname or target} 在本群（{group_id}）已被屏蔽"}

        LOGGER.info("守门人已添加群级屏蔽")

        # 向当前会话发送提示，告知对方已被屏蔽（文案模板可在配置中自定义）
        if self.config.block.notify_blocked_user:
            stream_id = str(kwargs.get("stream_id") or "")
            if stream_id:
                template = (self.config.block.blocked_tip_template or "").strip() or DEFAULT_BLOCKED_TIP
                tip = (
                    template.replace("{user}", str(nickname or target))
                    .replace("{duration}", duration_text or "永久")
                )
                try:
                    await self.ctx.send.text(tip, stream_id)
                except Exception as exc:
                    LOGGER.warning("守门人发送屏蔽提示失败 (%s)", type(exc).__name__)

        if self.config.block.notify_admin_on_block:
            await self._notify_admins(
                    f"【守门人】planner 已屏蔽用户 {target}（{nickname or '未知昵称'}），"
                    f"范围：本群（{group_id}），时长：{duration_text or '永久'}，"
                    f"原因：{reason or '未填写'}。可用 /block un 解除。", account_id=identity["account_id"]
            )
        duration_note = f"，时长 {duration_text}" if duration_text else ""
        return {
            "success": True,
            "content": f"已屏蔽用户 {nickname or target}（本群{duration_note}），其发言将被忽略",
        }

    @Tool(
        name="unblock_user",
        platforms="qq",
        trusted_context="neko.tools.context.v1",
        description="解除目标用户在当前QQ群的屏蔽，撤销block_user在本群的效果。仅群聊可用。",
        brief_description="解除目标用户在当前QQ群的屏蔽，撤销block_user在本群的效果。仅群聊可用。",
        detailed_description=(
            "解除 block_user 做出的本群屏蔽，该用户的发言恢复可见。"
            "优先通过目标用户在当前目标群的真实 message_id 定位；共享Focus缺账号锚点时必须提供。仅身份完整且用户ID已知时可用 user_id，缺证据不要猜。"
        ),
        parameters=[
            ToolParameterInfo(
                name="message_id",
                param_type=ToolParamType.STRING,
                description="目标用户在当前目标群的真实消息ID；优先使用，共享Focus缺账号锚点时必需，不猜编号",
                required=False,
            ),
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="已知的目标用户ID；仅可信身份完整时可单独使用，不能替代缺失的消息/账号证据",
                required=False,
            ),
        ],
    )
    async def tool_unblock_user(
        self, message_id: str = "", user_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        """planner 解除屏蔽工具（仅群聊）。"""
        if not self._tool_ready():
            return {"success": False, "content": "守门人插件未就绪"}
        identity = await self._resolve_tool_identity(kwargs, message_id=message_id)
        if identity is None:
            return {"success": False, "content": "缺少可信会话或账号身份，拒绝执行"}
        kwargs = identity
        generation = self._generation
        group_id = str(kwargs.get("group_id") or "").strip()
        if not group_id:
            return {"success": False, "content": "该工具仅限群聊使用"}
        store = self._require_store()

        target, _nickname = await self._resolve_target(message_id, user_id, identity)
        if not target:
            return {"success": False, "content": "需要 message_id 或 user_id 之一来定位用户"}
        if generation != self._generation or not self._tool_ready():
            return {"success": False, "content": "策略已变化，拒绝执行"}

        platform = str(kwargs.get("platform") or self.config.admin.platform)
        group_id = str(kwargs.get("group_id") or "").strip()
        scope = group_scope(group_id) if group_id else ""
        removed = store.remove_blocked_by_user(platform, target, scope)
        if not removed:
            return {"success": False, "content": f"用户 {target} 在当前范围没有被屏蔽"}
        return {"success": True, "content": f"已解除用户 {target} 的屏蔽（{removed} 条记录）"}

    @Tool(
        name="remove_friend",
        platforms="qq",
        trusted_context="neko.tools.context.v1",
        description=(
            "删除当前QQ私聊对象好友，并自动全局屏蔽以防再次申请；不能指定他人。"
            "仅用于持续骚扰且屏蔽不足的情况，操作不可逆，谨慎使用。"
            "须配置开启，保护名单用户不可删除；缺可信会话或账号证据不执行，不猜身份。"
        ),
        brief_description="删除当前QQ私聊对象好友，并自动全局屏蔽以防再次申请；不能指定他人。仅用于持续骚扰且屏蔽不足的情况，操作不可逆，谨慎使用。须配置开启，保护名单用户不可删除；缺可信会话或账号证据不执行，不猜身份。",
        detailed_description=(
            "只能删除正在与机器人私聊的对方，工具不接受指定其他用户。"
            "删除好友后会自动全局屏蔽对方，防止其再次申请好友。"
            "保护名单用户不可删除。"
        ),
        parameters=[
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="删除原因（简短，会记录日志并通知管理员）",
                required=False,
            ),
        ],
    )
    async def tool_remove_friend(self, reason: str = "", **kwargs: Any) -> dict[str, Any]:
        """私聊删好友工具（目标固定为当前对话对象）。

        目标只能取自 Host opt-in 信封，不读取模型可覆盖的普通 kwargs。
        """
        if not self._tool_ready():
            return {"success": False, "content": "守门人插件未就绪"}
        if not self.config.block.allow_remove_friend:
            return {"success": False, "content": "删好友功能已在配置中关闭"}
        identity = await self._resolve_tool_identity(kwargs)
        if identity is None or identity["group_id"]:
            return {"success": False, "content": "无法确定当前对话对象，拒绝执行删除"}
        peer = identity["user_id"]
        generation = self._generation
        if await self._is_protected(peer, identity):
            return {"success": False, "content": f"用户 {peer} 在保护名单中，不可删除好友"}
        async with self._lifecycle_lock:
            client = self._client
            if (not self._tool_ready() or not self.config.block.allow_remove_friend
                    or generation != self._generation or client is None or not client.connected
                    or str(getattr(client, "self_id", "") or "") != identity["account_id"]):
                return {"success": False, "content": "连接、账号或策略已变化，拒绝执行"}
            store = self._require_store()
            platform = identity["platform"]
            key = json.dumps([platform, identity["account_id"], peer], separators=(",", ":"))
            if not store.claim_action("delete_friend", key):
                return {"success": False, "content": "该删除操作已有持久记录，禁止自动重放；需人工核验"}
            try:
                response = await client.call_action("delete_friend", {"user_id": self._to_int_or_str(peer)})
            except BaseException as exc:
                store.finish_action("delete_friend", key, "unknown")
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return {"success": False, "content": "删除结果未知，禁止重试；需人工核验"}
            status = "succeeded" if self._action_ok(response) else (
                "failed" if self._action_failed(response) else "unknown")
            store.finish_action("delete_friend", key, status)
            if status != "succeeded":
                return {"success": False, "content": f"删除结果 {status}，禁止自动重放；需人工核验"}
            store.add_blocked(platform, peer, GLOBAL_SCOPE, f"删除好友自动屏蔽：{reason.strip()[:200]}", "auto")
        LOGGER.info("守门人删除好友动作成功，已全局屏蔽")

        # 转告管理员（删好友不可逆）
        if self.config.block.notify_admin_on_remove:
            await self._notify_admins(
                    f"【守门人】planner 已删除好友 {peer}，原因：{reason or '未填写'}，"
                    "并已自动全局屏蔽（对方再次申请好友将被自动拒绝）。", account_id=identity["account_id"]
            )
        return {"success": True, "content": f"已删除好友 {peer} 并全局屏蔽其消息"}

    async def _notify_admins(self, text: str, *, account_id: str = "") -> None:
        try:
            await self._push_admins(text, account_id=account_id)
        except Exception:
            LOGGER.warning("守门人动作已落库，通知失败；不会重放动作")

    def _tool_ready(self) -> bool:
        """插件总开关与存储就绪检查。"""
        return bool(self.config.plugin.enabled) and self._store is not None and not self._closing

    async def _is_protected(self, user_id: str, identity: dict[str, str]) -> bool:
        """管理员与保护名单不可被屏蔽/删除。"""
        admins = {str(a).strip() for a in self.config.admin.admin_ids}
        protected = {str(p).strip() for p in self.config.block.protect_user_ids}
        if not user_id or not identity.get("account_id") or not identity.get("platform"):
            return True
        if user_id in admins or user_id in protected or user_id == identity["account_id"]:
            return True
        try:
            permissions = await self.ctx.config.get("plugin.permission", None)
        except Exception:
            return True
        if not isinstance(permissions, list):
            return True
        scoped = f"{identity['platform'].lower()}:{user_id}"
        for permission in permissions:
            parts = str(permission).split(":", 1)
            if len(parts) != 2 or not all(p.strip() for p in parts):
                return True
            if f"{parts[0].strip().lower()}:{parts[1].strip()}" == scoped:
                return True
        return False

    @staticmethod
    def _trusted_identity(kwargs: dict[str, Any]) -> dict[str, str] | None:
        # Host replaces this envelope after model arguments, only for opted-in tools.
        context = kwargs.get("_host_context")
        if not isinstance(context, dict) or context.get("contract") != "neko.tools.context.v1":
            return None
        identity = {k: str(context.get(k) or "").strip() for k in (
            "session_id", "stream_id", "platform", "group_id", "user_id", "account_id")}
        if (not all(identity[k] for k in ("session_id", "platform", "user_id", "account_id"))
                or identity["stream_id"] != identity["session_id"] or identity["platform"].lower() != "qq"):
            return None
        return identity

    async def _resolve_tool_identity(self, kwargs: dict[str, Any], *, message_id: str = "") -> dict[str, str] | None:
        """Resolve anchorless Focus using an existing scoped message/stream, never model identity.

        Host's opt-in envelope is authoritative for route, but its account/caller
        may be absent in Focus. A connected, verified OneBot account alone is
        insufficient: the actual stored message/private stream must match it.
        """
        direct = self._trusted_identity(kwargs)
        if direct is not None:
            return direct
        context = kwargs.get('_host_context')
        if not isinstance(context, dict) or context.get('contract') != 'neko.tools.context.v1':
            return None
        identity = {k: str(context.get(k) or '').strip() for k in (
            'session_id', 'stream_id', 'platform', 'group_id', 'user_id', 'account_id')}
        if (not identity['session_id'] or identity['stream_id'] != identity['session_id']
                or identity['platform'] != 'qq' or identity['account_id']):
            return None  # A nonempty but incomplete/conflicting envelope is not repaired.
        client = self._client
        account = str(getattr(client, 'self_id', '') or '')
        if client is None or not client.connected or not account:
            return None
        if identity['group_id']:
            if not message_id:
                return None  # Direct arbitrary user ID cannot prove an anchorless group's account.
            try:
                source = await self.ctx.message.get_by_id(str(message_id), chat_id=identity['session_id'])
            except Exception:
                return None
            group = (source.get('message_info') or {}).get('group_info') or {} if isinstance(source, dict) else {}
            if (not isinstance(source, dict) or source.get('message_id') != str(message_id)
                    or source.get('session_id') != identity['session_id'] or source.get('platform') != 'qq'
                    or str(source.get('account_id') or '') != account
                    or str(group.get('group_id') or '') != identity['group_id']):
                return None
        else:
            if not identity['user_id']:
                return None
            try:
                source = await self.ctx.chat.get_stream_by_user_id(identity['user_id'], platform='qq')
            except Exception:
                return None
            if (not isinstance(source, dict)
                    or str(source.get('session_id') or source.get('stream_id') or '') != identity['session_id']
                    or str(source.get('account_id') or '') != account or source.get('platform') != 'qq'
                    or str(source.get('user_id') or '') != identity['user_id']
                    or source.get('group_id') or source.get('chat_type') not in ('private', None)):
                return None
        if client is not self._client or str(getattr(client, 'self_id', '') or '') != account:
            return None
        identity['account_id'] = account
        return identity

    async def _resolve_target(self, message_id: str, user_id: str, identity: dict[str, str] | None = None) -> tuple[str, str]:
        """解析工具目标用户：优先 message_id 反查，其次 user_id。返回 (user_id, nickname)。"""
        if identity is None:
            return "", ""
        message_id = str(message_id or "").strip()
        if message_id:
            try:
                result = await self.ctx.message.get_by_id(message_id, chat_id=identity["session_id"])
            except Exception as exc:
                LOGGER.warning("守门人反查消息失败 (%s)", type(exc).__name__)
                result = None
            if (isinstance(result, dict) and result.get('message_id') == message_id
                    and result.get("session_id") == identity["session_id"]
                    and str(result.get("account_id") or "") == identity["account_id"]
                    and result.get("platform") == identity["platform"]):
                msg = result
                info = msg.get("message_info") or {}
                group = info.get("group_info") or {}
                if str(group.get("group_id") or "") != identity["group_id"]:
                    return "", ""
                user_info = info.get("user_info") or {}
                resolved = str(user_info.get("user_id") or "").strip()
                if resolved:
                    return resolved, str(user_info.get("user_nickname") or "")
            return "", ""  # bad message must never fall back to an unrelated supplied ID
        return str(user_id or "").strip(), ""

    # ------------------------------------------------------------------
    # 管理员命令
    # ------------------------------------------------------------------

    @Command(
        name="fr",
        pattern=r"^/fr\b",
        description="好友申请管理（管理员）：/fr list | /fr review | /fr ok 序号 | /fr no 序号",
    )
    async def cmd_fr(self, **kwargs: Any) -> tuple[bool, str, int]:
        """好友申请查看与审批。"""
        if not self._tool_ready():
            return (True, "守门人插件未就绪", 1)
        stream_id = str(kwargs.get("stream_id") or "")
        if not await self._is_admin(kwargs):
            reply = "仅管理员可使用该命令"
        else:
            reply = await self._handle_fr_command(str(kwargs.get("text") or ""), str(kwargs.get("user_id") or ""))
        if stream_id:
            try:
                await self.ctx.send.text(reply, stream_id)
            except Exception as exc:
                LOGGER.warning("守门人回复 /fr 失败 (%s)", type(exc).__name__)
        return (True, reply, 1)

    async def _handle_fr_command(self, text: str, admin_user_id: str) -> str:
        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else "list"
        store = self._require_store()

        if sub == "review":
            account = str(getattr(self._client, "self_id", "") or "")
            rows = store.list_friend_observations(account) if account else []
            lines = ["异常申请（最近10条），请在QQ客户端处理："]
            for row in rows:
                reason = "申请人身份未确认" if row["reason"] == "unresolved_user" else "重复标识，无法确认是否新一轮申请"
                lines.append(f"{row['observed_at']} · {row['user_id'] or 'QQ号未知'} · {reason}")
            return "\n".join(lines) if rows else "当前没有异常申请记录"

        if sub == "list":
            status = parts[2] if len(parts) > 2 else "pending"
            if status not in {"pending", "executing", "unknown", "failed", "approved", "rejected", "expired"}:
                return "不支持的状态"
            requests = store.list_friend_requests(status, 10)
            if not requests:
                return "当前没有待处理的好友申请"
            lines = [f"好友申请 {status}（最新 10 条）："]
            for req in requests:
                comment = (req["comment"] or "（无验证消息）")[:30]
                lines.append(f"#{req['id']} {req['user_id']}（{req['nickname'] or '未知'}）：{comment}")
            lines.append("处理：/fr ok 序号 · /fr no 序号")
            return "\n".join(lines)

        if sub in ("ok", "no"):
            if len(parts) < 3 or not parts[2].isdigit():
                return "用法：/fr ok 序号 或 /fr no 序号（序号见 /fr list）"
            row_id = int(parts[2])
            outcome = await self._execute_decision(
                row_id, sub == "ok", f"admin:{admin_user_id}", "管理员人工审批"
            )
            outcome_text = {
                "review": "该申请需在QQ客户端处理，异常原因见 /fr review",
                "approved": "已同意该好友申请",
                "rejected": "已拒绝该好友申请",
                "expired": "该申请已过期（flag 失效），对方需重新申请",
                "offline": "OneBot 对端未连接，申请保持待处理，稍后重试",
                "not_pending": "该申请不存在或已被处理",
                "unknown": "结果未知，禁止自动重放，请人工核验对端记录",
                "failed": "对端明确返回失败；不推断 flag 过期，禁止自动重放",
                "unscoped": "申请缺少可验证的账号归属（含旧 pending），保留记录并拒绝执行；需人工核验",
                "policy_changed": "审批策略已变化，申请保持待处理",
            }.get(outcome, f"处理异常（{outcome}）")
            return outcome_text

        return "用法：/fr list · /fr review · /fr ok 序号 · /fr no 序号"

    @Command(
        name="block",
        pattern=r"^/block\b",
        description=(
            "屏蔽名单管理（管理员）：/block list | /block un 序号|用户ID | "
            "/block add 用户ID [global|group:群号] 原因"
        ),
    )
    async def cmd_block(self, **kwargs: Any) -> tuple[bool, str, int]:
        """屏蔽名单查看与管理。"""
        if not self._tool_ready():
            return (True, "守门人插件未就绪", 1)
        stream_id = str(kwargs.get("stream_id") or "")
        if not await self._is_admin(kwargs):
            reply = "仅管理员可使用该命令"
        else:
            reply = await self._handle_block_command(
                str(kwargs.get("text") or ""),
                str(kwargs.get("user_id") or ""),
                str(kwargs.get("group_id") or ""),
                str(kwargs.get("platform") or ""),
            )
        if stream_id:
            try:
                await self.ctx.send.text(reply, stream_id)
            except Exception as exc:
                LOGGER.warning("守门人回复 /block 失败 (%s)", type(exc).__name__)
        return (True, reply, 1)

    async def _handle_block_command(
        self, text: str, admin_user_id: str, group_id: str, platform: str
    ) -> str:
        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else "list"
        store = self._require_store()
        platform = platform or self.config.admin.platform

        if sub == "list":
            entries = store.list_blocked(20)
            if not entries:
                return "屏蔽名单为空"
            lines = ["屏蔽名单（最新 20 条）："]
            for entry in entries:
                reason = (entry["reason"] or "无")[:20]
                expiry = str(entry.get("expires_at") or "")
                expiry_note = f" 至 {expiry[5:16]}" if expiry else " 永久"
                lines.append(
                    f"#{entry['id']} {entry['user_id']} [{entry['scope']}]{expiry_note} {reason}"
                    f"（by {entry['blocked_by'] or '手动'}）"
                )
            lines.append("解除：/block un 序号 或 /block un 用户ID")
            return "\n".join(lines)

        if sub == "un":
            if len(parts) < 3:
                return "用法：/block un 序号 或 /block un 用户ID [global|group:群号]"
            target = parts[2]
            if target.isdigit() and store.remove_blocked(int(target)):
                return f"已删除屏蔽记录 #{target}"
            scope = parts[3] if len(parts) > 3 else ""
            removed = store.remove_blocked_by_user(platform, target, scope)
            if removed:
                return f"已解除用户 {target} 的屏蔽（{removed} 条记录）"
            return f"未找到匹配的屏蔽记录（{target}）"

        if sub == "add":
            if len(parts) < 3 or not parts[2].isdigit():
                return "用法：/block add 用户ID [global|group:群号] 原因"
            target = parts[2]
            scope_arg = (
                parts[3]
                if len(parts) > 3 and (parts[3] == "global" or parts[3].startswith("group:"))
                else ""
            )
            if scope_arg:
                scope = scope_arg
                reason = " ".join(parts[4:])
            else:
                scope = group_scope(group_id) if group_id else GLOBAL_SCOPE
                reason = " ".join(parts[3:])
            added = store.add_blocked(platform, target, scope, reason, f"admin:{admin_user_id}")
            if added:
                return f"已屏蔽用户 {target}（范围 {scope}）"
            return f"用户 {target} 在范围 {scope} 已被屏蔽"

        return "用法：/block list · /block un … · /block add …"

    async def _is_admin(self, kwargs: Any) -> bool:
        """管理员判定（三通道，任一命中即放行）。

        1. WebUI 本地控制台：命令上下文的 is_local_operator 标志
           （Host 下发；源码 core/local_operator.py 中它只标记控制台消息，
           不覆盖 QQ 侧操作员）；
        2. 插件配置 [admin].admin_ids；
        3. 主程序级操作员：bot_config.toml [plugin].permission
           （格式 ``platform:id``，平台前缀统一小写比对，
           与主程序 build_scoped_user_id 规则一致）。
        """
        if bool(kwargs.get("is_local_operator")):
            return True
        user_id = str(kwargs.get("user_id") or "").strip()
        if not user_id:
            return False
        admins = {str(a).strip() for a in self.config.admin.admin_ids}
        if user_id in admins:
            return True

        permissions: Any = []
        try:
            permissions = await self.ctx.config.get("plugin.permission", [])
        except Exception as exc:
            LOGGER.debug("守门人读取主程序操作员配置失败 (%s)", type(exc).__name__)
        if isinstance(permissions, list):
            platform = str(kwargs.get("platform") or "").strip().lower()
            scoped = f"{platform}:{user_id}"
            for entry in permissions:
                parts = str(entry).strip().split(":", 1)
                if len(parts) == 2 and f"{parts[0].strip().lower()}:{parts[1].strip()}" == scoped:
                    return True
        return False

    # ------------------------------------------------------------------
    # 组件作用域
    # ------------------------------------------------------------------

    def get_components(self) -> list[dict[str, Any]]:
        """按组件注入 chat_scope（SDK 的 collect_components 不会自动输出该顶层字段）。

        - block_user / unblock_user：仅群聊暴露（屏蔽作用域为本群）
        - remove_friend：仅私聊暴露
        - Hook / Command：all（Hook 无作用域概念；Command 本就不按 chat_scope 过滤）
        """
        components = super().get_components()
        scope_by_name = {
            "block_user": "group",
            "unblock_user": "group",
            "remove_friend": "private",
        }
        for component in components:
            name = component.get("name") or ""
            if name in scope_by_name:
                component["chat_scope"] = scope_by_name[name]
        return components


def create_plugin() -> MaiBotPlugin:
    """MaiBot 插件工厂函数。"""
    return MaiGateKeeper()
