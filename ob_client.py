"""麦麦守门人（Mai_gatekeeper）OneBot v11 正向 WebSocket 客户端。

不修改 snowluma-adapter：插件以第二个客户端身份直连同一个对端
（NapCat / SnowLuma，两者均为标准 OneBot v11 协议），用于：

- 接收 ``post_type == "request"`` 好友申请事件（适配器路由层不处理该类
  事件，但对端会向所有连接的客户端广播）；
- 发送 ``set_friend_add_request`` / ``delete_friend`` / ``get_stranger_info``
  等 action。

连接与鉴权方式与 snowluma-adapter/transport.py 一致：地址为
``ws://server:port``，token 同时以 URL ``?access_token=`` 参数与
``Authorization: Bearer`` 头双通道携带（两类对端各取所需）；
action 以 ``{"action", "params", "echo"}`` 发送，按 echo 匹配响应。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, WSMsgType

LOGGER = logging.getLogger(__name__)


@dataclass
class OneBotConnectionConfig:
    """对端连接参数。"""

    server: str = ""
    port: int = 0
    token: str = ""
    heartbeat_interval: float = 30.0
    reconnect_delay_sec: float = 5.0
    action_timeout_sec: float = 15.0

    @property
    def ws_url(self) -> str:
        base = f"ws://{self.server}:{self.port}"
        if not self.token:
            return base
        return f"{base}?{urlencode({'access_token': self.token})}"

    @property
    def display_url(self) -> str:
        base = f"ws://{self.server}:{self.port}"
        if not self.token:
            return base
        return f"{base}?access_token=***"

    @property
    def effective(self) -> bool:
        return bool(self.server.strip()) and bool(self.port)


@dataclass
class PeerInfo:
    """对端实现信息（连接后探测，仅用于日志与兼容标记）。"""

    app_name: str = ""
    app_version: str = ""
    client_type: str = ""


@dataclass
class _ClientState:
    """客户端运行状态。"""

    peer_info: PeerInfo = field(default_factory=PeerInfo)
    connected: bool = False


class OneBotClient:
    """OneBot v11 正向 WebSocket 客户端（连接循环 + echo 匹配的 action 调用）。"""

    def __init__(
        self,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        logger: Any = LOGGER,
    ) -> None:
        """初始化客户端。

        Args:
            on_event: 收到无 echo 的事件载荷后的异步回调。
            logger: 日志对象。
        """
        self._logger = logger
        self._on_event = on_event
        self._config: OneBotConnectionConfig | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._pending_actions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._send_lock = asyncio.Lock()
        self._ws: Any = None
        self._stop_requested = False
        self._state = _ClientState()
        self.self_id = ""
        self._event_overflow_logged = False

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    @property
    def peer_info(self) -> PeerInfo:
        return self._state.peer_info

    @property
    def connected(self) -> bool:
        return self._state.connected

    def configure(self, config: OneBotConnectionConfig) -> None:
        """更新连接配置（下次连接循环迭代生效）。"""
        self._config = config

    async def start(self) -> None:
        """启动连接循环（幂等）。"""
        if self._config is None or not self._config.effective:
            raise RuntimeError("OneBot 连接配置不完整（server/port 为空）")
        if self._connection_task is not None and not self._connection_task.done():
            return
        self._stop_requested = False
        self._connection_task = asyncio.create_task(
            self._connection_loop(), name="gatekeeper.ob_connection"
        )

    async def stop(self) -> None:
        """停止连接并清理所有后台任务。"""
        self._stop_requested = True
        self.self_id = ""
        connection_task = self._connection_task
        self._connection_task = None

        ws = self._ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()
        self._ws = None

        if connection_task is not None:
            connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connection_task

        await self._cancel_background_tasks()
        self._state.connected = False
        self._fail_pending_actions("连接已关闭")

    async def restart(self) -> None:
        """按当前配置重启连接。"""
        await self.stop()
        await self.start()

    async def call_action(self, action_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """发送 OneBot 动作并等待 echo 响应。

        Returns:
            dict[str, Any]: 对端返回的原始响应（含 status / retcode / data）。

        Raises:
            RuntimeError: 连接不可用、超时或插件停止时抛出。
        """
        ws = self._ws
        config = self._config
        if ws is None or ws.closed or config is None:
            raise RuntimeError("OneBot 对端未连接")
        if len(self._pending_actions) >= 32:
            raise RuntimeError("OneBot action capacity reached")

        echo_id = uuid4().hex
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_actions[echo_id] = response_future

        request_payload = {"action": action_name, "params": params, "echo": echo_id}
        try:
            async with self._send_lock:
                await ws.send_str(json.dumps(request_payload, ensure_ascii=False))
            return await asyncio.wait_for(response_future, timeout=config.action_timeout_sec)
        finally:
            self._pending_actions.pop(echo_id, None)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        """维护单个 WebSocket 连接，并在断开后按配置重连。"""
        while not self._stop_requested:
            config = self._config
            if config is None or not config.effective:
                return

            timeout = ClientTimeout(total=None, connect=10)
            headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
            self._logger.debug(f"守门人开始连接 OneBot 对端: {config.display_url}")

            try:
                async with ClientSession(trust_env=False, headers=headers, timeout=timeout) as session:
                    async with session.ws_connect(
                        config.ws_url, heartbeat=config.heartbeat_interval or None
                    ) as ws:
                        self._ws = ws
                        self._state.connected = True
                        self._logger.info(f"守门人已连接 OneBot 对端: {config.display_url}")
                        self._create_background_task(self._probe_peer(), "gatekeeper.probe")
                        disconnect_reason = await self._receive_loop(ws)
                        self._logger.warning(
                            f"守门人 OneBot 连接断开: {config.display_url}，{disconnect_reason}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(f"守门人连接 OneBot 对端失败 ({type(exc).__name__})；将在 {config.reconnect_delay_sec:g} 秒后重连")
            finally:
                self._ws = None
                self._state.connected = False
                self.self_id = ""
                self._fail_pending_actions("连接已中断")
                await self._cancel_background_tasks()

            if self._stop_requested:
                break
            await asyncio.sleep(config.reconnect_delay_sec)

    async def _probe_peer(self) -> None:
        """连接后探测对端实现类型（napcat / snowluma），仅用于日志。"""
        try:
            login = await self.call_action("get_login_info", {})
            if (not isinstance(login, dict) or login.get("status") != "ok"
                    or type(login.get("retcode")) is not int or login["retcode"] != 0
                    or not isinstance(login.get("data"), dict)):
                return
            self.self_id = str(login["data"].get("user_id") or "").strip()
            if not self.self_id:
                return
            response = await self.call_action("get_version_info", {})
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, dict):
                return
            app_name = str(data.get("app_name") or data.get("app") or "").strip()
            app_version = str(data.get("app_version") or data.get("version") or "").strip()
            normalized = app_name.lower()
            client_type = "snowluma" if "snowluma" in normalized else ("napcat" if "napcat" in normalized else "unknown")
            self._state.peer_info = PeerInfo(app_name=app_name, app_version=app_version, client_type=client_type)
            self._logger.info(f"守门人对端类型: {client_type}")
        except Exception as exc:
            self._logger.debug(f"守门人探测对端类型失败 ({type(exc).__name__})")

    async def _receive_loop(self, ws: Any) -> str:
        """持续消费 WebSocket 消息：echo 响应回填 future，事件交给回调。"""
        disconnect_reason = "未收到更多 WebSocket 消息，连接已结束"
        try:
            async for ws_message in ws:
                if ws_message.type != WSMsgType.TEXT:
                    if ws_message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    continue

                payload = self._parse_json_message(ws_message.data)
                if payload is None:
                    continue

                echo_id = str(payload.get("echo") or "").strip()
                if echo_id:
                    self._resolve_pending_action(echo_id, payload)
                    continue

                if payload.get("post_type") != "request" or payload.get("request_type") != "friend":
                    continue
                if len(self._background_tasks) >= 16:
                    if not self._event_overflow_logged:
                        self._logger.warning("守门人事件容量已满；未自动审批，需人工检查对端申请")
                        self._event_overflow_logged = True
                    continue
                self._event_overflow_logged = False
                self._create_background_task(self._on_event(payload), "gatekeeper.event")
        finally:
            pass
        return disconnect_reason

    def _create_background_task(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        """创建并跟踪一个后台任务。"""
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_background_task_completion)
        return task

    def _handle_background_task_completion(self, task: asyncio.Task[Any]) -> None:
        """后台任务结束后的清理与异常记录。"""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            self._logger.error(f"守门人后台任务异常 ({type(exception).__name__})")

    async def _cancel_background_tasks(self) -> None:
        """取消所有仍在运行的后台任务。"""
        background_tasks = list(self._background_tasks)
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    def _resolve_pending_action(self, echo_id: str, payload: dict[str, Any]) -> None:
        """把 echo 响应回填给等待中的 future。"""
        response_future = self._pending_actions.get(echo_id)
        if response_future is None or response_future.done():
            return
        response_future.set_result(payload)

    def _fail_pending_actions(self, error_message: str) -> None:
        """让所有等待中的动作以异常方式结束。"""
        for response_future in self._pending_actions.values():
            if not response_future.done():
                response_future.set_exception(RuntimeError(error_message))
        self._pending_actions.clear()

    def _parse_json_message(self, data: Any) -> dict[str, Any] | None:
        """解析 WebSocket 文本消息中的 JSON 数据。"""
        try:
            if len(str(data)) > 65536:
                return None
            payload = json.loads(str(data))
        except Exception as exc:
            self._logger.warning(f"守门人解析 JSON 载荷失败 ({type(exc).__name__})")
            return None
        return payload if isinstance(payload, dict) else None
