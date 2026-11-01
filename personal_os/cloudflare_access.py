"""Optional fail-closed Cloudflare Access gate for the local Tunnel origin."""
from __future__ import annotations
import os
import re
from urllib.parse import urlparse


class AccessFailure(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


class AccessGate:
    def __init__(self, origin: str, issuer: str, audience: str, owner_email: str):
        parsed = urlparse(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.netloc != parsed.hostname
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("Public origin must be one HTTPS hostname without a path or port")
        if not re.fullmatch(r"https://[a-zA-Z0-9-]+\.cloudflareaccess\.com", issuer):
            raise ValueError("Access issuer must be the exact Cloudflare Access team URL")
        if not audience or not owner_email or "@" not in owner_email or any(c.isspace() for c in owner_email):
            raise ValueError("Access audience and one owner email are required")
        self.origin = origin.rstrip("/")
        self.hostname = parsed.hostname.lower()
        self.issuer = issuer
        self.audience = audience
        self.owner_email = owner_email.lower()
        self.client = None

    def applies(self, host: str) -> bool:
        return host.lower() in {self.hostname, self.hostname + ":443"}

    def requires(self, headers) -> bool:
        # A forwarded request must never fall through to the loopback exemption.
        return self.applies(headers.get("Host", "")) or any(
            headers.get(name) for name in ("Cf-Ray", "Cf-Access-Jwt-Assertion", "Cf-Connecting-Ip")
        )

    def authorize(self, headers, state_changing=False):
        if not self.applies(headers.get("Host", "")):
            raise AccessFailure("Unexpected public hostname", 403)
        token = headers.get("Cf-Access-Jwt-Assertion", "")
        if not isinstance(token, str) or not token or len(token) > 16000:
            raise AccessFailure("Cloudflare Access authentication required")
        try:
            import jwt
            if self.client is None:
                self.client = jwt.PyJWKClient(self.issuer + "/cdn-cgi/access/certs", cache_keys=True, timeout=5)
            key = self.client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                audience=self.audience, leeway=30,
                                options={"require": ["exp", "iat", "iss", "aud", "sub", "email"]})
        except ImportError as error:
            raise AccessFailure("Cloudflare authentication dependencies are unavailable", 503) from error
        except Exception as error:
            raise AccessFailure("Cloudflare Access authentication could not be verified") from error
        if str(claims.get("email", "")).lower() != self.owner_email:
            raise AccessFailure("Owner access only", 403)
        if state_changing and headers.get("Origin", "").rstrip("/") != self.origin:
            raise AccessFailure("Same-origin request required", 403)
        return claims["sub"]


def from_environment():
    origin = os.environ.get("PERSONAL_OS_PUBLIC_ORIGIN", "").strip()
    if not origin:
        return None
    return AccessGate(origin, os.environ.get("PERSONAL_OS_ACCESS_ISSUER", "").strip(),
                      os.environ.get("PERSONAL_OS_ACCESS_AUDIENCE", "").strip(),
                      os.environ.get("PERSONAL_OS_ACCESS_OWNER_EMAIL", "").strip())
