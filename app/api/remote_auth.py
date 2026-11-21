from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthConfig:
    """Authentication configuration for the headless recording API."""

    api_key: str
    signing_secret: str
    download_ttl_seconds: int = 86400

    @classmethod
    def from_env(cls) -> AuthConfig:
        api_key = os.getenv("STREAMCAP_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("STREAMCAP_API_KEY must be set and non-empty to start the headless server")

        signing_secret = os.getenv("STREAMCAP_SIGNING_SECRET", "").strip()
        if not signing_secret:
            signing_secret = hmac.new(
                api_key.encode("utf-8"),
                b"streamcap-download-signing-v1",
                hashlib.sha256,
            ).hexdigest()

        raw_ttl = os.getenv("STREAMCAP_DOWNLOAD_TTL_SECONDS", "86400").strip()
        try:
            ttl = max(60, int(raw_ttl))
        except ValueError:
            ttl = 86400

        return cls(api_key=api_key, signing_secret=signing_secret, download_ttl_seconds=ttl)

    def verify_bearer(self, authorization: str | None) -> bool:
        """Verify an HTTP ``Authorization: Bearer <token>`` header in constant time."""
        if not authorization:
            return False
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        return hmac.compare_digest(token.strip(), self.api_key)

    def sign_download(self, task_id: str, filename: str, expires: int | None = None) -> tuple[int, str]:
        """Generate an expiration timestamp and HMAC-SHA256 signature for a file download."""
        exp = expires if expires is not None else int(time.time()) + self.download_ttl_seconds
        payload = f"{task_id}\n{filename}\n{exp}".encode("utf-8")
        sig = hmac.new(self.signing_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return exp, sig

    def verify_download(self, task_id: str, filename: str, expires: int | None, sig: str | None) -> bool:
        """Validate a signed download token and check expiry."""
        if expires is None or not sig:
            return False
        if int(time.time()) > expires:
            return False
        _, expected = self.sign_download(task_id, filename, expires=expires)
        return hmac.compare_digest(sig.strip(), expected)
