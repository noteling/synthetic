"""Backlog 48: test launchd labels are classified by owner, and a label this run leaves loaded fails the run."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from synthetics.service import bootstrap_launch_agent, write_launch_agent
from tests import test_service_recovery as recovery


class LabelOwnerTests(unittest.TestCase):
    def test_classify_names_own_live_dead_and_legacy(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            hexpart = uuid.uuid4().hex
            self.assertEqual(recovery._classify(recovery._test_label("m19r5")), "own")
            self.assertEqual(recovery._classify("com.synthetics.test.m19r5.p%d.%s" % (sleeper.pid, hexpart)),
                             "live pid %d" % sleeper.pid)
            self.assertEqual(recovery._classify("com.synthetics.test.m19r5.p999999.%s" % hexpart), "dead pid 999999")
            self.assertEqual(recovery._classify("com.synthetics.test.m19r5." + hexpart), "legacy")
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_labels_command_lists_owners(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "-m", "tests.test_service_recovery", "labels"], cwd=str(root),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        lines = result.stdout.splitlines()
        self.assertTrue(lines, "labels prints at least one line")
        if lines != ["no test labels"]:
            for line in lines:
                label, owner = line.split("\t")
                self.assertTrue(label.startswith("com.synthetics.test."), line)
                # the child is not the owner of anything this process loaded
                self.assertTrue(owner == "legacy" or owner.startswith(("live pid ", "dead pid ", "own")), line)


@unittest.skipUnless(sys.platform == "darwin", "macOS launchd integration")
class OwnLeakTests(unittest.TestCase):
    def test_teardown_fails_and_boots_out_a_label_this_run_left_loaded(self):
        with tempfile.TemporaryDirectory(prefix="synthetics-b48-") as temporary:
            root = Path(temporary)
            label = recovery._test_label("b48")
            plist = write_launch_agent(label, ["/usr/bin/true"], root, root / "logs", root / "agents",
                                       keep_alive=False)
            target = "gui/%d/%s" % (os.getuid(), label)
            try:
                bootstrap_launch_agent(plist)
                with self.assertRaises(AssertionError) as caught:
                    recovery.tearDownModule()
                self.assertIn(label, str(caught.exception))
                self.assertNotEqual(subprocess.run(["/bin/launchctl", "print", target],
                                                   capture_output=True).returncode, 0,
                                    "the leaked label is booted out before the failure is raised")
            finally:
                recovery._ensure_gone(label)


if __name__ == "__main__":
    unittest.main()
