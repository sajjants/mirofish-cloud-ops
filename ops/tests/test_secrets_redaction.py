import os
import unittest
from unittest import mock

from ops import keys
from ops.redact import REDACTED, redact, register_secret
from ops.tests.fake_provider import FAKE_GOOGLE_KEY


class RedactionTests(unittest.TestCase):
    def test_key_shaped_strings_are_redacted_without_registration(self):
        github = "ghp_" + "a1" * 18
        text = f"key {FAKE_GOOGLE_KEY} token {github} Authorization: Bearer abcdefgh12345 url ?key=zzz&x=1"
        out = redact(text)
        for secret in (FAKE_GOOGLE_KEY, github, "abcdefgh12345", "zzz"):
            self.assertNotIn(secret, out)
        self.assertIn("x=1", out)

    def test_registered_secrets_are_redacted_verbatim(self):
        odd_secret = "zep_" + "q9" * 12
        register_secret(odd_secret)
        self.assertEqual(redact(f"value={odd_secret}."), f"value={REDACTED}.")


class KeyLookupTests(unittest.TestCase):
    def test_environment_variable_wins(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": FAKE_GOOGLE_KEY}), \
                mock.patch.object(keys, "_from_keyring", return_value="from-keychain-value"):
            key = keys.get_key("GEMINI_API_KEY", "GEMINI_API_KEY")
        self.assertEqual((key.value, key.source), (FAKE_GOOGLE_KEY, "environment variable GEMINI_API_KEY"))
        self.assertNotIn(FAKE_GOOGLE_KEY, redact(key.value))

    def test_keychain_fallback_and_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(keys, "_from_keyring", return_value="keychain-value-123"):
                self.assertEqual(keys.get_key("GEMINI_API_KEY", "GEMINI_API_KEY").source, "OS keychain")
            with mock.patch.object(keys, "_from_keyring", return_value=None):
                self.assertIsNone(keys.get_key("GEMINI_API_KEY", "GEMINI_API_KEY"))


if __name__ == "__main__":
    unittest.main()
