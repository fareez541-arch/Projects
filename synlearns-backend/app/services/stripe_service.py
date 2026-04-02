import logging

import stripe
from fastapi import Request

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


# Map Stripe price IDs to internal tier numbers.
# Tier 0 = pre-test pending, Tier 1 = Feedback ($79), Tier 2 = Referral ($119), Tier 3 = Full ($149)
def _build_price_tier_map() -> dict[str, int]:
    """Build price_id → tier mapping from config. Skips empty values."""
    mapping: dict[str, int] = {}
    if settings.stripe_price_id_feedback:
        mapping[settings.stripe_price_id_feedback] = 1
    if settings.stripe_price_id_referral:
        mapping[settings.stripe_price_id_referral] = 2
    if settings.stripe_price_id_full:
        mapping[settings.stripe_price_id_full] = 3
    # Backwards compat: legacy single price_id defaults to tier 3 (Full Access)
    if settings.stripe_price_id and settings.stripe_price_id not in mapping:
        mapping[settings.stripe_price_id] = 3
    return mapping


async def verify_webhook(request: Request) -> dict | None:
    """Verify Stripe webhook signature and return event."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        return None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
        return event
    except (ValueError, stripe.error.SignatureVerificationError):
        return None


def resolve_tier_from_session(checkout_session_id: str) -> int:
    """Retrieve line items from Stripe and resolve to a tier number.

    Falls back to tier 3 (Full Access) if the price ID is unknown,
    ensuring no customer is left at tier 0 after paying.
    """
    price_tier_map = _build_price_tier_map()

    try:
        line_items = stripe.checkout.Session.list_line_items(checkout_session_id, limit=5)
        for item in line_items.data:
            price_id = item.price.id if item.price else None
            if price_id and price_id in price_tier_map:
                tier = price_tier_map[price_id]
                logger.info("Resolved price %s → tier %d", price_id, tier)
                return tier
            elif price_id:
                logger.warning("Unknown price ID %s — defaulting to tier 3", price_id)
    except Exception as e:
        logger.error("Failed to retrieve line items for session %s: %s", checkout_session_id, e)

    # Safe default: any paying customer gets full access rather than tier 0.
    # This MUST be investigated — it masks misconfigured price IDs in .env.
    logger.warning(
        "FALLBACK: No price ID matched for session %s — defaulting to tier 3. "
        "Check STRIPE_PRICE_ID_FEEDBACK/REFERRAL/FULL in .env.",
        checkout_session_id,
    )
    return 3


def extract_checkout_data(event: dict) -> dict | None:
    """Extract customer data from checkout.session.completed event."""
    if event["type"] != "checkout.session.completed":
        return None

    session = event["data"]["object"]
    return {
        "email": session.get("customer_details", {}).get("email"),
        "customer_id": session.get("customer"),
        "checkout_session_id": session.get("id"),
        "name": session.get("customer_details", {}).get("name"),
    }
