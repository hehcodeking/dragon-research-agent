import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dragon_agent.auth import configure_interactive, save_credential
from dragon_agent.data import CredentialError


class AuthTests(unittest.TestCase):
    def test_private_file_never_returns_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hithink-finance" / "credentials.env"
            result = save_credential("fake-test-key", path=path)
            self.assertNotIn("fake-test-key", str(result))
            self.assertEqual(path.read_text(), "HITHINK_FINANCE_API_KEY=fake-test-key\n")
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_no_unrequested_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.env"
            save_credential("first-test-key", path=path)
            with self.assertRaises(CredentialError):
                save_credential("second-test-key", path=path)
            self.assertIn("first-test-key", path.read_text())
            save_credential("second-test-key", path=path, replace=True)
            self.assertIn("second-test-key", path.read_text())

    def test_symlink_target_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            original.write_text("preserve")
            link = Path(directory) / "credentials.env"
            link.symlink_to(original)
            with self.assertRaises(CredentialError):
                save_credential("fake-test-key", path=link, replace=True)
            self.assertEqual(original.read_text(), "preserve")

    def test_invalid_token_never_saved_or_echoed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.env"
            with self.assertRaises(CredentialError) as caught:
                save_credential("bad-key\nInjected=1", path=path)
            self.assertNotIn("bad-key", str(caught.exception))
            self.assertFalse(path.exists())

    def test_existing_source_reused_without_input(self):
        with patch("dragon_agent.auth.credential_status", return_value={"present": True, "source": "environment"}), patch("dragon_agent.auth.getpass.getpass") as prompt:
            self.assertTrue(configure_interactive()["reused"])
            prompt.assert_not_called()

    def test_noninteractive_missing_key_requires_local_input(self):
        with patch("dragon_agent.auth.credential_status", return_value={"present": False}), patch("dragon_agent.auth.sys.stdin.isatty", return_value=False):
            with self.assertRaises(CredentialError):
                configure_interactive()


if __name__ == "__main__":
    unittest.main()
