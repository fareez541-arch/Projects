import hashlib
from datetime import datetime, timezone

MAX_DEVICES = 2


def compute_server_fingerprint(client_fingerprint: str, user_agent: str, ip: str) -> str:
    """Compute a composite fingerprint mixing client-supplied and server-observed signals.

    The client fingerprint alone is trivially spoofable. Mixing in the User-Agent
    and IP (or CF-Connecting-IP) makes it much harder to generate unlimited
    device slots from the same browser/machine.
    """
    composite = f"{client_fingerprint}|{user_agent}|{ip}"
    return hashlib.sha256(composite.encode()).hexdigest()[:32]


def validate_device(device_slots: list, fingerprint: str) -> tuple[bool, list, str | None]:
    """
    Check if device fingerprint is allowed.
    Returns (allowed, updated_slots, error_message).
    """
    if not device_slots:
        device_slots = []

    # Check if device already registered
    for slot in device_slots:
        if slot["fingerprint"] == fingerprint:
            return True, device_slots, None

    # Check slot availability
    if len(device_slots) >= MAX_DEVICES:
        return False, device_slots, f"Maximum {MAX_DEVICES} devices reached. Contact support to reset."

    # Register new device
    device_slots.append({
        "fingerprint": fingerprint,
        "first_seen": datetime.now(timezone.utc).isoformat(),
        "last_seen": datetime.now(timezone.utc).isoformat(),
    })
    return True, device_slots, None


def update_device_seen(device_slots: list, fingerprint: str) -> list:
    """Update last_seen for a device."""
    for slot in device_slots:
        if slot["fingerprint"] == fingerprint:
            slot["last_seen"] = datetime.now(timezone.utc).isoformat()
            break
    return device_slots
