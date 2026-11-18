import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from synthetics.service import (MANAGED_MARKER, bootstrap_launch_agent, remove_launch_agent,
                                restart_launch_agent, start_launch_agent, stop_launch_agent, write_launch_agent)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetics service ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.logs = self.root / "private logs"
        self.agents = self.root / "launch agents"
        self.label = "com.synthetics.telegram"
        self.argv = ["/usr/bin/python3", "-m", "synthetics", "--home", str(self.root / "employee home"), "serve"]

    def write(self, **kwargs):
        arguments = dict(label=self.label, argv=self.argv, working_directory=self.root,
                         logs_directory=self.logs, launch_agents_dir=self.agents)
        arguments.update(kwargs)
        return write_launch_agent(**arguments)

    def test_private_worker_plist_preserves_argument_and_path_boundaries(self):
        environment = {"PATH": "/custom binaries:/usr/bin:/bin", "PYTHONPATH": str(self.root), "HOME": str(Path.home())}
        path = self.write(environment=environment)
        configuration = plistlib.loads(path.read_bytes())
        self.assertEqual(configuration["ProgramArguments"], self.argv)
        self.assertEqual(configuration["WorkingDirectory"], str(self.root))
        self.assertEqual(configuration["EnvironmentVariables"], environment)
        self.assertEqual(configuration["StandardOutPath"], str(self.logs / (self.label + ".stdout.log")))
        self.assertEqual(configuration["StandardErrorPath"], str(self.logs / (self.label + ".stderr.log")))
        self.assertTrue(configuration["RunAtLoad"])
        self.assertFalse(configuration["KeepAlive"])
        self.assertNotIn("ThrottleInterval", configuration)
        self.assertEqual(configuration["Umask"], 0o077)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.logs.stat().st_mode & 0o777, 0o700)
        self.assertIn(MANAGED_MARKER, path.read_bytes())

    def test_transport_restart_is_throttled_and_environment_does_not_copy_secrets(self):
        with patch.dict(os.environ, {"PRIVATE_TOKEN": "must not inherit"}):
            configuration = plistlib.loads(self.write(keep_alive=True).read_bytes())
        self.assertTrue(configuration["KeepAlive"])
        self.assertEqual(configuration["ThrottleInterval"], 30)
        self.assertEqual(configuration["EnvironmentVariables"], {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})

    def test_invalid_input_has_no_filesystem_side_effects(self):
        cases = [dict(label="com.apple.other"), dict(label="com.synthetics../other"), dict(label="com.synthetics."),
                 dict(argv="/usr/bin/python3 -m synthetics"), dict(argv=[]), dict(argv=["python3"]),
                 dict(argv=["/usr/bin/python3", ""]), dict(argv=["/usr/bin/python3", "bad\x00arg"]),
                 dict(working_directory="relative"), dict(working_directory=self.root / "missing"),
                 dict(logs_directory="relative"), dict(logs_directory="/"), dict(launch_agents_dir="relative"),
                 dict(keep_alive=1), dict(environment=[]), dict(environment={"TOKEN": None}),
                 dict(environment={"BAD=NAME": "value"})]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.write(**kwargs)
            self.assertFalse(self.logs.exists())
            self.assertFalse(self.agents.exists())

    def test_only_same_managed_label_can_be_updated(self):
        path = self.write()
        old_data = path.read_bytes()
        replacement = self.argv + ["--poll", "5"]
        self.assertEqual(self.write(argv=replacement), path)
        self.assertNotEqual(path.read_bytes(), old_data)
        self.assertEqual(plistlib.loads(path.read_bytes())["ProgramArguments"], replacement)
        unrelated = plistlib.dumps({"Label": self.label, "ProgramArguments": ["/bin/echo", "unrelated"]})
        path.write_bytes(unrelated)
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual(path.read_bytes(), unrelated)

    def test_symlink_and_invalid_plist_are_not_overwritten(self):
        self.agents.mkdir()
        path = self.agents / (self.label + ".plist")
        other = self.root / "unrelated"
        other.write_text("must remain")
        path.symlink_to(other)
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertTrue(path.is_symlink())
        self.assertEqual(other.read_text(), "must remain")
        path.unlink()
        path.write_text("not a plist")
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual(path.read_text(), "not a plist")
        self.assertFalse(self.logs.exists())

    def test_failed_atomic_replacement_keeps_old_plist_and_cleans_temporary(self):
        path = self.write()
        previous = path.read_bytes()
        with patch("synthetics.service.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.write(argv=self.argv + ["--poll", "5"])
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(self.agents.iterdir()), [path])

    def test_bootstrap_uses_argument_array_scoped_to_current_uid(self):
        path = self.write()
        with patch("synthetics.service.sys.platform", "darwin"), patch("synthetics.service.os.getuid", return_value=os.getuid()), \
                patch("synthetics.service.subprocess.run") as run:
            result = bootstrap_launch_agent(path)
        domain = "gui/%s" % os.getuid()
        run.assert_called_once_with(["/bin/launchctl", "bootstrap", domain, str(path)], check=True,
                                    capture_output=True, text=True)
        self.assertEqual(result, dict(label=self.label, domain=domain, plist=str(path)))

    def test_stop_only_targets_valid_synthetics_label_in_current_uid(self):
        with patch("synthetics.service.sys.platform", "darwin"), patch("synthetics.service.os.getuid", return_value=1234), \
                patch("synthetics.service.subprocess.run") as run:
            result = stop_launch_agent(self.label)
            with self.assertRaises(ValueError):
                stop_launch_agent("com.apple.finder")
        run.assert_called_once_with(["/bin/launchctl", "bootout", "gui/1234/" + self.label], check=True,
                                    capture_output=True, text=True)
        self.assertTrue(result["stopped"])

    def test_remove_verifies_ownership_and_retains_plist_on_stop_failure(self):
        path = self.write()
        with patch("synthetics.service.sys.platform", "darwin"), \
                patch("synthetics.service.subprocess.run", side_effect=subprocess.CalledProcessError(5, ["launchctl"])):
            with self.assertRaises(subprocess.CalledProcessError):
                remove_launch_agent(self.label, self.agents)
        self.assertTrue(path.exists())
        with patch("synthetics.service.subprocess.run") as run:
            self.assertTrue(remove_launch_agent(self.label, self.agents, stop=False))
            run.assert_not_called()
            self.assertFalse(remove_launch_agent(self.label, self.agents, stop=False))
        path.write_bytes(plistlib.dumps({"Label": self.label}))
        with self.assertRaises(FileExistsError):
            remove_launch_agent(self.label, self.agents, stop=False)
        self.assertTrue(path.exists())

    def test_non_macos_start_does_not_run_any_command(self):
        path = self.write()
        with patch("synthetics.service.sys.platform", "linux"), patch("synthetics.service.subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                bootstrap_launch_agent(path)
        run.assert_not_called()

    def test_start_and_restart_use_distinct_scoped_launchd_requests(self):
        with patch("synthetics.service.sys.platform", "darwin"), patch("synthetics.service.os.getuid", return_value=1234), \
                patch("synthetics.service.subprocess.run") as run:
            started = start_launch_agent(self.label)
            restarted = restart_launch_agent(self.label)
            with self.assertRaises(ValueError):
                restart_launch_agent("com.apple.finder")
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["/bin/launchctl", "kickstart", "gui/1234/" + self.label],
            ["/bin/launchctl", "kickstart", "-k", "gui/1234/" + self.label],
        ])
        self.assertTrue(started["start_requested"])
        self.assertTrue(restarted["restart_requested"])

    def test_restart_failure_is_not_reported_as_success(self):
        with patch("synthetics.service.sys.platform", "darwin"), \
                patch("synthetics.service.subprocess.run", side_effect=subprocess.CalledProcessError(3, ["launchctl"])):
            with self.assertRaises(subprocess.CalledProcessError):
                restart_launch_agent(self.label)


if __name__ == "__main__":
    unittest.main()
