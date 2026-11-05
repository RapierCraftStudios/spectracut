# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signed render jobs.

The control plane signs each job envelope with its Ed25519 private key (from the
environment or the 0600 key file ``spectracut setup`` keeps in the SpectraCut home,
never from specs, recipes or logs). Registered render hosts
hold only the matching public key, so any host can verify a job routed to it by
worker tag, but no host can forge one. The signature covers the canonical JSON
of the whole envelope, so changing any field invalidates it.
"""

from __future__ import annotations

import base64
import binascii
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, model_validator

from spectracut.spec._base import ContentHash, Slug, StrictModel, canonical_json
from spectracut.spec.recipe import Recipe

SIGNING_KEY_ENV = "SPECTRACUT_JOB_SIGNING_KEY"
"""Env var holding the control plane's base64 raw Ed25519 private key."""
SIGNING_KEY_FILE = "job-signing.key"
"""The key file ``spectracut setup`` creates (mode 0600) in the SpectraCut home."""

TagName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64)]


class JobSignatureError(Exception):
    """A job is unsigned, its signature is invalid, or it was tampered with."""


class JobEnvelope(StrictModel):
    """Everything a host needs to render one shot, as signed by the control plane."""

    job_id: Slug
    recipe: Recipe
    recipe_hash: ContentHash
    frame_start: int = Field(ge=0)
    frame_end: int = Field(ge=0)
    output: Slug
    required_tag: TagName | None = None
    issued_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _ranges(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("frame_end must be >= frame_start")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


class SignedJob(StrictModel):
    """A job envelope plus the base64 Ed25519 signature over its canonical JSON."""

    envelope: JobEnvelope
    signature: str | None = None


def _b64decode(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{what} is not valid base64") from exc


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_b64, public_b64)`` raw Ed25519 keys."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def signing_key_path(home: Path | None = None) -> Path:
    """Where the control plane's signing key lives: ``<home>/job-signing.key``."""
    if home is None:
        from spectracut.studio.store import spectracut_home

        home = spectracut_home()
    return Path(home) / SIGNING_KEY_FILE


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    """The base64 raw public key matching ``private_key`` (what hosts register)."""
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def ensure_signing_key(home: Path | None = None) -> str:
    """Create the signing key file once (mode 0600) and return its base64 public key.

    An existing key is never overwritten or regenerated.
    """
    path = signing_key_path(home)
    if not path.is_file():
        private, public = generate_keypair()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # created concurrently: use that one
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(private + "\n")
            return public
    return public_key_b64(load_private_key(path.read_text(encoding="utf-8")))


def read_public_key(home: Path | None = None) -> str | None:
    """The public key of an existing key file, or ``None`` if setup has not created one."""
    path = signing_key_path(home)
    if not path.is_file():
        return None
    return public_key_b64(load_private_key(path.read_text(encoding="utf-8")))


def load_private_key(value: str | None = None, home: Path | None = None) -> Ed25519PrivateKey:
    """Load the signing key from ``value``, :data:`SIGNING_KEY_ENV`, else the key file.

    The key file is ``<home>/job-signing.key`` (default home: the SpectraCut home).
    """
    raw = value if value is not None else os.environ.get(SIGNING_KEY_ENV)
    if not raw:
        path = signing_key_path(home)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        if not raw.strip():
            raise ValueError(
                f"no job signing key: set {SIGNING_KEY_ENV} or run `spectracut setup` "
                f"(creates {path})"
            )
    try:
        return Ed25519PrivateKey.from_private_bytes(_b64decode(raw, "signing key"))
    except ValueError:
        raise ValueError("signing key must be a base64 raw 32-byte Ed25519 key") from None


def load_public_key(value: str) -> Ed25519PublicKey:
    """Load a base64 raw Ed25519 public key."""
    try:
        return Ed25519PublicKey.from_public_bytes(_b64decode(value, "public key"))
    except ValueError:
        raise ValueError("public key must be a base64 raw 32-byte Ed25519 key") from None


def sign_job(envelope: JobEnvelope, private_key: Ed25519PrivateKey) -> SignedJob:
    """Sign ``envelope`` with the control plane's key."""
    signature = private_key.sign(canonical_json(envelope))
    return SignedJob(envelope=envelope, signature=base64.b64encode(signature).decode())


def parse_signed_job(data: bytes | str) -> SignedJob:
    """Parse a signed job document, mapping any malformed input to :class:`JobSignatureError`."""
    try:
        return SignedJob.model_validate_json(data)
    except ValidationError as exc:
        raise JobSignatureError(f"malformed job: {exc.error_count()} validation error(s)") from exc


def verify_job(signed: SignedJob, public_key: Ed25519PublicKey) -> JobEnvelope:
    """Return the envelope if its signature verifies, else raise :class:`JobSignatureError`."""
    if not signed.signature:
        raise JobSignatureError("job is unsigned")
    try:
        signature = _b64decode(signed.signature, "signature")
        public_key.verify(signature, canonical_json(signed.envelope))
    except (ValueError, InvalidSignature):
        raise JobSignatureError("job signature is invalid") from None
    return signed.envelope
