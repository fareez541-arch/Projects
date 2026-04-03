"""
Create admin user.
Run: docker compose exec sls-api python scripts/seed_admin.py
"""
import asyncio
import secrets
import string
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from app.database import async_session
from app.models.user import User
from app.services.auth_service import hash_password
from app.config import get_settings

settings = get_settings()


async def seed():
    async with async_session() as db:
        result = await db.execute(
            select(User).where(User.email == settings.admin_email)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.is_admin = True
            await db.commit()
            print(f"Updated {settings.admin_email} to admin")
            return

        generated_pw = ''.join(secrets.choice(string.ascii_letters + string.digits + "!@#$%") for _ in range(24))
        admin = User(
            email=settings.admin_email,
            password_hash=hash_password(generated_pw),
            account_status="active",
            is_admin=True,
            tier=2,
            activated_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
            device_slots=[],
            fm_profile={},
        )
        db.add(admin)
        await db.commit()
        print(f"Created admin: {settings.admin_email}")
        print(f"PASSWORD: {generated_pw}")
        print("Save this password NOW — it will not be shown again.")


if __name__ == "__main__":
    asyncio.run(seed())
