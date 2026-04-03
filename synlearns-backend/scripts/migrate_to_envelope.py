"""
Migrate encrypted content from single-key format to envelope encryption.

Idempotent: skips rows already in envelope format.
Run: docker compose exec sls-api python scripts/migrate_to_envelope.py

To perform a dry run (no writes):
    docker compose exec sls-api python scripts/migrate_to_envelope.py --dry-run
"""
import asyncio
import hashlib
import sys

from sqlalchemy import select, update
from app.database import async_session
from app.models.course import ContentChunk, ContentAsset
from app.services.content_service import encrypt_content, decrypt_content
from app.services.key_management import is_envelope_format


async def migrate_table(table_cls, label: str, dry_run: bool = False):
    """Migrate all encrypted rows in a table to envelope format."""
    migrated = 0
    skipped = 0
    errors = 0

    async with async_session() as db:
        result = await db.execute(
            select(table_cls).where(table_cls.encrypted_content.isnot(None))
        )
        rows = result.scalars().all()
        total = len(rows)
        print(f"\n[{label}] Found {total} encrypted rows")

        for i, row in enumerate(rows):
            try:
                if is_envelope_format(row.encrypted_content):
                    skipped += 1
                    continue

                # Decrypt with legacy format (auto-detected)
                plaintext = decrypt_content(row.encrypted_content)

                # Verify hash if available
                if hasattr(row, "content_hash") and row.content_hash:
                    actual_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
                    if actual_hash != row.content_hash:
                        print(f"  HASH MISMATCH on {row.id} — skipping")
                        errors += 1
                        continue

                # Re-encrypt with envelope format
                envelope, content_hash = encrypt_content(plaintext)

                if not dry_run:
                    await db.execute(
                        update(table_cls)
                        .where(table_cls.id == row.id)
                        .values(encrypted_content=envelope, content_hash=content_hash)
                    )

                migrated += 1

                if (i + 1) % 50 == 0:
                    if not dry_run:
                        await db.commit()
                    print(f"  Progress: {i + 1}/{total}")

            except Exception as e:
                print(f"  ERROR on {row.id}: {e}")
                errors += 1

        if not dry_run:
            await db.commit()

    print(f"  Migrated: {migrated} | Skipped (already envelope): {skipped} | Errors: {errors}")
    return migrated, skipped, errors


async def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN — no writes will be performed ===")

    print("=== Envelope Encryption Migration ===")

    m1, s1, e1 = await migrate_table(ContentChunk, "ContentChunks", dry_run)
    m2, s2, e2 = await migrate_table(ContentAsset, "ContentAssets", dry_run)

    total_migrated = m1 + m2
    total_skipped = s1 + s2
    total_errors = e1 + e2

    print(f"\n=== Migration Complete ===")
    print(f"Total migrated: {total_migrated}")
    print(f"Total skipped:  {total_skipped}")
    print(f"Total errors:   {total_errors}")

    if total_errors > 0:
        print("\nWARNING: Some rows failed migration. Investigate before removing legacy support.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
