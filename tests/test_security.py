import unittest

from ebaseline.security import (
    collect_secret_values,
    mask_variables,
    merge_masked_values,
    redact_text,
)


class MaskTest(unittest.TestCase):
    def test_mask_nested(self):
        data = {
            "USER": "admin",
            "DB_PASSWORD": "p@ss",
            "nested": {"api_key": "k-123", "host": "127.0.0.1"},
            "list": [{"token": "t-1", "name": "n"}],
        }
        masked = mask_variables(data)
        self.assertEqual(masked["USER"], "admin")
        self.assertEqual(masked["DB_PASSWORD"], "********")
        self.assertEqual(masked["nested"]["api_key"], "********")
        self.assertEqual(masked["nested"]["host"], "127.0.0.1")
        self.assertEqual(masked["list"][0]["token"], "********")
        self.assertEqual(masked["list"][0]["name"], "n")
        # 原始数据不被修改
        self.assertEqual(data["DB_PASSWORD"], "p@ss")

    def test_merge_placeholder_keeps_old(self):
        old = {"USER": "u", "DB_PASSWORD": "old-secret",
               "nested": {"token": "old-token", "host": "h1"}}
        new = {"USER": "u2", "DB_PASSWORD": "********",
               "nested": {"token": "********", "host": "h2"}}
        merged = merge_masked_values(old, new)
        self.assertEqual(merged["USER"], "u2")
        self.assertEqual(merged["DB_PASSWORD"], "old-secret")
        self.assertEqual(merged["nested"]["token"], "old-token")
        self.assertEqual(merged["nested"]["host"], "h2")

    def test_merge_removed_key_deleted(self):
        merged = merge_masked_values({"A": 1, "B": 2}, {"A": 1})
        self.assertEqual(merged, {"A": 1})


class RedactTest(unittest.TestCase):
    def test_collect_and_redact(self):
        secrets = collect_secret_values(
            {"DB_PASSWORD": "supersecret", "nested": {"api_key": "abc12345"}})
        self.assertIn("supersecret", secrets)
        self.assertIn("abc12345", secrets)
        text = redact_text("connect with supersecret and abc12345", secrets)
        self.assertNotIn("supersecret", text)
        self.assertNotIn("abc12345", text)
        self.assertIn("********", text)

    def test_short_values_not_collected(self):
        self.assertEqual(collect_secret_values({"password": "123"}), set())


if __name__ == "__main__":
    unittest.main()
