from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

ROLES = {"analyst", "compliance", "ops", "admin"}

COMMAND_ROLE_POLICY: dict[str, set[str]] = {
    "submit_application": {"analyst", "ops", "admin"},
    "start_agent_session": {"analyst", "ops", "admin"},
    "record_credit_analysis": {"analyst", "ops", "admin"},
    "record_fraud_screening": {"analyst", "ops", "admin"},
    "record_compliance_check": {"compliance", "ops", "admin"},
    "generate_decision": {"analyst", "ops", "admin"},
    "record_human_review": {"ops", "admin"},
    "run_integrity_check": {"compliance", "admin"},
}


LEGACY_PLATFORM_USERS: list[tuple[str, str, str]] = [
    ("analyst", "analyst123!", "analyst"),
    ("compliance", "compliance123!", "compliance"),
    ("ops", "ops123!", "ops"),
    ("admin", "admin123!", "admin"),
]


def configured_seed_users() -> list[tuple[str, str, str]]:
    analyst_one_username = (
        os.getenv("LEDGER_ANALYST_ONE_USERNAME")
        or os.getenv("LEDGER_ANALYST_USERNAME", "melat")
    ).strip() or "melat"
    analyst_one_password = (
        os.getenv("LEDGER_ANALYST_ONE_PASSWORD")
        or os.getenv("LEDGER_ANALYST_PASSWORD", "melat@123")
    )
    analyst_two_username = os.getenv("LEDGER_ANALYST_TWO_USERNAME", "kedir").strip() or "kedir"
    analyst_two_password = os.getenv("LEDGER_ANALYST_TWO_PASSWORD", "kedir@123")
    admin_username = os.getenv("LEDGER_ADMIN_USERNAME", "nurye").strip() or "nurye"
    admin_password = os.getenv("LEDGER_ADMIN_PASSWORD", "nurye@123")

    users: list[tuple[str, str, str]] = [
        *LEGACY_PLATFORM_USERS,
        (analyst_one_username, analyst_one_password, "analyst"),
        (analyst_two_username, analyst_two_password, "analyst"),
        (admin_username, admin_password, "admin"),
    ]

    deduped: dict[str, tuple[str, str, str]] = {}
    for username, password, role in users:
        deduped[username] = (username, password, role)
    return list(deduped.values())


@dataclass(slots=True)
class AuthPrincipal:
    username: str
    role: str
    issued_at: datetime
    expires_at: datetime


@dataclass(slots=True)
class TokenClaims:
    sub: str
    role: str
    iat: int
    exp: int
    nbf: int
    iss: str


def can_invoke_command(role: str, command_name: str) -> bool:
    allowed = COMMAND_ROLE_POLICY.get(command_name)
    if allowed is None:
        return False
    return role in allowed


def can_rebuild_projections(role: str) -> bool:
    return role in {"ops", "admin"}


def can_view_auth_audit(role: str) -> bool:
    return role in {"compliance", "admin"}


def can_bootstrap_demo(role: str) -> bool:
    return role in {"analyst", "ops", "admin"}


def create_password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, salt, digest_hex = stored_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(digest.hex(), digest_hex)


def issue_access_token(
    *,
    username: str,
    role: str,
    secret: str,
    issuer: str,
    ttl_minutes: int,
    now: datetime | None = None,
) -> str:
    if role not in ROLES:
        raise ValueError(f"Invalid role '{role}'.")

    now_dt = now or datetime.now(UTC)
    iat = int(now_dt.timestamp())
    exp = int((now_dt + timedelta(minutes=ttl_minutes)).timestamp())

    payload = {
        "sub": username,
        "role": role,
        "iat": iat,
        "exp": exp,
        "nbf": iat,
        "iss": issuer,
    }
    header = {"alg": "HS256", "typ": "JWT"}

    encoded_header = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    encoded_signature = _b64url_encode(signature)
    return f"{encoded_header}.{encoded_payload}.{encoded_signature}"


def decode_access_token(
    token: str,
    *,
    secret: str,
    issuer: str,
    now: datetime | None = None,
) -> AuthPrincipal:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".", 2)
    except ValueError as exc:
        raise ValueError("Token must contain 3 segments.") from exc

    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    expected_signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    supplied_signature = _b64url_decode(encoded_signature)

    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise ValueError("Token signature is invalid.")

    payload_data = _b64url_decode(encoded_payload)
    try:
        payload = json.loads(payload_data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Token payload is malformed JSON.") from exc

    claims = TokenClaims(
        sub=str(payload.get("sub", "")),
        role=str(payload.get("role", "")),
        iat=int(payload.get("iat", 0)),
        exp=int(payload.get("exp", 0)),
        nbf=int(payload.get("nbf", 0)),
        iss=str(payload.get("iss", "")),
    )

    if claims.iss != issuer:
        raise ValueError("Token issuer does not match.")
    if claims.role not in ROLES:
        raise ValueError("Token role is invalid.")
    if not claims.sub:
        raise ValueError("Token subject is required.")

    now_dt = now or datetime.now(UTC)
    current_ts = int(now_dt.timestamp())
    if current_ts < claims.nbf:
        raise ValueError("Token is not valid yet.")
    if current_ts >= claims.exp:
        raise ValueError("Token has expired.")

    return AuthPrincipal(
        username=claims.sub,
        role=claims.role,
        issued_at=datetime.fromtimestamp(claims.iat, tz=UTC),
        expires_at=datetime.fromtimestamp(claims.exp, tz=UTC),
    )


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("utf-8"))
