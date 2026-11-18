"""M25 test 15 (C7): production code names no human and no provider channel prefix.

Scope is synthetics/*.py only. The Telegram adapter modules (telegram*.py) are the provider and are exempt;
test fixtures are exempt by scope."""
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "synthetics"
FORBIDDEN = re.compile(r"""["']david["']|telegram:|codex-desktop:""")


class NoBuiltinHumansTest(unittest.TestCase):
    def test_grep(self):
        hits = []
        for path in sorted(PACKAGE.glob("*.py")):
            if path.name.startswith("telegram"):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if FORBIDDEN.search(line):
                    hits.append("%s:%d: %s" % (path.name, number, line.strip()))
        self.assertEqual(hits, [], "\n".join(hits))

    def test_scope_is_not_empty(self):
        self.assertGreater(len(list(PACKAGE.glob("*.py"))), 5)


if __name__ == "__main__":
    unittest.main()
