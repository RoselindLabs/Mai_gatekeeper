import unittest

from Mai_gatekeeper.plugin import create_plugin


class PlatformScopeTests(unittest.TestCase):
    def test_real_sdk_limits_tools_not_inbound_hook(self):
        components = create_plugin().get_components()
        tools = [c for c in components if c['type'] == 'TOOL']
        self.assertEqual({c['name'] for c in tools}, {'block_user', 'unblock_user', 'remove_friend'})
        for tool in tools:
            self.assertIn(tool['metadata']['platforms'], ('qq', ['qq']))
        hook = next(c for c in components if c['name'] == 'gatekeeper_intercept')
        self.assertFalse(hook['metadata'].get('platforms'))
