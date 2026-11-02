"""Real SDK normalization, synthetic SQLite/actions, no network or production data."""
import asyncio
import ast
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from maibot_sdk.context import PluginContext
from Mai_gatekeeper.plugin import MaiGateKeeper
from Mai_gatekeeper.store import GateKeeperStore
from Mai_gatekeeper.ob_client import OneBotClient


def identity(group="g", user="caller", account="bot", session="shared-focus-member"):
    return dict(contract="neko.tools.context.v1", session_id=session, stream_id=session,
                platform="qq", group_id=group, user_id=user, account_id=account)


class Hardening(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gatekeeper-offline-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.db"
        self.plugin = MaiGateKeeper()
        self.plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"},
            "approve": {"mode": "hybrid", "notify_admin_on_decision": False},
            "admin": {"admin_ids": ["admin"]},
            "block": {"allow_remove_friend": True, "notify_admin_on_remove": False}})
        self.store = GateKeeperStore(self.path)
        self.addCleanup(self.store.close)
        self.plugin._store = self.store
        self.actions = []
        self.rpc_calls = []
        self.permission = ["QQ:operator"]
        self.message = dict(message_id='m', session_id="shared-focus-member", account_id="bot", platform="qq",
            message_info={"group_info": {"group_id": "g"},
                          "user_info": {"user_id": "target", "user_nickname": "name"}})

        async def rpc(method, plugin_id, payload, **kwargs):
            self.rpc_calls.append(payload)
            name = payload["capability"]
            if name == "config.get":
                return {"success": True, "value": self.permission}
            if name == "message.get_by_id":
                return {"success": True, "message": self.message}
            if name == "send.text":
                return {"success": True, "sent": True}
            raise AssertionError(name)
        self.plugin._set_context(PluginContext("octmicy.mai-gatekeeper", rpc_call=rpc))

        async def action(name, params):
            self.actions.append((name, params))
            return {"status": "ok", "retcode": 0}
        self.plugin._client = SimpleNamespace(connected=True, self_id="bot", call_action=action,
                                              stop=AsyncMock())

    def request(self, account="bot"):
        return self.store.add_friend_request("flag-" + str(len(self.store.list_friend_requests())),
                                            "target", account_id=account)

    async def test_actual_sdk_lookup_scopes_shared_focus_member(self):
        result = await self.plugin.tool_block_user(message_id="m", user_id="fallback",
            group_id="forged", platform="telegram", _host_context=identity())
        self.assertTrue(result["success"])
        self.assertTrue(self.store.is_blocked("qq", "target", "g"))
        self.assertFalse(self.store.is_blocked("qq", "fallback", "forged"))
        lookup = next(p for p in self.rpc_calls if p["capability"] == "message.get_by_id")
        self.assertEqual(lookup["args"]["chat_id"], "shared-focus-member")

    async def test_anchorless_focus_identity_requires_matching_scoped_message(self):
        envelope = identity(user='', account='')
        result = await self.plugin.tool_block_user(message_id='m', _host_context=envelope,
                                                   user_id='forged-caller', account_id='forged-account')
        self.assertTrue(result['success'])
        self.assertTrue(self.store.is_blocked('qq', 'target', 'g'))
        self.assertEqual(self.actions, [])
        await self.plugin.tool_unblock_user(message_id='m', _host_context=envelope)
        self.message['account_id'] = 'foreign'
        self.assertFalse((await self.plugin.tool_block_user(message_id='m', _host_context=envelope))['success'])
        self.message['account_id'] = 'bot'
        self.message['message_id'] = 'wrong-id'
        self.assertFalse((await self.plugin.tool_block_user(message_id='m', _host_context=envelope))['success'])
        self.assertFalse((await self.plugin.tool_block_user(user_id='target', _host_context=envelope))['success'])

    async def test_anchorless_private_uses_existing_stream_without_opening(self):
        calls = []
        async def rpc(method, plugin_id, payload, **kwargs):
            calls.append(payload['capability'])
            self.assertEqual(payload['capability'], 'chat.get_stream_by_user_id')
            return {'success': True, 'stream': {'stream_id': 's', 'platform':'qq',
                    'account_id':'bot', 'user_id':'peer', 'chat_type':'private'}}
        self.plugin._set_context(PluginContext('octmicy.mai-gatekeeper', rpc_call=rpc))
        result = await self.plugin._resolve_tool_identity({'_host_context': identity(
            group='', user='peer', account='', session='s')})
        self.assertEqual(result['account_id'], 'bot')
        self.assertEqual(calls, ['chat.get_stream_by_user_id'])

    async def test_lookup_other_chat_account_group_platform_never_falls_back(self):
        for field, value in [("session_id", "foreign"), ("account_id", "other"),
                             ("platform", "telegram")]:
            original = self.message[field]
            self.message[field] = value
            self.assertEqual(await self.plugin._resolve_target("m", "fallback", identity()), ("", ""))
            self.message[field] = original
        self.message["message_info"]["group_info"]["group_id"] = "other"
        self.assertEqual(await self.plugin._resolve_target("m", "fallback", identity()), ("", ""))

    async def test_missing_identity_and_model_kwargs_cannot_authorize_tools(self):
        for method in (self.plugin.tool_block_user, self.plugin.tool_unblock_user, self.plugin.tool_remove_friend):
            result = await method(user_id="target", group_id="g", stream_id="s", platform="qq", account_id="bot")
            self.assertFalse(result["success"])
        for key in ("account_id", "session_id", "user_id"):
            envelope = identity(); envelope[key] = ""
            self.assertFalse((await self.plugin.tool_block_user(user_id="target", _host_context=envelope))["success"])
        self.assertEqual(self.actions, [])

    async def test_real_sdk_metadata_opt_in_not_in_model_schema(self):
        tools = [c for c in self.plugin.get_components() if c["type"] == "TOOL"]
        self.assertEqual(len(tools), 3)
        for tool in tools:
            self.assertEqual(tool["metadata"]["trusted_context"], "neko.tools.context.v1")
            self.assertNotIn("_host_context", tool["metadata"].get("parameters_schema", {}).get("properties", {}))

    async def test_protect_host_operator_bot_and_fail_closed_config(self):
        for target in ("operator", "bot", "admin"):
            self.assertTrue(await self.plugin._is_protected(target, identity()))
            self.assertFalse((await self.plugin.tool_block_user(user_id=target, _host_context=identity()))["success"])
        self.assertTrue(await self.plugin._is_admin({"user_id": "operator", "platform": "qq"}))
        self.permission = None
        self.assertTrue(await self.plugin._is_protected("target", identity()))
        self.assertFalse((await self.plugin.tool_block_user(user_id="target", _host_context=identity()))["success"])

    async def test_concurrent_opposite_decisions_send_only_once_after_claim(self):
        row = self.request()
        entered, release = asyncio.Event(), asyncio.Event()
        async def action(name, params):
            self.assertEqual(self.store.get_friend_request(row)["status"], "executing")
            self.actions.append(name); entered.set(); await release.wait()
            return {"status": "ok", "retcode": 0}
        self.plugin._client.call_action = action
        first = asyncio.create_task(self.plugin._execute_decision(row, True, "admin:a", ""))
        await entered.wait()
        second = asyncio.create_task(self.plugin._execute_decision(row, False, "admin:b", ""))
        release.set()
        self.assertEqual(await asyncio.gather(first, second), ["approved", "not_pending"])
        self.assertEqual(self.actions, ["set_friend_add_request"])

    async def test_timeout_unknown_never_replayed(self):
        row = self.request()
        self.plugin._client.call_action = AsyncMock(side_effect=TimeoutError("SECRET-FLAG"))
        with self.assertLogs("MaiGateKeeper", level="WARNING") as log:
            self.assertEqual(await self.plugin._execute_decision(row, True, "admin:a", ""), "unknown")
        self.assertNotIn("SECRET-FLAG", str(log.output))
        self.assertEqual(self.store.get_friend_request(row)["status"], "unknown")
        self.assertEqual(await self.plugin._execute_decision(row, True, "admin:a", ""), "not_pending")
        self.assertEqual(self.plugin._client.call_action.await_count, 1)

    async def test_cancelled_action_is_durable_unknown(self):
        row = self.request(); entered = asyncio.Event()
        async def action(*args):
            entered.set(); await asyncio.Event().wait()
        self.plugin._client.call_action = action
        task = asyncio.create_task(self.plugin._execute_decision(row, True, "admin:a", ""))
        await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.store.get_friend_request(row)["status"], "unknown")

    async def test_explicit_failure_not_expiry_and_malformed_unknown(self):
        for response, expected in [({"status": "failed", "retcode": 1200}, "failed"),
                                   ({"status": "ok", "retcode": 42}, "unknown"),
                                   ({"retcode": 0}, "unknown"), ({"status": "ok"}, "unknown"),
                                   ({"status": "ok", "retcode": False}, "unknown"), (None, "unknown")]:
            row = self.store.add_friend_request("unique-" + str(response), "target", account_id="bot")
            self.plugin._client.call_action = AsyncMock(return_value=response)
            self.assertEqual(await self.plugin._execute_decision(row, True, "admin:a", ""), expected)
            self.assertEqual(await self.plugin._execute_decision(row, True, "admin:a", ""), "not_pending")

    async def test_legacy_unscoped_and_wrong_account_pending_preserved(self):
        for account in ("", "other"):
            row = self.store.add_friend_request("legacy-" + account, "target", account_id=account)
            self.assertEqual(await self.plugin._execute_decision(row, True, "admin:a", ""), "unscoped")
            self.assertEqual(self.store.get_friend_request(row)["status"], "pending")
        self.assertEqual(self.actions, [])

    async def test_manual_revocation_during_llm_preserves_pending(self):
        row = self.request(); entered, release = asyncio.Event(), asyncio.Event()
        async def review(*args):
            entered.set(); await release.wait()
            return {"approve": True, "confidence": 1.0, "reason": "synthetic"}
        self.plugin._llm_review = review
        task = asyncio.create_task(self.plugin._dispatch_friend_request(row, "target", "", ""))
        await entered.wait()
        self.plugin.config.approve.mode = "manual"
        await self.plugin.on_config_update("self", {}, "synthetic")
        release.set(); await task
        self.assertEqual(self.actions, [])
        self.assertEqual(self.store.get_friend_request(row)["status"], "pending")

    async def test_policy_rechecked_under_lifecycle_lock(self):
        row = self.request(); owner = self.plugin._client
        async with self.plugin._lifecycle_lock:
            task = asyncio.create_task(self.plugin._execute_decision(row, True, "llm:auto", "",
                generation=self.plugin._generation, owner=owner, confidence=.9))
            await asyncio.sleep(0)
            self.plugin.config.approve.mode = "manual"
        self.assertEqual(await task, "policy_changed")
        self.assertEqual(self.actions, [])

    async def test_notification_failure_does_not_redo_approval(self):
        self.plugin.config.approve.notify_admin_on_decision = True
        self.plugin._push_admins = AsyncMock(side_effect=RuntimeError("synthetic"))
        row = self.request()
        self.assertEqual(await self.plugin._execute_decision(row, True, "admin:a", ""), "approved")
        self.assertEqual(await self.plugin._execute_decision(row, True, "admin:a", ""), "not_pending")
        self.assertEqual(len(self.actions), 1)

    async def test_incomplete_connection_stops_old_client(self):
        client = self.plugin._client
        await self.plugin._start_ob_client(restart_if_changed=True)
        client.stop.assert_awaited_once()
        self.assertIsNone(self.plugin._client)
        self.assertIsNone(self.plugin._active_ob_config)

    async def test_sdk_existing_admin_stream_no_open_and_account_routing(self):
        calls = []
        account = "bot"
        def stream(a): return {"stream_id": "s-" + a, "account_id": a,
                               "platform": "qq", "user_id": "admin", "chat_type": "private"}
        async def rpc(method, plugin_id, payload, **kwargs):
            calls.append(payload)
            if payload["capability"] == "chat.get_stream_by_user_id":
                return {"success": True, "stream": stream(account)}
            self.assertEqual(payload["capability"], "chat.open_session")
            self.assertEqual(payload["args"]["account_id"], "bot")
            return {"success": True, "stream": stream("bot")}
        self.plugin._set_context(PluginContext("octmicy.mai-gatekeeper", rpc_call=rpc))
        self.assertEqual(await self.plugin._resolve_admin_stream("admin"), "s-bot")
        self.assertEqual(len(calls), 1)
        account = "other"
        self.assertEqual(await self.plugin._resolve_admin_stream("admin"), "s-bot")
        self.assertEqual(len(calls), 3)

    async def test_admin_lookup_account_change_on_same_client_aborts_route(self):
        async def rpc(method, plugin_id, payload, **kwargs):
            self.plugin._client.self_id = "other"
            return {"success": True, "stream": {"stream_id": "old-account-stream", "account_id": "bot",
                    "platform": "qq", "user_id": "admin", "chat_type": "private"}}
        self.plugin._set_context(PluginContext("octmicy.mai-gatekeeper", rpc_call=rpc))
        self.assertIsNone(await self.plugin._resolve_admin_stream("admin"))

    async def test_notification_cannot_route_old_request_via_new_account(self):
        self.plugin._resolve_admin_stream = AsyncMock(return_value="new-stream")
        await self.plugin._push_admins("synthetic old account result", account_id="other")
        self.plugin._resolve_admin_stream.assert_not_awaited()

    async def test_delete_concurrent_and_unknown_no_retry(self):
        self.plugin._client.call_action = AsyncMock(side_effect=TimeoutError())
        args = {"_host_context": identity(group="", user="target")}
        results = await asyncio.gather(self.plugin.tool_remove_friend(**args), self.plugin.tool_remove_friend(**args))
        self.assertTrue(all(not result["success"] for result in results))
        self.assertEqual(self.plugin._client.call_action.await_count, 1)
        self.assertEqual(self.store._conn.execute("SELECT status FROM action_ledger").fetchone()[0], "unknown")
        self.assertFalse(self.store.is_blocked("qq", "target"))

    async def test_delete_target_is_trusted_private_peer_not_kwargs(self):
        result = await self.plugin.tool_remove_friend(user_id="forged", group_id="forged",
                                                      _host_context=identity(group="", user="target"))
        self.assertTrue(result["success"])
        self.assertEqual(self.actions, [("delete_friend", {"user_id": "target"})])
        self.assertTrue(self.store.is_blocked("qq", "target"))
        self.assertFalse((await self.plugin.tool_remove_friend(_host_context=identity()))["success"])

    async def test_delete_wrong_account_or_protected_peer(self):
        for ctx in (identity(group="", account="other"), identity(group="", user="operator"),
                    identity(group="", user="bot")):
            self.assertFalse((await self.plugin.tool_remove_friend(_host_context=ctx))["success"])
        self.assertEqual(self.actions, [])

    async def test_delete_notification_failure_still_returns_success_and_no_replay(self):
        self.plugin.config.block.notify_admin_on_remove = True
        self.plugin._push_admins = AsyncMock(side_effect=RuntimeError("synthetic"))
        args = {"_host_context": identity(group="", user="target")}
        self.assertTrue((await self.plugin.tool_remove_friend(**args))["success"])
        self.assertFalse((await self.plugin.tool_remove_friend(**args))["success"])
        self.assertEqual(len(self.actions), 1)

    async def test_delete_strict_failed_or_malformed_response_is_not_replayed(self):
        for n, response in enumerate(({"status": "failed", "retcode": 1}, {"status": "ok", "retcode": 2})):
            self.plugin._client.call_action = AsyncMock(return_value=response)
            args = {"_host_context": identity(group="", user="peer" + str(n))}
            self.assertFalse((await self.plugin.tool_remove_friend(**args))["success"])
            self.assertFalse((await self.plugin.tool_remove_friend(**args))["success"])
            self.assertEqual(self.plugin._client.call_action.await_count, 1)

    async def test_connection_lifecycle_waits_for_inflight_action(self):
        row = self.request(); entered, release = asyncio.Event(), asyncio.Event()
        client = self.plugin._client
        async def action(*args):
            entered.set(); await release.wait()
            return {"status": "ok", "retcode": 0}
        client.call_action = action
        decision = asyncio.create_task(self.plugin._execute_decision(row, True, "admin:a", ""))
        await entered.wait()
        self.plugin.config.plugin.enabled = False
        update = asyncio.create_task(self.plugin.on_config_update("self", {}, "synthetic"))
        await asyncio.sleep(0)
        client.stop.assert_not_awaited()
        release.set()
        self.assertEqual(await decision, "approved")
        await update
        client.stop.assert_awaited_once()

    async def test_source_code_does_not_swallow_negative_finish_cas(self):
        row = self.request()
        self.assertFalse(self.store.claim_request(row, "other", "admin:a", ""))
        self.assertTrue(self.store.claim_request(row, "bot", "admin:a", ""))
        self.assertFalse(self.store.claim_request(row, "bot", "admin:b", ""))
        self.store.finish_request(row, "approved")
        with self.assertRaises(RuntimeError): self.store.finish_request(row, "rejected")

    async def test_corrupt_schema_and_future_db_version_are_not_rebuilt(self):
        path = Path(self.temp.name) / "future.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA user_version=99")
        with self.assertRaises(RuntimeError): GateKeeperStore(path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 99)

    async def test_ws_action_timeout_removes_pending_future(self):
        from Mai_gatekeeper.ob_client import OneBotConnectionConfig
        client = OneBotClient(AsyncMock())
        client.configure(OneBotConnectionConfig(server="synthetic.invalid", port=1, action_timeout_sec=.01))
        client._ws = SimpleNamespace(closed=False, send_str=AsyncMock())
        with self.assertRaises(TimeoutError): await client.call_action("synthetic", {})
        self.assertEqual(client._pending_actions, {})

    async def test_restart_preserves_old_data_and_converts_inflight_unknown(self):
        self.store.add_blocked("qq", "blocked", "global")
        row = self.request()
        self.assertTrue(self.store.claim_request(row, "bot", "admin:a", "yes"))
        self.assertTrue(self.store.claim_action("delete_friend", "key"))
        pending = self.store.add_friend_request("untouched", "pending-user")
        self.store.close()
        reopened = GateKeeperStore(self.path); self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_friend_request(row)["status"], "unknown")
        self.assertEqual(reopened.get_friend_request(pending)["status"], "pending")
        self.assertTrue(reopened.is_blocked("qq", "blocked"))
        self.assertFalse(reopened.claim_action("delete_friend", "key"))

    async def test_migration_backup_preserves_original_and_corruption_fails_closed(self):
        path = Path(self.temp.name) / "legacy.db"
        old_source = Path(__file__).resolve().parents[1] / "store.py"
        tree = ast.parse(old_source.read_text(encoding="utf-8"))
        schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "_SCHEMA" for t in n.targets))
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(schema)
            conn.execute("INSERT INTO friend_requests (flag,user_id,received_at) VALUES ('legacy','u','now')")
            conn.commit()
        migrated = GateKeeperStore(path); self.addCleanup(migrated.close)
        self.assertEqual(migrated.get_friend_request(1)["status"], "pending")
        self.assertEqual(migrated.get_friend_request(1)["account_id"], "")
        with closing(sqlite3.connect(str(path) + ".pre-0.6.8.bak")) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(backup.execute("SELECT flag FROM friend_requests").fetchone()[0], "legacy")
        corrupt = Path(self.temp.name) / "corrupt.db"; corrupt.write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError): GateKeeperStore(corrupt)
        self.assertEqual(corrupt.read_bytes(), b"not sqlite")

    async def test_event_identity_and_input_limits(self):
        self.plugin._dispatch_friend_request = AsyncMock()
        event = dict(post_type="request", request_type="friend", self_id="other", user_id="u", flag="f")
        await self.plugin._on_ob_event(event)
        self.assertEqual(self.store.list_friend_requests(), [])
        event.update(self_id="bot", comment="x" * 10000)
        await self.plugin._on_ob_event(event)
        self.assertEqual(len(self.store.list_friend_requests()[0]["comment"]), 2000)

    async def test_llm_bounded_input_and_nonfinite_output(self):
        calls = []
        async def rpc(method, plugin_id, payload, **kwargs):
            calls.append(payload)
            return {"success": True, "response": '{"approve":true,"confidence":0.9}'}
        self.plugin._set_context(PluginContext("octmicy.mai-gatekeeper", rpc_call=rpc))
        self.plugin.config.approve.extra_rules = "r" * 10000
        self.assertIsNotNone(await self.plugin._llm_review("u" * 10000, "n" * 10000, "c" * 10000))
        prompt = calls[0]["args"]["prompt"]
        self.assertLess(len(prompt[1]["content"]), 6300)
        self.assertIsNone(self.plugin._parse_review('{"approve":true,"confidence":NaN}'))
        self.assertIsNone(self.plugin._parse_review("x" * 8193))

    async def test_ws_limits_and_strict_login_identity(self):
        client = OneBotClient(AsyncMock())
        self.assertIsNone(client._parse_json_message("x" * 65537))
        responses = [{"status": "ok", "retcode": 0, "data": {"user_id": "bot"}},
                     {"status": "ok", "retcode": 0, "data": {"app_name": "NapCat"}}]
        client.call_action = AsyncMock(side_effect=responses)
        await client._probe_peer()
        self.assertEqual(client.self_id, "bot")
        await client.stop()
        self.assertEqual(client.self_id, "")
        client.call_action = AsyncMock(return_value={"status": "failed", "retcode": 0, "data": {"user_id": "forged"}})
        await client._probe_peer()
        self.assertEqual(client.self_id, "")

    async def test_ws_event_capacity_does_not_starve_echo(self):
        from aiohttp import WSMsgType
        payloads = [dict(post_type="request", request_type="friend", flag=str(n)) for n in range(100)]
        payloads.append(dict(echo="expected", status="ok", retcode=0))
        class WS:
            async def __aiter__(self):
                for payload in payloads:
                    yield SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(payload))
        async def event(payload): await asyncio.Event().wait()
        client = OneBotClient(event)
        future = asyncio.get_running_loop().create_future()
        client._pending_actions["expected"] = future
        await client._receive_loop(WS())
        self.assertLessEqual(len(client._background_tasks), 16)
        self.assertEqual((await future)["status"], "ok")
        await client.stop()

    async def test_disabled_commands_and_tools_are_inert(self):
        self.plugin.config.plugin.enabled = False
        self.assertIn("未就绪", (await self.plugin.cmd_fr(user_id="admin", text="/fr list"))[1])
        self.assertIn("未就绪", (await self.plugin.cmd_block(user_id="admin", text="/block list"))[1])
        self.assertFalse((await self.plugin.tool_block_user(user_id="target", _host_context=identity()))["success"])
