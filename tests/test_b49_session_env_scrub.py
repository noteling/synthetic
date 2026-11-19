"""Backlog 49: the suite does not inherit the launching session's identity.

Run from an employee session (or a detached script started from one), CLI tests used to inherit
SYNTHETICS_SESSION_RUN/SYNTHETICS_HOME and fail owner gates; tests/__init__.py now clears them.
Each check runs a child interpreter with the variables set, as a session-launched suite would.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMES = ("SYNTHETICS_SESSION_RUN", "SYNTHETICS_HOME", "XPC_SERVICE_NAME")


class SessionEnvScrubTests(unittest.TestCase):
    def child(self, args, home):
        env = dict(os.environ, SYNTHETICS_SESSION_RUN="run-b49probe", SYNTHETICS_HOME=str(home),
                   XPC_SERVICE_NAME="com.synthetics.worker.b49probe")
        return subprocess.run([sys.executable] + list(args), cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                              timeout=120)

    def test_importing_the_test_package_clears_session_variables(self):
        with tempfile.TemporaryDirectory() as home:
            code = "import os, json, tests; print(json.dumps([os.environ.get(n) for n in %r]))" % (NAMES,)
            result = self.child(["-c", code], home)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), [None, None, None])

    def test_owner_gated_cli_tests_pass_when_launched_from_a_session(self):
        # The three tests that failed under a session environment (memory of run-98fad19a, 11-18).
        for name in ("tests.test_engine.CliTests.test_operator_can_provision_and_revoke_declared_access",
                     "tests.test_evidence.EvidenceTests.test_cli_evidence_cancel_and_override"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as home:
                result = self.child(["-m", "unittest", name], home)
                self.assertEqual(result.returncode, 0, result.stdout[-2000:])


if __name__ == "__main__":
    unittest.main()
