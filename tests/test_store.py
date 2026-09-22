import multiprocessing
from pathlib import Path
import stat
import tempfile
import unittest

from agentbridge.store import Store, redact_summary


def _open_worker(project):
    # Concurrent first opens race on the WAL switch; each must still succeed.
    with Store(project) as store:
        assert store.namespace


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.project)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_same_named_directories_get_different_namespaces(self):
        sibling = self.root / "other" / "project"
        sibling.mkdir(parents=True)
        with Store(sibling) as other:
            self.assertNotEqual(other.namespace, self.store.namespace)

    def test_canonical_alias_has_same_scope(self):
        alias = self.root / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        with Store(alias) as other:
            self.assertEqual(other.project, self.project.resolve())
            self.assertEqual(other.namespace, self.store.namespace)

    def test_permissions_and_refusal_of_external_storage_symlinks(self):
        self.assertEqual(stat.S_IMODE(self.store.db_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.db_path.parent.stat().st_mode), 0o700)
        for name in ("memory.sqlite3-wal", "memory.sqlite3-shm"):
            path = self.store.db_path.parent / name
            if path.exists():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
        other = self.root / "symlink-project"
        other.mkdir()
        (other / ".agentbridge").symlink_to(self.project / ".agentbridge")
        with self.assertRaises(ValueError):
            Store(other)

    def test_missing_directory_is_rejected(self):
        with self.assertRaises(ValueError):
            Store(self.root / "does-not-exist")

    def test_paths_normalize_and_sensitive_paths_are_omitted(self):
        cleaned = self.store._files([
            str(self.project / "src" / "main.py"), "src/main.py", "./tests/test_main.py",
            ".env", ".env.local", "credentials.json", "keys/server.pem", "secrets/token.txt",
        ])
        self.assertEqual(cleaned, ["src/main.py", "tests/test_main.py"])
        self.assertEqual(self.store._files(None), [])
        for path in ("../outside", "src/../main.py", str(self.root / "outside"), ".",
                     "bad\x00name", "bad\nname", "a" * 161):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store._files([path])
        with self.assertRaises(ValueError):
            self.store._files(["a.py"] * 41)
        with self.assertRaises(ValueError):
            self.store._files("a.py")
        (self.project / "outside-link").symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.store._files(["outside-link/other"])

    def test_redaction_and_control_characters(self):
        secret = "sk-proj-" + "a" * 32
        summary = (
            f"Fixed API. {secret}\x00\x1b[31m\n"
            "password='hidden password' api_key=hidden-key\n"
            '"password": "json-password"\n'
            "Bearer abcdef123456\n"
            "-----BEGIN RSA PRIVATE KEY-----\nprivate-body\n-----END RSA PRIVATE KEY-----\n"
            "https://bob:password@host.example/path"
        )
        scrubbed = redact_summary(summary)
        for forbidden in (secret, "hidden password", "hidden-key", "json-password",
                          "abcdef123456", "private-body", "bob:password", "\x00", "\x1b"):
            self.assertNotIn(forbidden, scrubbed)
        self.assertIn("[REDACTED]", scrubbed)
        self.assertIn("Fixed API.", scrubbed)
        self.assertIn("\n", scrubbed)

    def test_multiprocess_concurrent_first_open(self):
        concurrent = self.root / "concurrent"
        concurrent.mkdir()
        context = multiprocessing.get_context("spawn")
        processes = [context.Process(target=_open_worker, args=(str(concurrent),)) for _ in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        with Store(concurrent) as store:
            self.assertTrue(store.db_path.is_file())


if __name__ == "__main__":
    unittest.main()
