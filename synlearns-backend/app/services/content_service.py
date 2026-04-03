"""
Content encryption/decryption using AES-256-GCM with envelope encryption.

Each content item is encrypted with a unique DEK (Data Encryption Key).
The DEK is wrapped (encrypted) by the MEK (Master Encryption Key) from env.
Legacy single-key format is auto-detected and decrypted transparently.
"""
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings
from app.services.key_management import (
    generate_dek,
    wrap_dek,
    unwrap_dek,
    is_envelope_format,
    pack_envelope,
    unpack_envelope,
    NONCE_SIZE,
)

settings = get_settings()


def _get_mek() -> bytes:
    """Get the Master Encryption Key from environment."""
    return bytes.fromhex(settings.content_encryption_key)


def encrypt_content(plaintext: str) -> tuple[bytes, str]:
    """
    Encrypt content with per-item DEK wrapped by MEK (envelope encryption).

    Returns (encrypted_bytes, content_hash).
    encrypted_bytes uses envelope wire format:
        version(1B) || wrapped_dek_len(2B) || wrapped_dek || nonce(12B) || ciphertext
    """
    mek = _get_mek()

    # Generate unique DEK for this content item
    dek = generate_dek()

    # Encrypt content with DEK
    aesgcm = AESGCM(dek)
    nonce = os.urandom(NONCE_SIZE)
    plaintext_bytes = plaintext.encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext_bytes, None)

    # Wrap DEK with MEK
    wrapped_dek = wrap_dek(mek, dek)

    # Pack into envelope format
    envelope = pack_envelope(wrapped_dek, nonce, ciphertext)

    content_hash = hashlib.sha256(plaintext_bytes).hexdigest()
    return envelope, content_hash


def decrypt_content(encrypted: bytes) -> str:
    """
    Decrypt content — auto-detects envelope vs legacy format.

    Envelope format: unwrap DEK with MEK, decrypt content with DEK.
    Legacy format: decrypt directly with MEK (backward compatible).
    """
    mek = _get_mek()

    if is_envelope_format(encrypted):
        # Envelope format: unwrap DEK, then decrypt
        wrapped_dek, nonce, ciphertext = unpack_envelope(encrypted)
        dek = unwrap_dek(mek, wrapped_dek)
        aesgcm = AESGCM(dek)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    else:
        # Legacy format: nonce(12B) || ciphertext, encrypted with MEK directly
        nonce = encrypted[:NONCE_SIZE]
        ciphertext = encrypted[NONCE_SIZE:]
        aesgcm = AESGCM(mek)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)

    return plaintext.decode("utf-8")
