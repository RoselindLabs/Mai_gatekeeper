import unittest

from Mai_gatekeeper.plugin import create_plugin


class PlatformScopeTests(unittest.TestCase):
    def test_real_sdk_limits_tools_not_inbound_hook(self):
        components = create_plugin().get_components()
        tools = [c for c in components if c['type'] == 'TOOL']
        self.assertEqual({c['name'] for c in tools}, {'block_user', 'unblock_user', 'remove_friend'})
        for tool in tools:
            self.assertIn(tool['metadata']['platforms'], ('qq', ['qq']))
            description = tool['metadata']['description']
            self.assertIn('缺', description)
            self.assertIn('不猜', description)
            if tool['name'] != 'remove_friend':
                for hint in ('目标用户', '当前目标群', '真实message_id', 'Focus', '必须提供'):
                    self.assertIn(hint, description)
                parameters = {p['name']: p for p in tool['metadata']['parameters']}
                self.assertIn('当前目标群', parameters['message_id']['description'])
                self.assertIn('身份完整', parameters['user_id']['description'])
        hook = next(c for c in components if c['name'] == 'gatekeeper_intercept')
        self.assertFalse(hook['metadata'].get('platforms'))
