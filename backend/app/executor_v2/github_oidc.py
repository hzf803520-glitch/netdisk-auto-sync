from __future__ import annotations

import logging
import os
import threading
from typing import Any

import jwt


logger = logging.getLogger(__name__)

OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OIDC_JWKS_URL = f"{OIDC_ISSUER}/.well-known/jwks"
OIDC_AUDIENCE = "netdisk-sync-executor-v2"


class GitHubWakeAuth:
    """Validate short-lived GitHub Actions OIDC tokens without shared secrets."""

    def __init__(self) -> None:
        self.repository = os.getenv(
            "GITHUB_WAKE_REPOSITORY",
            "hzf803520-glitch/netdisk-auto-sync",
        ).strip()
        self.workflow = os.getenv(
            "GITHUB_WAKE_WORKFLOW",
            ".github/workflows/executor-keepalive.yml",
        ).strip()
        self.ref = os.getenv("GITHUB_WAKE_REF", "refs/heads/main").strip()
        self._client: jwt.PyJWKClient | None = None
        self._lock = threading.Lock()

    def valid_bearer(self, authorization: str) -> bool:
        if not authorization.startswith("Bearer "):
            return False
        token = authorization[7:].strip()
        if not token or not self.repository or not self.workflow:
            return False
        try:
            signing_key = self._jwk_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=OIDC_AUDIENCE,
                issuer=OIDC_ISSUER,
                leeway=30,
                options={"require": ["exp", "iat", "repository", "ref"]},
            )
        except Exception as exc:
            logger.warning("GitHub wake token rejected: %s", type(exc).__name__)
            return False
        return self._claims_allowed(claims)

    def _jwk_client(self) -> jwt.PyJWKClient:
        with self._lock:
            if self._client is None:
                self._client = jwt.PyJWKClient(
                    OIDC_JWKS_URL,
                    cache_keys=True,
                    lifespan=3600,
                )
            return self._client

    def _claims_allowed(self, claims: dict[str, Any]) -> bool:
        workflow_ref = str(claims.get("workflow_ref") or "")
        expected_workflow = f"{self.repository}/{self.workflow}@{self.ref}"
        return (
            str(claims.get("repository") or "") == self.repository
            and str(claims.get("ref") or "") == self.ref
            and str(claims.get("event_name") or "") in {"schedule", "workflow_dispatch"}
            and workflow_ref == expected_workflow
        )
