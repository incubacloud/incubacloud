"""
Tier 1 — Pure-Python unit tests for GitHub utilities:
  - validate_hmac_sha256  (webhook_utils)
  - generate_github_app_jwt / _b64url  (jwt_utils)
"""
import base64
import hashlib
import hmac
import json
import time
import unittest


# ── validate_hmac_sha256 ──────────────────────────────────────────────────────

class TestValidateHmacSha256(unittest.TestCase):

    def setUp(self):
        from odoo.addons.incubacloud.github.webhook_utils import validate_hmac_sha256
        self.validate = validate_hmac_sha256

    def _sign(self, payload: bytes, secret: str) -> str:
        return "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()

    def test_valid_signature_returns_true(self):
        payload = b'{"action": "opened"}'
        secret = "my-webhook-secret"
        self.assertTrue(self.validate(payload, self._sign(payload, secret), secret))

    def test_wrong_secret_returns_false(self):
        payload = b'{"action": "opened"}'
        sig = self._sign(payload, "correct-secret")
        self.assertFalse(self.validate(payload, sig, "wrong-secret"))

    def test_tampered_payload_returns_false(self):
        sig = self._sign(b"original-payload", "secret")
        self.assertFalse(self.validate(b"tampered-payload", sig, "secret"))

    def test_missing_sha256_prefix_returns_false(self):
        payload = b"data"
        raw_hex = hmac.new(b"secret", payload, hashlib.sha256).hexdigest()
        # No "sha256=" prefix
        self.assertFalse(self.validate(payload, raw_hex, "secret"))

    def test_empty_secret_returns_false(self):
        payload = b"data"
        sig = self._sign(payload, "nonempty")
        self.assertFalse(self.validate(payload, sig, ""))

    def test_none_secret_returns_false(self):
        self.assertFalse(self.validate(b"data", "sha256=abc", None))

    def test_empty_header_returns_false(self):
        self.assertFalse(self.validate(b"data", "", "secret"))

    def test_none_header_returns_false(self):
        self.assertFalse(self.validate(b"data", None, "secret"))

    def test_empty_payload_valid_sig(self):
        payload = b""
        secret = "secret"
        sig = self._sign(payload, secret)
        self.assertTrue(self.validate(payload, sig, secret))

    def test_large_payload(self):
        payload = b"x" * 100_000
        secret = "bigsecret"
        sig = self._sign(payload, secret)
        self.assertTrue(self.validate(payload, sig, secret))


# ── generate_github_app_jwt ───────────────────────────────────────────────────

class TestGenerateGitHubAppJwt(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Generate a real RSA key pair once for all JWT tests."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.private_key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        cls.public_key = private_key.public_key()

    def setUp(self):
        from odoo.addons.incubacloud.github.jwt_utils import (
            generate_github_app_jwt,
            _b64url,
        )
        self.generate_jwt = generate_github_app_jwt
        self.b64url = _b64url

    def _decode_part(self, part: str) -> dict:
        """Decode a base64url JWT part (no padding assumed)."""
        padding = 4 - len(part) % 4
        if padding != 4:
            part += "=" * padding
        return json.loads(base64.urlsafe_b64decode(part))

    # ── structure ─────────────────────────────────────────────────────────────

    def test_jwt_has_three_parts(self):
        token = self.generate_jwt("12345", self.private_key_pem)
        self.assertEqual(len(token.split(".")), 3)

    def test_jwt_header_alg_and_typ(self):
        token = self.generate_jwt("12345", self.private_key_pem)
        header = self._decode_part(token.split(".")[0])
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(header["typ"], "JWT")

    def test_jwt_payload_iss_matches_app_id(self):
        token = self.generate_jwt("99999", self.private_key_pem)
        payload = self._decode_part(token.split(".")[1])
        self.assertEqual(payload["iss"], "99999")

    def test_jwt_payload_iat_is_backdated(self):
        before = int(time.time())
        token = self.generate_jwt("12345", self.private_key_pem)
        payload = self._decode_part(token.split(".")[1])
        # iat must be <= now - 59 seconds
        self.assertLessEqual(payload["iat"], before - 59)

    def test_jwt_payload_exp_is_10_min_from_now(self):
        before = int(time.time())
        token = self.generate_jwt("12345", self.private_key_pem)
        after = int(time.time())
        payload = self._decode_part(token.split(".")[1])
        self.assertGreaterEqual(payload["exp"], before + 590)
        self.assertLessEqual(payload["exp"], after + 610)

    # ── signature verification ────────────────────────────────────────────────

    def test_signature_verifiable_with_public_key(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

        parts = self.generate_jwt("12345", self.private_key_pem).split(".")
        signing_input = f"{parts[0]}.{parts[1]}".encode()

        sig_b64 = parts[2]
        pad = 4 - len(sig_b64) % 4
        if pad != 4:
            sig_b64 += "=" * pad
        signature = base64.urlsafe_b64decode(sig_b64)

        # Must not raise
        self.public_key.verify(
            signature,
            signing_input,
            asym_padding.PKCS1v15(),
            hashes.SHA256(),
        )

    # ── _b64url helper ────────────────────────────────────────────────────────

    def test_b64url_encodes_dict(self):
        result = self.b64url({"key": "value"})
        decoded = json.loads(base64.urlsafe_b64decode(result + "=="))
        self.assertEqual(decoded["key"], "value")

    def test_b64url_no_padding_chars(self):
        result = self.b64url(b"hello")
        self.assertNotIn("=", result)

    def test_b64url_string_input(self):
        result = self.b64url("hello")
        decoded = base64.urlsafe_b64decode(result + "==")
        self.assertEqual(decoded, b"hello")
