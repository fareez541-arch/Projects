from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.session import UserSession
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, RefreshRequest, UserPublic
from app.services import auth_service, device_service
from app.routers.deps import get_current_user

router = APIRouter()


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Activate account after Stripe payment. Sets password, issues JWT."""
    result = await db.execute(
        select(User).where(User.activation_token == req.activation_token)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid activation token")
    if user.account_status != "pending":
        raise HTTPException(status_code=400, detail="Account already activated")

    # Validate device
    allowed, updated_slots, err = device_service.validate_device(
        user.device_slots or [], req.device_fingerprint
    )
    if not allowed:
        raise HTTPException(status_code=403, detail=err)

    # Activate account
    user.password_hash = auth_service.hash_password(req.password)
    user.first_name = req.first_name
    user.last_name = req.last_name
    user.account_status = "active"
    user.activation_token = None
    user.activated_at = datetime.now(timezone.utc)
    user.expires_at = datetime.now(timezone.utc) + timedelta(days=180)
    from sqlalchemy.orm.attributes import flag_modified
    user.device_slots = updated_slots
    flag_modified(user, "device_slots")

    # Create tokens
    access_token, jti, access_expires = auth_service.create_access_token(
        str(user.id), user.is_admin
    )
    refresh_token, refresh_expires = auth_service.create_refresh_token(str(user.id))

    # Create session
    session = UserSession(
        user_id=user.id,
        device_fingerprint=req.device_fingerprint,
        access_token_jti=jti,
        refresh_token_hash=auth_service.hash_password(refresh_token),
        expires_at=refresh_expires,
    )
    db.add(session)
    await db.commit()
    await db.refresh(user)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=get_settings().jwt_access_expire_minutes * 60,
        user=UserPublic.model_validate(user),
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not auth_service.verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check account status
    if user.account_status == "expired":
        raise HTTPException(status_code=403, detail="Account expired")
    if user.account_status == "suspended":
        raise HTTPException(status_code=403, detail="Account suspended")
    if user.account_status != "active":
        raise HTTPException(status_code=403, detail="Account not activated")

    # Check expiry
    if user.expires_at and user.expires_at < datetime.now(timezone.utc):
        user.account_status = "expired"
        await db.commit()
        raise HTTPException(status_code=403, detail="Account expired")

    # Device check
    allowed, updated_slots, err = device_service.validate_device(
        user.device_slots or [], req.device_fingerprint
    )
    if not allowed:
        raise HTTPException(status_code=403, detail=err)

    from sqlalchemy.orm.attributes import flag_modified
    user.device_slots = updated_slots
    flag_modified(user, "device_slots")

    # Invalidate old session on same device
    result = await db.execute(
        select(UserSession).where(
            UserSession.user_id == user.id,
            UserSession.device_fingerprint == req.device_fingerprint,
            UserSession.is_active == True,
        )
    )
    old_sessions = result.scalars().all()
    for s in old_sessions:
        s.is_active = False

    # Create new tokens
    access_token, jti, access_expires = auth_service.create_access_token(
        str(user.id), user.is_admin
    )
    refresh_token, refresh_expires = auth_service.create_refresh_token(str(user.id))

    session = UserSession(
        user_id=user.id,
        device_fingerprint=req.device_fingerprint,
        access_token_jti=jti,
        refresh_token_hash=auth_service.hash_password(refresh_token),
        expires_at=refresh_expires,
    )
    db.add(session)

    # Update device last_seen
    user.device_slots = device_service.update_device_seen(user.device_slots, req.device_fingerprint)
    await db.commit()
    await db.refresh(user)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=get_settings().jwt_access_expire_minutes * 60,
        user=UserPublic.model_validate(user),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = auth_service.decode_token(req.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload["sub"]
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user or user.account_status != "active":
        raise HTTPException(status_code=401, detail="Invalid session")

    # Validate refresh token hash against active session for this device
    result = await db.execute(
        select(UserSession).where(
            UserSession.user_id == user.id,
            UserSession.device_fingerprint == req.device_fingerprint,
            UserSession.is_active == True,
        )
    )
    active_sessions = result.scalars().all()

    # Verify the incoming refresh token matches at least one active session's hash
    token_valid = False
    for s in active_sessions:
        if s.refresh_token_hash and auth_service.verify_password(req.refresh_token, s.refresh_token_hash):
            token_valid = True
            break

    if not token_valid:
        # Token doesn't match any active session — possible token reuse attack
        # Invalidate ALL sessions for this user as a precaution
        all_result = await db.execute(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.is_active == True,
            )
        )
        for s in all_result.scalars().all():
            s.is_active = False
        await db.commit()
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    # Create new access token
    access_token, jti, access_expires = auth_service.create_access_token(
        str(user.id), user.is_admin
    )
    refresh_token, refresh_expires = auth_service.create_refresh_token(str(user.id))

    # Invalidate old sessions for this device, create new
    for s in active_sessions:
        s.is_active = False

    session = UserSession(
        user_id=user.id,
        device_fingerprint=req.device_fingerprint,
        access_token_jti=jti,
        refresh_token_hash=auth_service.hash_password(refresh_token),
        expires_at=refresh_expires,
    )
    db.add(session)
    await db.commit()
    await db.refresh(user)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=get_settings().jwt_access_expire_minutes * 60,
        user=UserPublic.model_validate(user),
    )


@router.post("/logout")
async def logout(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invalidate all active sessions for the current user."""
    result = await db.execute(
        select(UserSession).where(
            UserSession.user_id == user.id,
            UserSession.is_active == True,
        )
    )
    for session in result.scalars().all():
        session.is_active = False
    await db.commit()
    return {"status": "logged_out"}


@router.get("/me", response_model=UserPublic)
async def me(user: User = Depends(get_current_user)):
    return UserPublic.model_validate(user)


# Import here to avoid circular
from app.config import get_settings
