"""VIS G5: Lookout's project memory page lists its sources, each linked to the thread that shows it.

Before G5 the page said "Sources: N" (projectview.render) and listed none, so a reader could not get from the count
to the captured messages (Codex event-3b0572e9, item 4)."""
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from synthetics import lookout
from synthetics.projects import ProjectStore

VESPER, CODEX = "employee-v", "employee-c"


def fp_source(sender, to, mid, text, home):
    data = "\n".join(["from: %s" % sender, "message-id: %s" % mid, "to: %s" % to,
                      "sent-at: 2026-11-18T10:00:00Z", "", "---", "", text])
    origin = {"role": "other", "direction": "in", "sender": sender, "source": "fieldpost", "thread": None,
              "auth": {"channel": "fieldpost"}, "external_id": mid, "event_id": "event-x" + mid[-4:], "home": home,
              "occurred_at": "2026-11-18T10:00:00Z"}
    return {"kind": "source", "bytes": data, "body": {"origin": origin, "capture": {"mode": "tag"},
                                                      "quote_source": "event"}}


def plain_source(text):
    """A source with no Fieldpost headers (e.g. a promoted excerpt): it still forms a one-message thread."""
    origin = {"role": "human", "direction": "in", "sender": "david", "source": "desktop", "thread": "t-1",
              "auth": {"channel": "desktop"}, "external_id": "x-1", "event_id": "event-plain", "home": VESPER,
              "occurred_at": "2026-11-18T08:00:00Z"}
    return {"kind": "source", "bytes": text, "body": {"origin": origin, "capture": {"mode": "promote"},
                                                      "quote_source": "event"}}


class MemorySourcesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        store = ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")
        self.addCleanup(store.close)
        self.store = store
        # The same Fieldpost message captured by both homes (two source entries, one message, one thread).
        a = store.append([fp_source(CODEX, VESPER, "message-aaaa", "Please build G5", VESPER)], author=VESPER)
        b = store.append([fp_source(CODEX, VESPER, "message-aaaa", "Please build G5", CODEX)], author=CODEX)
        c = store.append([plain_source("David says <b>hello</b>")], author=VESPER)
        d = store.append([fp_source(VESPER, CODEX, "message-cccc", "SECRET-REDACTED-TEXT", VESPER)], author=VESPER)
        self.a, self.b, self.c, self.d = a["ids"][0], b["ids"][0], c["ids"][0], d["ids"][0]
        store.append([{"kind": "considered", "body": {"source": self.c, "reason": "read"}}], author=VESPER)
        store.redact(self.d, reason="private", by="op")

    def get(self, path):
        return lookout.route(str(self.root), path, agents={})

    def section(self):
        code, body = self.get("/p/synthetics/memory")
        self.assertEqual(code, 200)
        self.assertIn("<h2>Sources", body)
        return body.split("<h2>Sources", 1)[1]

    def test_every_source_listed_with_anchor(self):
        sec = self.section()
        for sid in (self.a, self.b, self.c, self.d):
            self.assertIn("id='%s'" % sid, sec)
        self.assertEqual(len(re.findall(r"<tr id='pm-", sec)), 4)  # same N as "Sources: 4" in the view above

    def test_links_open_the_thread_holding_the_source(self):
        sec = self.section()
        links = {}
        for sid in (self.a, self.b, self.c, self.d):
            row = re.search(r"<tr id='%s'>(.*?)</tr>" % sid, sec).group(1)
            href = re.search(r"href='(/p/synthetics/thread/[^']+)'", row).group(1)
            links[sid] = href
        self.assertEqual(links[self.a], links[self.b])  # both captures of message-aaaa → one thread
        code, page = self.get(links[self.a])
        self.assertEqual(code, 200)
        self.assertIn("Please build G5", page)
        code, page = self.get(links[self.c])
        self.assertEqual(code, 200)
        self.assertIn("David says &lt;b&gt;hello&lt;/b&gt;", page)
        self.assertEqual(urllib.parse.unquote(links[self.c].rsplit("/", 1)[1]), "t:t-1")

    def test_row_shows_sender_coverage_and_redaction_without_text(self):
        sec = self.section()
        row_c = re.search(r"<tr id='%s'>(.*?)</tr>" % self.c, sec).group(1)
        row_a = re.search(r"<tr id='%s'>(.*?)</tr>" % self.a, sec).group(1)
        row_d = re.search(r"<tr id='%s'>(.*?)</tr>" % self.d, sec).group(1)
        self.assertIn("david", row_c)
        self.assertIn("human", row_c)
        self.assertIn("considered ×1", row_c)
        self.assertNotIn("not considered", row_c)
        self.assertIn("not considered", row_a)
        self.assertIn(CODEX, row_a)
        self.assertIn("redacted", row_d)
        code, body = self.get("/p/synthetics/memory")
        self.assertNotIn("SECRET-REDACTED-TEXT", body)
        self.assertNotIn("<b>hello</b>", body)

    def test_empty_project_says_none(self):
        (self.root / "projects" / "noteling").mkdir()
        ProjectStore(self.root / "projects" / "noteling" / "memory.sqlite3", "noteling").close()
        code, body = self.get("/p/noteling/memory")
        self.assertEqual(code, 200)
        self.assertIn("<h2>Sources (0)</h2>", body)


if __name__ == "__main__":
    unittest.main()
