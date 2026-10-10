import unittest

from scout.boss_interface import (
    AurenBossV2Interface,
    BossInterfaceError,
    normalize_origin,
    signed_request_headers,
    validate_allowed_origins,
    verify_request_signature,
)


SECRET = "0123456789abcdef0123456789abcdef"


class AurenBossInterfaceTests(unittest.TestCase):
    def test_signed_request_verifies_method_path_and_exact_body(self):
        body = b'{"task_id":"123"}'
        headers = signed_request_headers(
            SECRET,
            "POST",
            "/operator/tasks",
            body,
            timestamp=1_800_000_000,
            nonce="0123456789abcdef",
        )

        self.assertTrue(verify_request_signature(
            SECRET,
            "POST",
            "/operator/tasks",
            body,
            headers["X-Auren-Timestamp"],
            headers["X-Auren-Nonce"],
            headers["X-Auren-Signature"],
            now=1_800_000_000,
        ))
        self.assertFalse(verify_request_signature(
            SECRET,
            "POST",
            "/operator/tasks",
            body + b" ",
            headers["X-Auren-Timestamp"],
            headers["X-Auren-Nonce"],
            headers["X-Auren-Signature"],
            now=1_800_000_000,
        ))
        self.assertFalse(verify_request_signature(
            SECRET,
            "GET",
            "/operator/tasks",
            body,
            headers["X-Auren-Timestamp"],
            headers["X-Auren-Nonce"],
            headers["X-Auren-Signature"],
            now=1_800_000_000,
        ))

    def test_origin_scope_normalizes_ports_and_rejects_local_destinations(self):
        self.assertEqual(normalize_origin("https://directory.example/"), "https://directory.example")
        self.assertEqual(normalize_origin("http://directory.example:8081"), "http://directory.example:8081")
        with self.assertRaisesRegex(ValueError, "origens HTTP"):
            normalize_origin("https://directory.example/search")
        with self.assertRaisesRegex(ValueError, "não pública"):
            normalize_origin("http://127.0.0.1:8080")
        with self.assertRaisesRegex(ValueError, "duplicatas"):
            validate_allowed_origins(["https://directory.example", "https://directory.example/"])

    def test_interface_requires_loopback_http_and_strong_secret(self):
        with self.assertRaisesRegex(ValueError, "32 bytes"):
            AurenBossV2Interface("short")
        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            AurenBossV2Interface(SECRET, "http://0.0.0.0:8080")

    def test_http_errors_keep_structured_code(self):
        error = BossInterfaceError(401, "invalid_boss_signature", "invalid signature")
        self.assertEqual(error.status, 401)
        self.assertEqual(error.code, "invalid_boss_signature")


if __name__ == "__main__":
    unittest.main()
