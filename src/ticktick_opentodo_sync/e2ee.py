"""OpenTodo end-to-end encryption: the client side, as OpenTodo's own apps do it.

Format (OpenTodo ``docs/e2ee.md``, ``internal/e2ee/crypt``):

    value  "enc:v1:" + base64url(nonce(12) || AES-256-GCM(utf8(plaintext)) || tag(16))
    aad    entity + "\\0" + entity id + "\\0" + field
    key    the 256-bit account key, AES-KW (RFC 3394) wrapped under
           PBKDF2-HMAC-SHA-256(NFC(passphrase), salt, iterations) or under
           HKDF-SHA-256(recovery key, salt=32 zero bytes, info=RECOVERY_INFO)

The server never decrypts; it serves the wrapped key record at
``GET /api/v1/encryption/keys``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap

PREFIX = "enc:"
PREFIX_V1 = "enc:v1:"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
KDF_PBKDF2_SHA256 = 1
MIN_ITERATIONS = 600_000
MAX_ITERATIONS = 10_000_000
RECOVERY_INFO = b"opentodo/e2ee/recovery-kek/v1"

_ENVELOPE = re.compile(r"enc:v[0-9]+:")


class E2EEError(Exception):
    pass


class DecryptError(E2EEError):
    """The value is not authentic for this key, entity, id and field."""


class WrongSecretError(E2EEError):
    """The passphrase or recovery key does not unwrap the account key."""


def is_ciphertext(value: object) -> bool:
    """True for any ``enc:vN:...`` envelope (current or future format)."""
    return isinstance(value, str) and _ENVELOPE.match(value) is not None


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _aad(entity: str, entity_id: str, field: str) -> bytes:
    return f"{entity}\0{entity_id}\0{field}".encode()


class AccountKey:
    """The unwrapped account key. Never logged, never written to disk."""

    __slots__ = ("_gcm",)

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise E2EEError(f"account key must be {KEY_BYTES} bytes")
        self._gcm = AESGCM(key)

    def __repr__(self) -> str:
        return "AccountKey(<redacted>)"

    def seal(
        self, entity: str, entity_id: str, field: str, plaintext: str, *, nonce: bytes | None = None
    ) -> str:
        """Encrypt one value; ``nonce`` is for known-answer tests only."""
        n = os.urandom(NONCE_BYTES) if nonce is None else nonce
        if len(n) != NONCE_BYTES:
            raise E2EEError("nonce must be 12 bytes")
        ct = self._gcm.encrypt(n, plaintext.encode(), _aad(entity, entity_id, field))
        return PREFIX_V1 + _b64e(n + ct)

    def open(self, entity: str, entity_id: str, field: str, value: str) -> str:
        if not value.startswith(PREFIX_V1):
            raise DecryptError("not an enc:v1 value")
        try:
            body = _b64d(value[len(PREFIX_V1) :])
        except ValueError:
            raise DecryptError("malformed base64") from None
        if len(body) < NONCE_BYTES + TAG_BYTES:
            raise DecryptError("value too short")
        try:
            pt = self._gcm.decrypt(
                body[:NONCE_BYTES], body[NONCE_BYTES:], _aad(entity, entity_id, field)
            )
            return pt.decode()
        except (InvalidTag, UnicodeDecodeError):
            raise DecryptError(f"cannot decrypt {entity}.{field} of {entity_id}") from None


@dataclass(frozen=True, slots=True)
class KeyRecord:
    version: int
    kdf: int
    salt: str
    iterations: int
    wrapped_by_passphrase: str
    wrapped_by_recovery: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> KeyRecord:
        try:
            return cls(
                version=int(data["version"]),
                kdf=int(data["kdf"]),
                salt=str(data["salt"]),
                iterations=int(data["iterations"]),
                wrapped_by_passphrase=str(data["wrappedByPassphrase"]),
                wrapped_by_recovery=str(data["wrappedByRecovery"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise E2EEError(f"malformed OpenTodo key record: {exc}") from None


def _unwrap(kek: bytes, wrapped_b64: str) -> AccountKey:
    try:
        return AccountKey(aes_key_unwrap(kek, _b64d(wrapped_b64)))
    except (InvalidUnwrap, ValueError):
        raise WrongSecretError("the passphrase or recovery key is not correct") from None


def unlock_with_passphrase(
    record: KeyRecord, passphrase: str, *, min_iterations: int = MIN_ITERATIONS
) -> AccountKey:
    if record.kdf != KDF_PBKDF2_SHA256:
        raise E2EEError(f"unsupported key derivation {record.kdf}")
    if not min_iterations <= record.iterations <= MAX_ITERATIONS:
        raise E2EEError(f"key record iterations {record.iterations} outside the accepted range")
    kek = hashlib.pbkdf2_hmac(
        "sha256",
        unicodedata.normalize("NFC", passphrase).encode(),
        _b64d(record.salt),
        record.iterations,
        KEY_BYTES,
    )
    return _unwrap(kek, record.wrapped_by_passphrase)


_B32_FIX = str.maketrans({"0": "O", "1": "I", "8": "B"})


def parse_recovery_key(text: str) -> bytes:
    """Typed recovery key -> 32 raw bytes. Case, spaces and dashes are ignored; 0/1/8 read O/I/B."""
    s = re.sub(r"[\s-]", "", text).upper().translate(_B32_FIX)
    bad = E2EEError("not a valid OpenTodo recovery key; check for typos")
    if len(s) != 56:
        raise bad
    try:
        raw = base64.b32decode(s[:52] + "====")[:KEY_BYTES]
    except ValueError:
        raise bad from None
    if base64.b32encode(raw).decode().rstrip("=") != s[:52]:
        raise bad
    if base64.b32encode(hashlib.sha256(raw).digest()[:3]).decode()[:4] != s[52:]:
        raise bad
    return raw


def unlock_with_recovery_key(record: KeyRecord, text: str) -> AccountKey:
    if record.kdf != KDF_PBKDF2_SHA256:
        raise E2EEError(f"unsupported key derivation {record.kdf}")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=bytes(32), info=RECOVERY_INFO)
    return _unwrap(hkdf.derive(parse_recovery_key(text)), record.wrapped_by_recovery)
