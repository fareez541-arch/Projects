from typing import Literal

from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime


class StartAssessmentRequest(BaseModel):
    assessment_type: Literal["pretest", "posttest", "practice", "module_quiz"]
    module_number: int | None = None  # required for module_quiz


class AnswerRequest(BaseModel):
    session_id: UUID
    question_id: str = Field(max_length=255)
    selected_answer: str = Field(max_length=2048)
    response_time_ms: int = Field(ge=0, le=600_000)  # max 10 minutes
    click_history: list[dict] = Field(default=[], max_length=100)


class QuestionResponse(BaseModel):
    question_id: str
    stem: str
    choices: list[str]
    domain: str
    difficulty: str
    question_number: int
    total_questions: int


class AnswerResponse(BaseModel):
    correct: bool
    correct_answer: str
    rationale: str
    distractors_analysis: list[dict] | None = None  # only for student breakdown
    next_question: QuestionResponse | None = None
    assessment_complete: bool = False


class AssessmentResult(BaseModel):
    session_id: UUID
    assessment_type: str
    score: int
    total_questions: int
    percentage: float
    tier_assigned: int | None = None

    # Student-facing breakdown
    score_by_domain: dict
    score_by_difficulty: dict
    time_per_question_avg_ms: float
    total_time_ms: int

    # Benchmark summary
    strengths: list[str]
    weaknesses: list[str]
    fm_critical: list[str]  # FMs that need mandatory remediation

    completed_at: datetime


class AdminAssessmentResult(AssessmentResult):
    """Full telemetry for admin view"""
    answers: list[dict]
    band_history: list[dict]
    fm_profile: dict
    click_history_summary: dict  # aggregated click patterns
    time_per_question: list[dict]  # [{question_id, time_ms}]
