"""Satchel P1 r3 (Rowan's #107 round-2 finding A, Codex ruling): under a secret-named JSON key every non-empty
string and number leaf is masked, however deep in lists and dicts. Plain values that no pattern catches are used,
so only the key-name rule can mask them. Synthetic fixtures only."""

import json
import secrets
import unittest

from tests.test_satchel_p1 import satchel, tag
from tests.test_satchel_r2 import Base

ASSIGN = r"\A«masked:assign:\d+»\Z"


def plain():
    return "plain" + tag(6)


class SecretTreeA(Base):
    def config(self, value):
        (self.home / "glance.json").write_text(json.dumps(value, indent=1))
        result = self.extract()
        return result, json.loads(self.members(result["archive"])["config/glance.json"])

    def paths(self, result):
        manifest = json.loads(self.members(result["archive"])["manifest.json"])
        return {m.get("json_path") for m in manifest["masks"] if m["member"] == "config/glance.json"}

    def test_list_under_token(self):
        a, b = plain(), plain()
        result, archived = self.config({"token": [a, b]})
        self.assert_clean(result, [a, b])
        for value in archived["token"]:
            self.assertRegex(value, ASSIGN)
        self.assertTrue({"$.token[0]", "$.token[1]"} <= self.paths(result))

    def test_dict_under_password(self):
        a = plain()
        result, archived = self.config({"password": {"value": a}})
        self.assert_clean(result, [a])
        self.assertRegex(archived["password"]["value"], ASSIGN)
        self.assertIn("$.password.value", self.paths(result))

    def test_dict_under_api_key(self):
        a, b = plain(), plain()
        result, archived = self.config({"api_key": {"prod": a, "dev": b}})
        self.assert_clean(result, [a, b])
        self.assertRegex(archived["api_key"]["prod"], ASSIGN)
        self.assertRegex(archived["api_key"]["dev"], ASSIGN)

    def test_list_of_lists_under_client_secret(self):
        a, num = plain(), secrets.randbelow(10 ** 9) + 10 ** 9
        result, archived = self.config({"client_secret": [[a, num]]})
        self.assert_clean(result, [a, str(num)])
        self.assertRegex(archived["client_secret"][0][0], ASSIGN)
        self.assertRegex(archived["client_secret"][0][1], ASSIGN)
        self.assertIn("$.client_secret[0][0]", self.paths(result))

    def test_deep_mixed_keeps_bool_null_and_empty(self):
        a = plain()
        result, archived = self.config({"auth": {"Authorization": [{"h": [a], "on": True, "x": None, "e": ""}]},
                                        "label": "keep me", "port": 8765})
        self.assert_clean(result, [a])
        inner = archived["auth"]["Authorization"][0]
        self.assertRegex(inner["h"][0], ASSIGN)
        self.assertEqual((inner["on"], inner["x"], inner["e"]), (True, None, ""))
        self.assertEqual((archived["label"], archived["port"]), ("keep me", 8765))

    def test_masker_value_directly(self):
        a = plain()
        masker = satchel.Masker()
        out, changed = masker.value({"token": [{"k": [a]}], "tokens": [7]}, {"member": "x"})
        self.assertTrue(changed)
        self.assertRegex(out["token"][0]["k"][0], ASSIGN)
        self.assertEqual(out["tokens"], [7])
        self.assertNotIn(a, json.dumps(out))


if __name__ == "__main__":
    unittest.main()
