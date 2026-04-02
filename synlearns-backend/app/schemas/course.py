from pydantic import BaseModel
from uuid import UUID


class ModuleSummary(BaseModel):
    module_number: int
    title: str
    description: str | None
    duration_hours: float | None
    section_count: int
    is_mandatory: bool
    status: str  # locked, available, in_progress, completed, skipped
    quiz_passed: bool | None = None
    completed_sections_count: int = 0

    class Config:
        from_attributes = True


class SectionDetail(BaseModel):
    section_number: int
    title: str
    subsections: list[dict]  # [{title, duration_min, type, completed}]


class ModuleDetail(BaseModel):
    module_number: int
    title: str
    description: str | None
    duration_hours: float | None
    sections: list[SectionDetail]
    status: str
    assets: list["AssetSummary"]


class AssetSummary(BaseModel):
    id: UUID
    asset_type: str
    display_name: str | None
    display_order: int
    status: str
    section_number: int | None

    class Config:
        from_attributes = True


class ContentChunkResponse(BaseModel):
    """Server-decrypted, watermarked HTML content"""
    module_number: int
    section_number: int
    subsection_number: int
    chunk_order: int
    title: str | None
    html_content: str  # decrypted + watermarked
    inline_assets: list[dict]  # [{id, type, display_name, order}]


from typing import Literal


class ProgressUpdate(BaseModel):
    module_number: int
    section_number: int
    modality: Literal["text", "speech", "video"]
