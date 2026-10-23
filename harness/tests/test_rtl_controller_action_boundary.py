"""Controller receipt actions never become model workspace capabilities."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    'readback_boundary_bridge', Path(__file__).parents[1] / 'runtime/rtl_bridge.py')
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class ControllerBoundaryTests(unittest.TestCase):
    def test_model_cannot_register_end_or_upload_control_evidence(self):
        for role in ('implementation', 'architecture', 'review', 'rtl_audit'):
            for action in ('rtl_session_begin', 'rtl_session_end', 'put_evidence', 'append_evidence'):
                with self.subTest(role=role, action=action), tempfile.TemporaryDirectory() as folder:
                    context = Path(folder) / 'context.json'
                    context.write_text(json.dumps({'environment': {}, 'run_id': 'fixture',
                                                  'worker': '/fixture/worker.py', 'role': role,
                                                  'scope': 'work'}))
                    remote = Mock()
                    with patch.dict(os.environ, {'HARNESS_RTL_CONTEXT': str(context)}), \
                            patch.object(bridge, 'Remote', return_value=remote), \
                            patch('sys.stdin', io.StringIO(json.dumps({'action': action,
                                                                      'session_id': 'forged'}))):
                        with self.assertRaisesRegex(ValueError, 'unsupported model workspace action'):
                            bridge.main()
                    remote.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
