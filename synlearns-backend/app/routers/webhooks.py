from fastapi import APIRouter, Request, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.database import get_db
from app.models.user import User
from app.services.stripe_service import verify_webhook, extract_checkout_data, resolve_tier_from_session
from app.services.auth_service import generate_activation_token
from app.services.email_service import send_checkout_email

router = APIRouter()


@router.post("/stripe")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    event = await verify_webhook(request)
    if not event:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        data = extract_checkout_data(event)
        if not data or not data["email"]:
            raise HTTPException(status_code=400, detail="No customer email")

        # Idempotency: reject duplicate events by checkout session ID
        dup_check = await db.execute(
            select(User).where(User.stripe_checkout_session_id == data["checkout_session_id"])
        )
        if dup_check.scalar_one_or_none():
            return {"status": "duplicate_event_ignored"}

        # Check if user already exists
        result = await db.execute(select(User).where(User.email == data["email"]))
        existing = result.scalar_one_or_none()

        # Resolve tier from Stripe line items (maps price ID → tier 1/2/3)
        tier = resolve_tier_from_session(data["checkout_session_id"])

        if existing:
            if existing.account_status == "expired":
                # Re-purchase or renewal — reset expired account
                token = generate_activation_token()
                existing.activation_token = token
                existing.account_status = "pending"
                existing.tier = tier
                existing.stripe_customer_id = data["customer_id"]
                existing.stripe_checkout_session_id = data["checkout_session_id"]
                await db.commit()
                send_checkout_email(data["email"], token, data.get("name"))
            elif existing.account_status == "active" and tier > existing.tier:
                # Active user upgrading tier — apply immediately
                existing.tier = tier
                existing.stripe_customer_id = data["customer_id"]
                existing.stripe_checkout_session_id = data["checkout_session_id"]
                await db.commit()
            return {"status": "existing_user_updated"}

        # Create new user
        token = generate_activation_token()
        user = User(
            email=data["email"],
            stripe_customer_id=data["customer_id"],
            stripe_checkout_session_id=data["checkout_session_id"],
            activation_token=token,
            account_status="pending",
            tier=tier,
            device_slots=[],
            fm_profile={},
        )
        db.add(user)
        await db.commit()

        # Send activation email
        send_checkout_email(data["email"], token, data.get("name"))

        return {"status": "user_created"}

    return {"status": "ignored"}
