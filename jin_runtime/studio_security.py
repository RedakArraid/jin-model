"""Security primitives and role policy for JIN Studio."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Iterable


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "VIEWER": frozenset({"studio.read"}),
    "ANNOTATOR": frozenset(
        {"studio.read", "documents.write", "annotations.draft"}
    ),
    "VALIDATOR": frozenset(
        {
            "studio.read",
            "documents.write",
            "annotations.draft",
            "annotations.validate",
        }
    ),
    "TRAINER": frozenset(
        {"studio.read", "datasets.write", "training.write"}
    ),
    "ADMIN": frozenset({"*"}),
}
SERVICE_PERMISSIONS = frozenset(
    {
        "studio.read",
        "documents.write",
        "annotations.draft",
        "annotations.validate",
        "datasets.write",
        "training.write",
    }
)
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
PASSWORD_MAX_LENGTH = 256
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


class StudioSecurityError(ValueError):
    """Raised when identity data does not satisfy the Studio policy."""


@dataclass(frozen=True)
class StudioPrincipal:
    user_id: str
    username: str
    display_name: str
    roles: tuple[str, ...]
    permissions: frozenset[str]
    auth_type: str
    session_hash: str = ""
    csrf_token: str = ""
    expires_at: str = ""

    def allows(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions

    def public(self, *, include_csrf: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "userId": self.user_id,
            "username": self.username,
            "displayName": self.display_name,
            "roles": list(self.roles),
            "permissions": sorted(self.permissions),
            "authType": self.auth_type,
        }
        if include_csrf and self.csrf_token:
            payload["csrfToken"] = self.csrf_token
            payload["sessionExpiresAt"] = self.expires_at
        return payload


def password_min_length() -> int:
    try:
        configured = int(os.getenv("JIN_STUDIO_PASSWORD_MIN_LENGTH") or "8")
    except ValueError:
        configured = 8
    return max(4, min(128, configured))


def session_duration_hours() -> int:
    try:
        configured = int(os.getenv("JIN_STUDIO_SESSION_HOURS") or "12")
    except ValueError:
        configured = 12
    return max(1, min(168, configured))


def normalize_username(value: Any) -> str:
    username = str(value or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise StudioSecurityError(
            "Le nom d'utilisateur doit contenir 3 à 64 caractères : lettres "
            "minuscules, chiffres, point, tiret ou soulignement."
        )
    return username


def normalize_roles(values: Iterable[Any]) -> tuple[str, ...]:
    roles = tuple(
        dict.fromkeys(str(value or "").strip().upper() for value in values)
    )
    if not roles:
        raise StudioSecurityError("Au moins un rôle est requis.")
    invalid = [role for role in roles if role not in ROLE_PERMISSIONS]
    if invalid:
        raise StudioSecurityError(f"Rôle inconnu : {', '.join(invalid)}")
    return roles


def permissions_for_roles(roles: Iterable[str]) -> frozenset[str]:
    permissions: set[str] = set()
    for role in roles:
        permissions.update(ROLE_PERMISSIONS.get(str(role).upper(), ()))
    return frozenset(permissions)


def validate_password(password: Any) -> str:
    value = str(password or "")
    minimum = password_min_length()
    if len(value) < minimum:
        raise StudioSecurityError(
            f"Le mot de passe doit contenir au moins {minimum} caractères."
        )
    if len(value) > PASSWORD_MAX_LENGTH:
        raise StudioSecurityError(
            f"Le mot de passe ne peut pas dépasser {PASSWORD_MAX_LENGTH} caractères."
        )
    return value


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: Any) -> str:
    value = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${_encode(salt)}${_encode(digest)}"
    )


def _dummy_password_hash() -> str:
    salt = b"jin-studio-dummy"
    digest = hashlib.scrypt(
        b"not-a-real-studio-password",
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${_encode(salt)}${_encode(digest)}"
    )


DUMMY_PASSWORD_HASH = _dummy_password_hash()


def verify_password(password: Any, encoded: str) -> bool:
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, expected_raw = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if n > 2**16 or r > 16 or p > 4:
            return False
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=_decode(salt_raw),
            n=n,
            r=r,
            p=p,
            dklen=len(_decode(expected_raw)),
        )
        return hmac.compare_digest(digest, _decode(expected_raw))
    except (ValueError, TypeError):
        return False


def random_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def service_principal() -> StudioPrincipal:
    return StudioPrincipal(
        user_id="service:jin-studio",
        username="service",
        display_name="Intégration JIN Studio",
        roles=("SERVICE",),
        permissions=SERVICE_PERMISSIONS,
        auth_type="service",
    )


__all__ = [
    "DUMMY_PASSWORD_HASH",
    "PASSWORD_MAX_LENGTH",
    "ROLE_PERMISSIONS",
    "SERVICE_PERMISSIONS",
    "StudioPrincipal",
    "StudioSecurityError",
    "hash_password",
    "normalize_roles",
    "normalize_username",
    "password_min_length",
    "permissions_for_roles",
    "random_token",
    "service_principal",
    "session_duration_hours",
    "token_digest",
    "validate_password",
    "verify_password",
]
