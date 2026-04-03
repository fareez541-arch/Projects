"""
Envelope encryption key management.

DEKs (Data Encryption Keys) are per-content AES-256 keys.
The MEK (Master Encryption Key) wraps/unwraps DEKs.
Wrapped DEKs are stored inline with ciphertext — no separate key table needed.

Wire format v2:
    0x02 || wrapped_dek_len(2B BE) || wrapped_dek || nonce(12B) || ciphertext

wrapped_dek format:
    wrap_nonce(12B) || aesgcm_encrypt(mek, dek)
"""
import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Envelope format version byte
ENVELOPE_VERSION = 0x02
NONCE_SIZE = 12
DEK_SIZE = 32  # AES-256
AES_GCM_TAG_SIZE = 16


def generate_dek() -> bytes:
    """Generate a random 256-bit Data Encryption Key."""
    return os.urandom(DEK_SIZE)


def wrap_dek(mek: bytes, dek: bytes) -> bytes:
    """
    Encrypt a DEK with the Master Encryption Key.
    Returns: wrap_nonce(12B) || aesgcm(mek, dek)
    """
    aesgcm = AESGCM(mek)
    wrap_nonce = os.urandom(NONCE_SIZE)
    wrapped = aesgcm.encrypt(wrap_nonce, dek, None)
    return wrap_nonce + wrapped


def unwrap_dek(mek: bytes, wrapped_dek: bytes) -> bytes:
    """
    Decrypt a wrapped DEK using the Master Encryption Key.
    Input: wrap_nonce(12B) || aesgcm(mek, dek)
    Returns: raw DEK bytes (32B)
    """
    aesgcm = AESGCM(mek)
    wrap_nonce = wrapped_dek[:NONCE_SIZE]
    ciphertext = wrapped_dek[NONCE_SIZE:]
    return aesgcm.decrypt(wrap_nonce, ciphertext, None)


def is_envelope_format(data: bytes) -> bool:
    """Check if encrypted data uses envelope format (version byte 0x02)."""
    return len(data) > 3 and data[0] == ENVELOPE_VERSION


def pack_envelope(wrapped_dek: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    """
    Pack envelope format:
    version(1B) || wrapped_dek_len(2B BE) || wrapped_dek || nonce(12B) || ciphertext
    """
    dek_len = len(wrapped_dek)
    return (
        bytes([ENVELOPE_VERSION])
        + struct.pack(">H", dek_len)
        + wrapped_dek
        + nonce
        + ciphertext
    )


def unpack_envelope(data: bytes) -> tuple[bytes, bytes, bytes]:
    """
    Unpack envelope format.
    Returns: (wrapped_dek, nonce, ciphertext)
    Raises ValueError if format is invalid.
    """
    if len(data) < 3:
        raise ValueError("Data too short for envelope format")
    if data[0] != ENVELOPE_VERSION:
        raise ValueError(f"Unknown envelope version: {data[0]:#x}")

    dek_len = struct.unpack(">H", data[1:3])[0]
    offset = 3

    if len(data) < offset + dek_len + NONCE_SIZE + AES_GCM_TAG_SIZE:
        raise ValueError("Data too short for declared wrapped DEK length")

    wrapped_dek = data[offset : offset + dek_len]
    offset += dek_len

    nonce = data[offset : offset + NONCE_SIZE]
    offset += NONCE_SIZE

    ciphertext = data[offset:]

    return wrapped_dek, nonce, ciphertext
