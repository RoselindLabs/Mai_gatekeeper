"""Offline actual SDK -> Host registration/filtering -> mixed Focus routing proof.

Accepts a plugin source directory so the same harness can verify Bilibili history.
Only the RPC transport and synthetic authorization are replaced; no model/send IO.
"""
import argparse
import asyncio
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


async def verify(args):
    host, source = args.host_source.resolve(), args.plugin_source.resolve()
    with ExitStack() as cleanup:
        instance = cleanup.enter_context(TemporaryDirectory(prefix='platform-proof-', dir=args.temp_dir))
        previous = Path.cwd()
        os.chdir(instance)
        cleanup.callback(os.chdir, previous)
        os.environ.update(MAIBOT_INSTANCE_DIR=instance, MAIBOT_LOCALE='zh-CN')
        sys.path[:0] = [str(host / 'scripts'), str(host)]
        import neko_pytest_guard  # noqa: F401
        from src.core.tooling import ToolAvailabilityContext, ToolExecutionContext, ToolInvocation
        from src.plugin_runtime.component_query import ComponentQueryService
        from src.plugin_runtime.host.component_registry import ComponentRegistry
        from src.maisaka.focus.tools import FocusToolRouter
        from src.common.database.database import engine
        from src.common.logger import close_handlers
        cleanup.callback(engine.dispose)
        cleanup.callback(close_handlers)
        spec = importlib.util.spec_from_file_location('platform_subject', source / '__init__.py',
                                                     submodule_search_locations=[str(source)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        plugin_module = __import__('platform_subject.plugin', fromlist=['create_plugin'])
        plugin = plugin_module.create_plugin()
        components = plugin.get_components()
        plugin_id = json.loads((source / '_manifest.json').read_text(encoding='utf-8'))['id']
        tools = {c['name'] for c in components if c['type'] == 'TOOL'}
        assert tools
        registry = ComponentRegistry()
        registry.register_plugin_components(plugin_id, [
            {**c, 'component_type': c['type']} for c in components if c['type'] == 'TOOL'
        ])
        transport = AsyncMock(return_value=SimpleNamespace(payload={'success': True, 'result': {'success': True}}))
        supervisor = SimpleNamespace(component_registry=registry, invoke_plugin=transport)
        service = ComponentQueryService()
        cleanup.enter_context(patch.object(service, '_iter_supervisors', return_value=[supervisor]))
        cleanup.enter_context(patch('src.plugin_runtime.component_query.can_use_plugin', return_value=True))
        cleanup.enter_context(patch('src.files.service.get_file_service',
            return_value=SimpleNamespace(enabled=False, revoke=lambda scope: None)))
        for platform in ('qq', 'telegram', ''):
            names = set(service.get_llm_available_tool_specs(ToolAvailabilityContext(platform=platform)))
            assert names == (tools if platform == 'qq' else set())
            for name in tools:
                transport.reset_mock()
                result = await service.invoke_tool_as_tool(ToolInvocation(name), ToolExecutionContext(platform=platform))
                assert result.success == (platform == 'qq')
                assert transport.await_count == int(platform == 'qq')
        for name in tools:
            transport.reset_mock()
            assert not (await service.invoke_tool_as_tool(ToolInvocation(name), None)).success
            transport.assert_not_awaited()

        async def list_tools(context):
            return list(service.get_llm_available_tool_specs(context).values())

        async def get_tool_spec(name, context):
            return service.get_llm_available_tool_specs(context).get(name)

        facade = SimpleNamespace(list_tools=list_tools, get_tool_spec=get_tool_spec, invoke=service.invoke_tool_as_tool)
        covered = set()
        for is_group in (True, False):
            members = {platform: SimpleNamespace(
                session_id=platform, chat_stream=SimpleNamespace(platform=platform, is_group_session=is_group,
                    group_id='group' if is_group else '', user_id='' if is_group else 'peer'), _tool_registry=facade,
                update_deferred_tool_specs=lambda specs: None, expand_message_id_aliases=lambda values: values,
            ) for platform in ('qq', 'telegram')}
            router = FocusToolRouter(SimpleNamespace(members=members, is_member_valid=lambda runtime: True, scope_id='mixed'))
            specs = await router.refresh()
            for spec in specs:
                covered.add(spec.name)
                assert spec.parameters_schema['properties']['session_id']['enum'] == ['qq']
                transport.reset_mock()
                assert not (await router.invoke(spec.name, {'session_id': 'telegram'}, 'test', ''))[1].success
                transport.assert_not_awaited()
                assert (await router.invoke(spec.name, {'session_id': 'qq'}, 'test', ''))[1].success
                assert transport.await_count == 1
        assert covered == tools
        print(json.dumps({'plugin': plugin_id, 'tools': sorted(tools), 'real_sdk_host_focus': True,
                          'qq_route_preserved': True, 'tg_and_missing_platform_denied': True, 'live_calls': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host-source', type=Path, required=True)
    parser.add_argument('--plugin-source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--temp-dir', type=Path)
    asyncio.run(verify(parser.parse_args()))
