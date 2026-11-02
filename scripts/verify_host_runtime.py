"""Actual Host Runner, trusted routing and GIF admission in a synthetic instance."""
import argparse
import asyncio
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import sys


async def verify(args):
    root = Path(__file__).resolve().parents[1]
    host, gif = args.host_source.resolve(), args.gif_source.resolve()
    with ExitStack() as cleanup:
        instance = Path(cleanup.enter_context(TemporaryDirectory(prefix='gatekeeper-runner-', dir=args.temp_dir))).resolve()
        previous = Path.cwd(); os.chdir(instance); cleanup.callback(os.chdir, previous)
        os.environ.update(MAIBOT_INSTANCE_DIR=str(instance), MAIBOT_LOCALE='zh-CN', MAIBOT_HOST_VERSION='1.3.19')
        sys.path[:0] = [str(host/'scripts'), str(host)]
        import neko_pytest_guard  # noqa: F401
        from src.plugin_runtime.runner.runner_main import PluginRunner, PluginActivationStatus
        from src.plugin_runtime.runner import runner_main
        from src.plugin_runtime.protocol.envelope import Envelope
        from src.plugin_runtime.component_query import ComponentQueryService
        from src.plugin_runtime.host.component_registry import HookHandlerEntry
        from src.plugin_runtime.host.hook_dispatcher import HookDispatcher
        from src.plugin_runtime.host.hook_spec_registry import HookSpec
        from src.common.database.database import engine
        from src.common.logger import close_handlers
        cleanup.callback(engine.dispose); cleanup.callback(close_handlers)
        assert runner_main._PROJECT_ROOT.resolve() == instance
        target = instance/'plugins/octmicy_mai-gatekeeper'; target.mkdir(parents=True)
        for name in ('plugin.py','_manifest.json','config.py','store.py','ob_client.py','__init__.py'):
            shutil.copyfile(root/name, target/name)
        (target/'config.toml').write_text('[plugin]\nenabled=true\nconfig_version="1.0.0"\n[block]\nnotify_blocked_user=false\n',encoding='utf-8')
        runner = PluginRunner(host_address='unix:///unused-test.sock',session_token='synthetic',plugin_dirs=[str(target.parent)])
        registered=[]
        source={'message_id':'m','session_id':'s','platform':'qq','account_id':'bot',
                'message_info':{'group_info':{'group_id':'g'},'user_info':{'user_id':'target','user_nickname':'synthetic'}}}
        async def rpc(method,**kwargs):
            if method=='cap.call':
                payload=kwargs['payload']; name=payload['capability']
                assert name in ('message.get_by_id','config.get'), 'Unexpected model/QQ/send capability'
                result={'success':True,'value':[]} if name=='config.get' else {'success':True,'message':source}
                return SimpleNamespace(error=None,payload={'result':result})
            assert method in ('plugin.bootstrap','plugin.register_components','plugin.unregister')
            if method=='plugin.register_components':registered.extend(kwargs['payload']['components'])
            return SimpleNamespace(error=None,payload={'accepted':True})
        runner._rpc_client.send_request=AsyncMock(side_effect=rpc)
        metas=runner._loader.discover_and_load([str(target.parent)])
        assert len(metas)==1 and not runner._loader.failed_plugins
        meta=metas[0];assert runner._prepare_plugin_activation(meta) is None and not meta.config_error
        assert await runner._activate_plugin(meta)==PluginActivationStatus.LOADED
        try:
            assert len(registered)==6
            async def forbidden(*args,**kwargs):raise AssertionError('QQ actions forbidden')
            meta.instance._client=SimpleNamespace(connected=True,self_id='bot',stop=AsyncMock(),call_action=forbidden)
            tool=next(c for c in registered if c['name']=='block_user')
            entry=SimpleNamespace(invoke_method='plugin.invoke_tool',metadata=tool['metadata'])
            context=SimpleNamespace(session_id='s',stream_id='s',platform='qq',group_id='g',user_id='',metadata={})
            invocation=SimpleNamespace(arguments={'message_id':'m','_host_context':{'account_id':'forged'}})
            payload=ComponentQueryService._build_tool_invocation_payload(entry,invocation,context)
            assert payload['_host_context']['account_id']==''
            async def invoke_tool(arguments):
                response=await runner._handle_invoke(Envelope(request_id=1,message_type='request',
                    method='plugin.invoke_tool',plugin_id='octmicy.mai-gatekeeper',
                    payload={'component_name':'block_user','args':arguments}))
                assert response.error is None
                return response.payload['result']
            assert (await invoke_tool(payload))['success']
            source['account_id']='foreign'
            assert not (await invoke_tool(payload))['success']
            source['account_id']='bot'
            spec=importlib.util.spec_from_file_location('coordinated_gif',gif/'plugin.py',submodule_search_locations=[str(gif)])
            module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
            gif_plugin=module.create_plugin();gif_plugin.set_plugin_config({})
            gif_calls=[]
            async def fake_process(message,**kwargs):gif_calls.append(True);return None
            gif_plugin._processor=SimpleNamespace(process_message=fake_process)
            gate_hook=next(c for c in registered if c['name']=='gatekeeper_intercept')
            gif_hook=next(c for c in gif_plugin.get_components() if c['name']=='animation_frame_sheet')
            entries=[HookHandlerEntry(c['name'],'HOOK_HANDLER',pid,c['metadata']) for pid,c in (
                ('github.fabiocsa.read-gif',gif_hook),('octmicy.mai-gatekeeper',gate_hook))]
            supervisor=SimpleNamespace(group_name='third_party',component_registry=SimpleNamespace(get_hook_handlers=lambda name:entries))
            async def dispatch(method,pid,name,arguments,**kwargs):
                raw=await (gif_plugin.handle_before_process(**arguments) if pid=='github.fabiocsa.read-gif'
                           else meta.instance.intercept_blocked_message(**arguments))
                return SimpleNamespace(payload={'success':True,**(raw or {})})
            supervisor.invoke_plugin=dispatch
            dispatcher=HookDispatcher()
            dispatcher.register_hook_spec(HookSpec(name='chat.receive.before_process',parameters_schema={},allow_abort=True))
            blocked={'platform':'qq','message_info':{'group_info':{'group_id':'g'},'user_info':{'user_id':'target'}}}
            result=await dispatcher.invoke_hook('chat.receive.before_process',supervisors=[supervisor],message=blocked)
            assert result.aborted and not gif_calls and not result.errors
            blocked['message_info']['user_info']['user_id']='allowed'
            result=await dispatcher.invoke_hook('chat.receive.before_process',supervisors=[supervisor],message=blocked)
            assert not result.aborted and len(gif_calls)==1 and not result.errors
            await dispatcher.stop()
        finally:
            await runner._invoke_plugin_on_unload(meta)
        print(json.dumps({'real_host_runner':True,'trusted_focus_scoped_account_resolution':True,
                          'wrong_account_denied':True,'actual_dispatcher_blocks_before_GIF':True,
                          'allowed_message_reaches_GIF':True,'component_count':6,'live_model_calls':0,'QQ_actions':0}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--host-source',type=Path,required=True)
    parser.add_argument('--gif-source',type=Path,required=True);parser.add_argument('--temp-dir',type=Path)
    asyncio.run(verify(parser.parse_args()))
