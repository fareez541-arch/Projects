from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.course import CourseModule
from app.models.progress import UserProgress
from app.schemas.course import ModuleSummary, ModuleDetail, ProgressUpdate
from app.routers.deps import get_current_user

router = APIRouter()


@router.get("/modules")
async def list_modules(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all modules with user's progress status."""
    result = await db.execute(
        select(CourseModule).order_by(CourseModule.module_number)
    )
    modules = result.scalars().all()

    result = await db.execute(
        select(UserProgress).where(UserProgress.user_id == user.id)
    )
    progress_map = {p.module_number: p for p in result.scalars().all()}

    summaries = []
    for mod in modules:
        prog = progress_map.get(mod.module_number)
        completed_count = 0
        if prog and prog.completed_sections:
            completed_count = sum(
                1 for v in prog.completed_sections.values()
                if isinstance(v, dict) and v.get("text")
            )

        summaries.append(ModuleSummary(
            module_number=mod.module_number,
            title=mod.title,
            description=mod.description,
            duration_hours=mod.duration_hours,
            section_count=mod.section_count,
            is_mandatory=mod.is_mandatory,
            status=prog.status if prog else "locked",
            quiz_passed=prog.quiz_passed if prog else None,
            completed_sections_count=completed_count,
        ))

    return summaries


@router.get("/module/{module_number}", response_model=ModuleDetail)
async def get_module(
    module_number: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed module info including syllabus and assets."""
    result = await db.execute(
        select(CourseModule).where(CourseModule.module_number == module_number)
    )
    mod = result.scalar_one_or_none()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")

    result = await db.execute(
        select(UserProgress).where(
            UserProgress.user_id == user.id,
            UserProgress.module_number == module_number,
        )
    )
    progress = result.scalar_one_or_none()
    status = progress.status if progress else "locked"

    # Build sections from syllabus with completion status
    sections = []
    completed_sections = progress.completed_sections if progress else {}
    for section in (mod.syllabus or []):
        sec_num = section.get("section_number", 0)
        sec_completion = completed_sections.get(str(sec_num), {})
        subsections = []
        for sub in section.get("subsections", []):
            subsections.append({
                **sub,
                "completed": sec_completion.get(sub.get("type", "text"), False),
            })
        sections.append({
            "section_number": sec_num,
            "title": section.get("title", ""),
            "subsections": subsections,
        })

    # Asset summaries
    from app.models.course import ContentAsset
    result = await db.execute(
        select(ContentAsset).where(
            ContentAsset.module_number == module_number
        ).order_by(ContentAsset.display_order)
    )
    assets = [
        {
            "id": str(a.id),
            "asset_type": a.asset_type,
            "display_name": a.display_name,
            "display_order": a.display_order,
            "status": a.status,
            "section_number": a.section_number,
        }
        for a in result.scalars().all()
    ]

    return {
        "module_number": mod.module_number,
        "title": mod.title,
        "description": mod.description,
        "duration_hours": mod.duration_hours,
        "sections": sections,
        "status": status,
        "assets": assets,
    }


@router.post("/progress")
async def update_progress(
    req: ProgressUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a section's modality as completed."""
    result = await db.execute(
        select(UserProgress).where(
            UserProgress.user_id == user.id,
            UserProgress.module_number == req.module_number,
        )
    )
    progress = result.scalar_one_or_none()
    if not progress:
        raise HTTPException(status_code=404, detail="Module progress not found")
    if progress.status == "locked":
        raise HTTPException(status_code=403, detail="Module locked")

    # Validate section exists in module syllabus before accepting progress
    result = await db.execute(
        select(CourseModule).where(CourseModule.module_number == req.module_number)
    )
    mod = result.scalar_one_or_none()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")

    # Verify section_number is a valid section in the syllabus
    valid_sections = {s.get("section_number") for s in (mod.syllabus or [])}
    if req.section_number not in valid_sections:
        raise HTTPException(status_code=400, detail="Invalid section number for this module")

    # Update section completion
    completed = dict(progress.completed_sections or {})
    sec_key = str(req.section_number)
    if sec_key not in completed:
        completed[sec_key] = {}
    completed[sec_key][req.modality] = True
    progress.completed_sections = completed

    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(progress, "completed_sections")

    # Update module status
    if progress.status == "available":
        progress.status = "in_progress"
    # Check if all sections completed for module completion
    if mod.syllabus:
        all_done = True
        for section in mod.syllabus:
            sec_num = str(section.get("section_number", 0))
            sec_data = completed.get(sec_num, {})
            # At minimum text must be done
            if not sec_data.get("text"):
                all_done = False
                break
        if all_done and progress.quiz_passed:
            progress.status = "completed"
            # Unlock next module
            next_result = await db.execute(
                select(UserProgress).where(
                    UserProgress.user_id == user.id,
                    UserProgress.module_number == req.module_number + 1,
                )
            )
            next_prog = next_result.scalar_one_or_none()
            if next_prog and next_prog.status == "locked":
                next_prog.status = "available"

    await db.commit()
    return {"status": "updated", "module_status": progress.status}
