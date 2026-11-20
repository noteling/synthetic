"""A slow authentication status check must not consume the reasoning timeout."""

from pathlib import Path
import sys
import tempfile
import unittest

from synthetics.codexbrain import CodexBrain


class LoginPreflightTests(unittest.TestCase):
    def test_reasoning_timeout_starts_after_login_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / 'codex-fixture'
            executable.write_text('#!' + sys.executable + '\nimport json,sys,time\n'
                                  'if sys.argv[1:]==["login","status"]:\n'
                                  ' time.sleep(0.3);print("Logged in using ChatGPT");raise SystemExit(0)\n'
                                  'sys.stdin.read()\n'
                                  'print(json.dumps({"type":"item.completed","item":'
                                  '{"type":"agent_message","text":"Completed work."}}))\n'
                                  'print(json.dumps({"type":"turn.completed","usage":{}}))\n')
            executable.chmod(0o700)
            result = CodexBrain(root/'employee', executable=str(executable), timeout=0.2).run({})
            self.assertEqual(result.summary, 'Completed work.')


if __name__ == '__main__':
    unittest.main()
