"""Local secret preparation never replaces credentials already in use."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[3] / "infra" / "compose" / "setup.py"
FILES = ("postgres_password", "migrator_password", "runtime_password")


class ComposeSetupTests(unittest.TestCase):
    def prepare(self, directory: Path) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(SETUP), "--directory", str(directory)]
        if os.name != "nt":
            command = [
                sys.executable,
                "-c",
                "import os, runpy, sys; os.umask(0o077); "
                "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')",
                *command[1:],
            ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_create_distinct_secrets_and_repeat_without_replacing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "secrets"
            first = self.prepare(directory)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = {name: (directory / name).read_bytes() for name in FILES}
            self.assertEqual(len(set(original.values())), 3)
            for password in original.values():
                self.assertEqual(len(password), 64)
                self.assertNotIn(password.decode(), first.stdout + first.stderr)
            second = self.prepare(directory)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                original, {name: (directory / name).read_bytes() for name in FILES}
            )
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                for name in FILES:
                    self.assertEqual(
                        stat.S_IMODE((directory / name).stat().st_mode), 0o444
                    )

    def test_partial_or_empty_set_fails_without_modification(self) -> None:
        for value in ("secret-sentinel", ""):
            with self.subTest(value=bool(value)), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                (directory / FILES[0]).write_text(value, encoding="utf-8")
                result = self.prepare(directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(directory.iterdir()), [directory / FILES[0]])
                self.assertEqual(
                    (directory / FILES[0]).read_text(encoding="utf-8"), value
                )
                self.assertNotIn("secret-sentinel", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertIn("Secret preparation failed.", result.stderr)

    def test_invalid_complete_set_is_rejected(self) -> None:
        for values in (("", "a", "b"), ("same", "same", "same")):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                for name, value in zip(FILES, values, strict=True):
                    (directory / name).write_text(value, encoding="utf-8")
                result = self.prepare(directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    tuple((directory / name).read_text() for name in FILES), values
                )


if __name__ == "__main__":
    unittest.main()
