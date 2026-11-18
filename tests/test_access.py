import tempfile
import unittest
from pathlib import Path

from synthetics.store import Store


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "employee"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Ellis", "Synthetics maintainer")

    def test_grants_are_canonical_durable_and_in_employee_context(self):
        target = Path(self.temp.name) / "internal-projects"
        grant = self.store.access_grant(target / ".." / "internal-projects", "write", "Create internal projects.", granted_by="david")
        self.assertEqual(grant["path"], str(target.resolve()))
        self.assertEqual(grant["mode"], "write")
        self.assertEqual(grant["granted_by"], "david")
        self.assertFalse(target.exists(), "Declaring access must not create the project itself.")
        with Store(self.home) as later:
            self.assertEqual(later.context()["access_grants"], [grant])
            self.assertEqual(later.access_list(), [grant])

    def test_same_path_and_mode_preserve_identity_but_update_assignment(self):
        path = Path(self.temp.name) / "assigned"
        first = self.store.access_grant(path, "read", "Inspect assigned sources.", granted_by="david")
        repeated = self.store.access_grant(path, "read", "Inspect assigned sources.", granted_by="david")
        self.assertEqual(repeated, first)
        updated = self.store.access_grant(path, "read", "Inspect design and implementation.", granted_by="manager")
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(updated["created_at"], first["created_at"])
        self.assertEqual(updated["purpose"], "Inspect design and implementation.")
        self.assertEqual(updated["granted_by"], "manager")
        writable = self.store.access_grant(path, "write", "Maintain assigned sources.", granted_by="david")
        self.assertNotEqual(writable["id"], first["id"])
        self.assertEqual(len(self.store.access_list()), 2)

    def test_revocation_is_audited_and_regrant_reuses_identity(self):
        grant = self.store.access_grant(Path(self.temp.name) / "internal", "write", "Create internal projects.", granted_by="david")
        revoked = self.store.access_revoke(grant["id"], granted_by="manager")
        self.assertEqual(revoked["revoked_by"], "manager")
        self.assertIsNotNone(revoked["revoked_at"])
        self.assertEqual(self.store.access_list(), [])
        self.assertEqual(self.store.context()["access_grants"], [])
        self.assertEqual(self.store.access_revoke(grant["id"], granted_by="david"), revoked)
        actions = [entry["action"] for entry in self.store.audit_list()]
        self.assertIn("access.granted", actions)
        self.assertIn("access.revoked", actions)
        restored = self.store.access_grant(grant["path"], "write", "Create internal projects.", granted_by="david")
        self.assertEqual(restored["id"], grant["id"])
        self.assertEqual(restored["created_at"], grant["created_at"])
        self.assertIsNone(restored["revoked_at"])
        self.assertIsNone(restored["revoked_by"])

    def test_employee_cannot_grant_or_revoke_its_own_access(self):
        grant = self.store.access_grant(Path(self.temp.name) / "internal", "write", "Create internal projects.", granted_by="david")
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as employee:
            self.assertEqual(employee.access_list(), [grant])
            with self.assertRaisesRegex(ValueError, "Only an operator"):
                employee.access_grant(Path(self.temp.name) / "other", "write", "Self-assigned scope.", granted_by="david")
            with self.assertRaisesRegex(ValueError, "Only an operator"):
                employee.access_revoke(grant["id"], granted_by="david")
        self.assertEqual(self.store.access_list(), [grant])

    def test_invalid_grants_do_not_modify_access(self):
        path = Path(self.temp.name) / "valid"
        for arguments in (("", "write", "Maintain"), (str(path), "execute", "Maintain"),
                          (str(path), "write", ""), (123, "write", "Maintain")):
            with self.assertRaises(ValueError):
                self.store.access_grant(*arguments, granted_by="david")
        with self.assertRaises(ValueError):
            self.store.access_grant(path, "write", "Maintain", granted_by=" ")
        file_path = Path(self.temp.name) / "file.txt"
        file_path.write_text("not a directory")
        with self.assertRaises(ValueError):
            self.store.access_grant(file_path, "read", "Inspect", granted_by="david")
        self.assertEqual(self.store.access_list(), [])

    def test_symlink_grant_uses_actual_directory(self):
        directory = Path(self.temp.name) / "actual"
        directory.mkdir()
        link = Path(self.temp.name) / "alias"
        link.symlink_to(directory, target_is_directory=True)
        grant = self.store.access_grant(link, "read", "Inspect assigned directory.", granted_by="david")
        self.assertEqual(grant["path"], str(directory.resolve()))
        repeated = self.store.access_grant(directory, "read", "Inspect assigned directory.", granted_by="david")
        self.assertEqual(repeated, grant)


if __name__ == "__main__":
    unittest.main()
