"""Deterministic allowlisted source artifact, real SDK discovery/lifecycle in isolation."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = "octmicy_mai-gatekeeper"
FILES = ("__init__.py", "plugin.py", "config.py", "store.py", "ob_client.py",
         "_manifest.json", "LICENSE", "NOTICE.md", "README.md", "CHANGELOG.md",
         "config.example.toml", "mai.png")


def verify(output=None):
    hashes = {}
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Missing/unsafe source member: " + name)
            data = path.read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(DIRECTORY + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    artifact = content.getvalue()
    with TemporaryDirectory(prefix="gatekeeper-package-") as directory:
        extracted = Path(directory) / "unpacked"
        with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
            assert set(archive.namelist()) == {DIRECTORY + "/" + name for name in FILES}
            archive.extractall(extracted)
        root = extracted / DIRECTORY
        for name, digest in hashes.items():
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
        program = r'''import asyncio,importlib.util,json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve()
def guard(event,args):
 if event!='socket.connect':return
 caller=sys._getframe(1)
 if caller.f_code.co_name=='_fallback_socketpair' and caller.f_globals.get('__name__')=='socket' and caller.f_locals.get('csock') is args[0]:return
 raise RuntimeError('Isolated package: external network forbidden')
sys.addaudithook(guard)
spec=importlib.util.spec_from_file_location('isolated_gatekeeper',root/'__init__.py',submodule_search_locations=[str(root)])
package=importlib.util.module_from_spec(spec);sys.modules[spec.name]=package;spec.loader.exec_module(package)
from isolated_gatekeeper.plugin import create_plugin
from maibot_sdk import MaiBotPlugin
from maibot_sdk.context import PluginContext,PluginPaths
plugin=create_plugin();assert isinstance(plugin,MaiBotPlugin)
entries=plugin.get_components();assert len(entries)==6
assert {c['name'] for c in entries}=={'block_user','unblock_user','remove_friend','fr','block','gatekeeper_intercept'}
for c in entries:
 if c['type']=='TOOL':assert c['metadata']['trusted_context']=='neko.tools.context.v1'
defaults=plugin.build_default_config()
assert defaults['block']['allow_remove_friend'] is False
assert defaults['approve']['mode']=='hybrid' and defaults['approve']['confidence_threshold']==0.7
assert defaults['block']['max_duration_hours']==24 and defaults['block']['intercept_enabled'] is True
assert defaults['plugin']['config_version']=='1.0.0'
manifest=json.loads((root/'_manifest.json').read_text(encoding='utf-8'))
assert manifest['id']=='octmicy.mai-gatekeeper' and manifest['version']=='0.6.11'
async def forbidden_rpc(*args,**kwargs):raise AssertionError('No Host/model/QQ RPC permitted during package lifecycle')
paths=PluginPaths(data_dir=pathlib.Path.cwd()/'synthetic-instance'/'data',runtime_dir=pathlib.Path.cwd()/'synthetic-instance'/'runtime')
plugin._set_context(PluginContext(manifest['id'],rpc_call=forbidden_rpc,paths=paths))
plugin.set_plugin_config(defaults)
async def lifecycle():
 await plugin.on_load()
 assert plugin._store is not None and plugin._client is None
 assert plugin._resolve_data_dir().resolve().is_relative_to(pathlib.Path.cwd().resolve())
 await plugin.on_unload()
 assert plugin._store is None and plugin._client is None
asyncio.run(lifecycle())
print('Isolated allowlisted source: actual SDK2.20, six components, synthetic lifecycle; no Host/QQ/LLM')
'''
        subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", program, str(root)],
                       cwd=directory, check=True)
    canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    report = {"version": "0.6.11", "plugin_id": "octmicy.mai-gatekeeper", "directory": DIRECTORY,
              "members": len(FILES), "files": hashes, "source_sha256": hashlib.sha256(canonical).hexdigest(),
              "zip_sha256": hashlib.sha256(artifact).hexdigest(), "deployed": False}
    if output:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "gatekeeper-source.zip").write_bytes(artifact)
        (destination / "source-hashes.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Optional local artifact/evidence directory; never a deployment")
    verify(parser.parse_args().output)
