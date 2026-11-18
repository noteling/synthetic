"""Real temporary launchd job regression; never uses a live employee or model."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from synthetics.service import bootstrap_launch_agent, write_launch_agent
from synthetics.telegram_cli import _start_if_needed


def _test_label(kind):
    """com.synthetics.test.<kind>.p<pid>.<hex>: the owning test process is recorded in the label, so a later run
    can boot out a label whose run was killed (SIGKILL or a session timeout) before its `finally` while the
    temporary plist still exists (M28 r2 N2: the plist-gone rule alone missed that case)."""
    return "com.synthetics.test.%s.p%d.%s" % (kind, os.getpid(), uuid.uuid4().hex)


def _owner_dead(label):
    """True when LABEL names an owning test pid (see _test_label) that is no longer running."""
    part = label.split(".")[-2] if label.count(".") >= 4 else ""
    if not (part.startswith("p") and part[1:].isdigit()):
        return False
    pid = int(part[1:])
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _boot_out_orphaned_test_labels(prefix="com.synthetics.test."):
    """Remove test launchd labels left by an earlier run killed before its `finally` (M26 r2 N1).

    Removed: labels whose plist no longer exists (their TemporaryDirectory is gone), and labels whose owning
    test pid (_test_label) is dead. A live test, e.g. a concurrent rerun, owns neither kind."""
    if sys.platform != "darwin":
        return []
    listed = subprocess.run(["/bin/launchctl", "list"], capture_output=True, text=True).stdout
    removed = []
    for line in listed.splitlines():
        label = line.split("\t")[-1].strip()
        if not label.startswith(prefix):
            continue
        target = "gui/%d/%s" % (os.getuid(), label)
        info = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True).stdout
        paths = [l.split("=", 1)[1].strip() for l in info.splitlines() if l.strip().startswith("path =")]
        if (paths and not Path(paths[0]).exists()) or _owner_dead(label):
            subprocess.run(["/bin/launchctl", "bootout", target], capture_output=True)
            removed.append(label)
    return removed


def _ensure_gone(label):
    """Boot LABEL out and confirm launchd no longer has it (M28 P1b: the m19r5 leak). Runs even when
    bootstrap raised after loading, tolerates a bootout that races a relaunch, and retries briefly."""
    if sys.platform != "darwin":
        return
    target = "gui/%d/%s" % (os.getuid(), label)
    for _ in range(10):
        subprocess.run(["/bin/launchctl", "bootout", target], capture_output=True)
        if subprocess.run(["/bin/launchctl", "print", target], capture_output=True).returncode != 0:
            return
        time.sleep(0.5)
    raise AssertionError("test label %s is still loaded after bootout" % label)


def setUpModule():
    _boot_out_orphaned_test_labels()


class TestLabelOwnerTests(unittest.TestCase):
    def test_owner_dead_reads_the_pid_from_the_label(self):
        self.assertTrue(_owner_dead("com.synthetics.test.m19r5.p999999.%s" % uuid.uuid4().hex))  # pids < 99999
        self.assertFalse(_owner_dead(_test_label("m19r5")))  # this process
        self.assertFalse(_owner_dead("com.synthetics.test.m19r5." + uuid.uuid4().hex))  # legacy: plist rule only


def tearDownModule():
    # Last line of defence: every test label whose temporary plist is gone is booted out before the run ends.
    _boot_out_orphaned_test_labels()


@unittest.skipUnless(sys.platform == "darwin", "macOS launchd integration")
class LoadedJobRecoveryTests(unittest.TestCase):
    def test_explicit_start_reruns_a_loaded_job_after_its_process_has_exited(self):
        with tempfile.TemporaryDirectory(prefix="synthetics-service-recovery-") as temporary:
            root = Path(temporary)
            marker = root / "starts.txt"
            label = _test_label("recovery")
            command = [sys.executable, "-c", "from pathlib import Path; import sys; "
                       "p=Path(sys.argv[1]); p.open('a').write('started\\n')", str(marker)]
            plist = write_launch_agent(label, command, root, root / "logs", root / "agents", keep_alive=False)
            target = "gui/%s/%s" % (os.getuid(), label)
            loaded = False
            try:
                bootstrap_launch_agent(plist)
                loaded = True
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True, check=True)
                    if marker.exists() and "state = not running" in status.stdout:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Temporary job did not reach its loaded-but-stopped state")
                self.assertEqual(marker.read_text().splitlines(), ["started"])
                _start_if_needed(plist)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if len(marker.read_text().splitlines()) == 2:
                        break
                    time.sleep(0.05)
                self.assertEqual(marker.read_text().splitlines(), ["started", "started"],
                                 "An explicitly started loaded-but-stopped job must run again")
            finally:
                _ensure_gone(label)

    def test_job_can_request_its_own_restart_through_launchd(self):
        with tempfile.TemporaryDirectory(prefix="synthetics-self-restart-") as temporary:
            root = Path(temporary)
            marker = root / "starts.txt"
            label = _test_label("selfrestart")
            script = root / "job.py"
            script.write_text(
                "from pathlib import Path\nimport sys,time\n"
                "from synthetics.service import restart_launch_agent\n"
                "p=Path(sys.argv[1])\n"
                "first=not p.exists()\n"
                "with p.open('a') as out:\n out.write('started\\n');out.flush()\n"
                "if first:\n restart_launch_agent(sys.argv[2])\n"
                "time.sleep(30)\n")
            package = str(Path(__file__).resolve().parent.parent)
            plist = write_launch_agent(label, [sys.executable, str(script), str(marker), label], root,
                                        root / "logs", root / "agents", keep_alive=False,
                                        environment={"PYTHONPATH": package})
            loaded = False
            try:
                bootstrap_launch_agent(plist)
                loaded = True
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if marker.exists() and len(marker.read_text().splitlines()) >= 2:
                        break
                    time.sleep(0.05)
                self.assertEqual(marker.read_text().splitlines() if marker.exists() else [],
                                 ["started", "started"], "launchd must restart the job even when its requesting client is killed")
                # No KeepAlive or automatic model retry policy was enabled.
                import plistlib
                self.assertFalse(plistlib.loads(plist.read_bytes())["KeepAlive"])
            finally:
                _ensure_gone(label)

    def test_kickstart_relaunches_a_clean_exit_job_under_successful_exit_false(self):
        """M19 R5: serve exits 0 on SIGTERM; KeepAlive{SuccessfulExit:false} alone would leave it
        stopped, so the self-restart depends on kickstart -k relaunching it. Uses request_restart,
        whose launchctl runs in its own session so launchd's process-group cleanup cannot reap it."""
        _boot_out_orphaned_test_labels()
        with tempfile.TemporaryDirectory(prefix="synthetics-m19-r5-") as temporary:
            root = Path(temporary)
            marker = root / "starts.txt"
            label = _test_label("m19r5")
            script = root / "job.py"
            script.write_text(
                "from pathlib import Path\nimport os,signal,sys,time\n"
                "from synthetics.service import request_restart\n"
                "p=Path(sys.argv[1])\n"
                "first=not p.exists()\n"
                "def term(*_):\n with p.open('a') as out: out.write('sigterm-exit-0\\n')\n os._exit(0)\n"
                "signal.signal(signal.SIGTERM, term)\n"
                "with p.open('a') as out:\n out.write('started %s\\n' % os.environ.get('XPC_SERVICE_NAME'))\n"
                "with open(sys.argv[1]+'.times','a') as out: out.write('%.2f\\n' % time.time())\n"
                "if first:\n request_restart(os.environ['XPC_SERVICE_NAME'])\n"
                "time.sleep(30)\n")
            package = str(Path(__file__).resolve().parent.parent)
            plist = write_launch_agent(label, [sys.executable, str(script), str(marker)], root,
                                        root / "logs", root / "agents", keep_alive="on-failure",
                                        environment={"PYTHONPATH": package})
            loaded = False
            try:
                bootstrap_launch_agent(plist)
                loaded = True
                deadline = time.monotonic() + 45  # ThrottleInterval 30 may delay the relaunch
                while time.monotonic() < deadline:
                    if marker.exists() and len(marker.read_text().splitlines()) >= 3:
                        break
                    time.sleep(0.05)
                lines = marker.read_text().splitlines() if marker.exists() else []
                times = Path(str(marker) + ".times")
                print("M19 R5 start times:", times.read_text().split() if times.exists() else None)
                self.assertEqual(lines, ["started " + label, "sigterm-exit-0", "started " + label],
                                 "kickstart -k must relaunch a job that exits 0 under SuccessfulExit:false")
                import plistlib
                self.assertEqual(plistlib.loads(plist.read_bytes())["KeepAlive"], {"SuccessfulExit": False})
            finally:
                _ensure_gone(label)


if __name__ == "__main__":
    unittest.main()
