# SLS-003: Content Envelope Encryption Architecture

## Status: IMPLEMENTATION COMPLETE

## Problem

All encrypted content (lesson chunks, SVGs, infographics) uses a single AES-256-GCM key
derived from `CONTENT_ENCRYPTION_KEY` environment variable. A single key compromise exposes
100% of course IP simultaneously.

### Current State (Pre-Migration)

| Component | Value |
|-----------|-------|
| Algorithm | AES-256-GCM |
| Key source | `CONTENT_ENCRYPTION_KEY` env var, hex-encoded |
| Key count | 1 (global) |
| Key storage | Environment variable only |
| Wire format | `nonce(12B) \|\| ciphertext` |
| Encrypted data | `content_chunks.encrypted_content`, `content_assets.encrypted_content` |
| Rotation capability | None — requires full re-encryption of all content |

### Threat Model

| Attack Vector | Impact with Single Key |
|---------------|----------------------|
| Env var leak (logging, crash dump, container escape) | Total IP loss |
| DB dump exfiltration | Attacker needs 1 key to decrypt everything |
| Insider with env access | Full decryption capability |
| Key rotation | Requires downtime + full re-encryption |

## Design: Envelope Encryption

### Architecture

```
Master Key (MEK)                    — env-injected, never touches DB
    |
    +-- wraps --> DEK_1  (content_chunks module 1-3)
    +-- wraps --> DEK_2  (content_chunks module 4-6)
    +-- wraps --> DEK_N  (per-content or per-module)
    +-- wraps --> DEK_A  (content_assets batch)
```

Each piece of content is encrypted with its own Data Encryption Key (DEK).
The DEK is itself encrypted (wrapped) by the Master Encryption Key (MEK).
The wrapped DEK is stored alongside the ciphertext.

### Wire Format (Post-Migration)

```
Version(1B) || wrapped_dek_len(2B) || wrapped_dek(var) || nonce(12B) || ciphertext
```

- **Version byte**: `0x02` for envelope format (`0x01` reserved for legacy detection)
- **wrapped_dek_len**: uint16 big-endian length of the wrapped DEK blob
- **wrapped_dek**: DEK encrypted with MEK using AES-256-GCM (nonce + ciphertext)
- **nonce**: 12-byte random nonce for content encryption
- **ciphertext**: AES-256-GCM encrypted content

### Legacy Format Detection

Legacy content has no version prefix. Since AES-GCM nonces are random bytes,
the probability of the first byte being exactly `0x02` is ~0.4%. We use an
additional heuristic: legacy format is exactly `nonce(12B) + ciphertext`, so
the total length minus 12 must be a valid AES-GCM ciphertext (plaintext + 16B tag).

The system detects format by checking the first byte:
- `0x02` → envelope format, parse wrapped DEK
- anything else → legacy single-key format, decrypt with MEK directly

### Key Rotation

1. Generate new MEK (MEK_v2)
2. Set `CONTENT_ENCRYPTION_KEY_V2` env var
3. Run migration: for each content row, decrypt DEK with MEK_v1, re-wrap with MEK_v2
4. DEKs never change — only the wrapping changes
5. Swap env vars: `CONTENT_ENCRYPTION_KEY` = MEK_v2
6. Remove `_V2` env var

This re-wraps DEKs without touching content ciphertext. O(rows) fast DB updates,
no content re-encryption needed.

### Migration Path (Single-Key to Envelope)

1. Deploy code with envelope support + legacy fallback (reads both formats)
2. Run `scripts/migrate_to_envelope.py` — re-encrypts each row:
   - Decrypt with legacy single key
   - Generate fresh DEK
   - Encrypt content with DEK
   - Wrap DEK with MEK
   - Write envelope format
3. Migration is idempotent — skips rows already in envelope format
4. Rollback: legacy decrypt path remains functional indefinitely

### Components

| File | Purpose |
|------|---------|
| `app/services/key_management.py` | DEK generation, wrapping, unwrapping, format detection |
| `app/services/content_service.py` | Updated encrypt/decrypt using envelope format |
| `scripts/migrate_to_envelope.py` | Batch migration from single-key to envelope |

### Security Properties

| Property | Mechanism |
|----------|-----------|
| Key isolation | Each content item has unique DEK |
| Key compromise blast radius | 1 DEK = 1 content item |
| MEK rotation | Re-wrap DEKs only, O(rows) fast updates |
| No plaintext keys in DB | DEKs stored only in wrapped form within ciphertext |
| Forward secrecy on rotation | Old MEK cannot unwrap new DEK wrappings |
| Backward compat | Legacy format auto-detected and decrypted |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `CONTENT_ENCRYPTION_KEY` | Master Encryption Key, 64-char hex (256-bit) |
| `CONTENT_ENCRYPTION_KEY_V2` | Rotation target key (temporary, during rotation only) |
