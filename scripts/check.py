"""Offline actual-SDK tests. All external network connections fail closed."""
import asyncio
import importlib.util
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("Mai_gatekeeper", ROOT / "__init__.py",
                                          submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def guard(event, args):
    if event != "socket.connect":
        return
    caller = sys._getframe(1)
    if (caller.f_code.co_name == "_fallback_socketpair" and caller.f_globals.get("__name__") == "socket"
            and caller.f_locals.get("csock") is args[0]):
        return
    raise RuntimeError("Offline gate: network/model/QQ connection forbidden")


sys.addaudithook(guard)


async def no_connection(self):
    # Exercise start/restart/stop lifecycle, but never create even a loopback WS.
    await asyncio.Event().wait()


if __name__ == "__main__":
    upstream = runpy.run_path(str(ROOT / "tests/run_self_test.py"), run_name="upstream_tests")
    from Mai_gatekeeper.ob_client import OneBotClient
    with patch.object(OneBotClient, "_connection_loop", no_connection):
        status = asyncio.run(upstream["main"]())
    assert len(upstream["RESULTS"]) >= 105, "Upstream safety coverage must not shrink"
    sys.path.insert(0, str(ROOT / "tests"))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(bool(status) or not result.wasSuccessful())
