"""PUB-1: the shipped package carries no host-specific paths or defaults."""
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from synthetics import brain, cli

ROOT = Path(__file__).resolve().parents[1]
PROMPT = ROOT / "synthetics" / "prompts" / "employee.md"
HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s]+/")


class PublishHygieneTest(unittest.TestCase):
    def test_shipped_prompt_has_no_host_paths(self):
        self.assertIsNone(HOME_PATH.search(PROMPT.read_text()))

    def test_host_rules_come_from_the_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"  # company root = tmp, which has no company log
            home.mkdir()
            plain = brain.system_prompt(home)
            self.assertEqual(plain, PROMPT.read_text())
            self.assertNotIn("Host rules", plain)
            (home / brain.HOST_RULES).write_text("   \n")
            self.assertEqual(brain.system_prompt(home), plain)
            (home / brain.HOST_RULES).write_text("- Do not modify /srv/protected/checkout.\n")
            ruled = brain.system_prompt(home)
            self.assertTrue(ruled.startswith(plain))
            self.assertIn("Host rules (host-assigned; prompt.local.md in your home):", ruled)
            self.assertIn("- Do not modify /srv/protected/checkout.", ruled)
            command = brain.ClaudeBrain(home).command()
            self.assertEqual(command[command.index("--system-prompt") + 1], ruled)

    def test_prompt_names_no_invented_approver(self):
        # PUB-1 G2: protection wording cites existing grants or host rules, never "operator" permission.
        text = PROMPT.read_text()
        self.assertNotIn("operator grant", text)
        self.assertIn("unless an existing explicit write grant in your access_grants covers it", text)
        self.assertNotIn("set by the operator", Path(brain.__file__).read_text())

    def test_default_home_is_not_an_employee_name(self):
        env = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_HOME"}
        with patch.dict(os.environ, env, clear=True):
            args = cli.parser().parse_args(["status"])
        self.assertEqual(Path(args.home), Path.home() / ".synthetics" / "default")

    def test_gitignore_covers_state_caches_and_host_rules(self):
        lines = (ROOT / ".gitignore").read_text().split()
        for entry in (".synthetics/", ".pytest_cache/", "__pycache__/", ".env", "prompt.local.md",
                      "/experiments/", "/docs/proposals/project-memory.md"):  # G1: private originals
            self.assertIn(entry, lines)


if __name__ == "__main__":
    unittest.main()
