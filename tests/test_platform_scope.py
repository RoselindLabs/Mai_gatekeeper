import unittest

from Mai_gatekeeper.plugin import create_plugin


class PlatformScopeTests(unittest.TestCase):
    def test_real_sdk_limits_tools_not_inbound_hook(self):
        components = create_plugin().get_components()
        tools = [c for c in components if c['type'] == 'TOOL']
        self.assertEqual({c['name'] for c in tools}, {'block_user', 'unblock_user', 'remove_friend'})
        for tool in tools:
            # SDK 2.23 promotes scope to the declaration; 2.20 keeps platforms in metadata.
            scope = tool['metadata'] | tool
            self.assertIn(scope['platforms'], ('qq', ['qq']))
            self.assertEqual(scope['chat_scope'], 'private' if tool['name'] == 'remove_friend' else 'group')
            description = tool['metadata']['description']
            if tool['name'] != 'remove_friend':
                self.assertIn('目标用户', description)
                parameters = {p['name']: p for p in tool['metadata']['parameters']}
                for hint in ('当前目标群', '真实消息ID', 'Focus', '必需', '不猜'):
                    self.assertIn(hint, parameters['message_id']['description'])
                self.assertIn('身份完整', parameters['user_id']['description'])
            else:
                self.assertIn('缺', description)
                self.assertRegex(description, '不(?:要)?猜')
        hook = next(c for c in components if c['name'] == 'gatekeeper_intercept')
        scope = hook['metadata'] | hook
        self.assertEqual(hook['type'], 'HOOK_HANDLER')
        self.assertEqual(hook['metadata']['hook'], 'chat.receive.before_process')
        self.assertIn(scope.get('platforms'), (None, []))
        self.assertIn(scope.get('chat_scope'), (None, 'all'))
        self.assertIn(scope.get('allowed_session'), (None, []))
