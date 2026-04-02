from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.course import ContentChunk, ContentAsset
from app.models.progress import UserProgress
from app.services.content_service import decrypt_content
from app.services.watermark import inject_watermark
from app.routers.deps import get_current_user
from app.schemas.course import ContentChunkResponse

router = APIRouter()


@router.get("/chunk/{module_number}/{section_number}/{subsection_number}")
async def get_content_chunk(
    module_number: int,
    section_number: int,
    subsection_number: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serve decrypted, watermarked content for a specific subsection."""
    # Check user has access to this module
    result = await db.execute(
        select(UserProgress).where(
            UserProgress.user_id == user.id,
            UserProgress.module_number == module_number,
        )
    )
    progress = result.scalar_one_or_none()
    if not progress or progress.status == "locked":
        raise HTTPException(status_code=403, detail="Module locked")

    # Load chunks for this subsection
    result = await db.execute(
        select(ContentChunk).where(
            ContentChunk.module_number == module_number,
            ContentChunk.section_number == section_number,
            ContentChunk.subsection_number == subsection_number,
        ).order_by(ContentChunk.chunk_order)
    )
    chunks = result.scalars().all()
    if not chunks:
        raise HTTPException(status_code=404, detail="Content not found")

    # Load inline assets for this section
    result = await db.execute(
        select(ContentAsset).where(
            ContentAsset.module_number == module_number,
            ContentAsset.section_number == section_number,
            ContentAsset.status == "available",
        ).order_by(ContentAsset.display_order)
    )
    assets = result.scalars().all()

    # Decrypt and watermark each chunk
    content_parts = []
    for chunk in chunks:
        html = decrypt_content(chunk.encrypted_content)
        html = inject_watermark(html, str(user.id))
        content_parts.append(html)

    combined_html = "\n".join(content_parts)

    inline_assets = [
        {
            "id": str(a.id),
            "type": a.asset_type,
            "display_name": a.display_name,
            "order": a.display_order,
        }
        for a in assets
    ]

    response = ContentChunkResponse(
        module_number=module_number,
        section_number=section_number,
        subsection_number=subsection_number,
        chunk_order=1,
        title=chunks[0].title if chunks else None,
        html_content=combined_html,
        inline_assets=inline_assets,
    )

    return Response(
        content=response.model_dump_json(),
        media_type="application/json",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@router.get("/asset/{asset_id}")
async def get_asset(
    asset_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Serve decrypted, watermarked asset (SVG/infographic)."""
    result = await db.execute(
        select(ContentAsset).where(ContentAsset.id == asset_id)
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Check module access BEFORE revealing asset status
    result = await db.execute(
        select(UserProgress).where(
            UserProgress.user_id == user.id,
            UserProgress.module_number == asset.module_number,
        )
    )
    progress = result.scalar_one_or_none()
    if not progress or progress.status == "locked":
        raise HTTPException(status_code=403, detail="Module locked")

    if asset.status == "coming_soon":
        return Response(
            content='{"status": "coming_soon", "message": "This content is in production."}',
            media_type="application/json",
        )

    if asset.encrypted_content:
        content = decrypt_content(asset.encrypted_content)
        # Inject watermark into SVG text elements
        if asset.asset_type == "svg":
            watermark_text = f'<!-- uid:{str(user.id)[:8]} -->'
            content = content.replace("</svg>", f"{watermark_text}</svg>")
        content = inject_watermark(content, str(user.id))
    else:
        content = ""

    media_type = "image/svg+xml" if asset.asset_type == "svg" else "text/html"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/gallery/{module_number}")
async def get_module_gallery(
    module_number: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get all assets for a module as a gallery listing."""
    result = await db.execute(
        select(UserProgress).where(
            UserProgress.user_id == user.id,
            UserProgress.module_number == module_number,
        )
    )
    progress = result.scalar_one_or_none()
    if not progress or progress.status == "locked":
        raise HTTPException(status_code=403, detail="Module locked")

    result = await db.execute(
        select(ContentAsset).where(
            ContentAsset.module_number == module_number,
        ).order_by(ContentAsset.section_number, ContentAsset.display_order)
    )
    assets = result.scalars().all()

    return [
        {
            "id": str(a.id),
            "asset_type": a.asset_type,
            "display_name": a.display_name,
            "section_number": a.section_number,
            "display_order": a.display_order,
            "status": a.status,
        }
        for a in assets
    ]
