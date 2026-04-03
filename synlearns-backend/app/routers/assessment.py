import random
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.question import Question
from app.models.assessment import AssessmentSession
from app.models.progress import UserProgress
from app.schemas.assessment import (
    StartAssessmentRequest,
    AnswerRequest,
    QuestionResponse,
    AnswerResponse,
    AssessmentResult,
)
from app.services import assessment_engine
from app.services.email_service import send_benchmark_email
from app.routers.deps import get_current_user

router = APIRouter()


def _sanitize_rationale(text: str) -> str:
    """Strip internal formula gate references from student-facing rationale."""
    import re
    # Remove gate references like C1_Corrected, C2, C3_New, C4_Corrected, OR gate, AND gate
    text = re.sub(r'\b[Cc]\d+(?:_(?:Corrected|New|Raw))?\b', '', text)
    # Remove Φ formula IDs like ΦACS, ΦStroke
    text = re.sub(r'Φ\w+', '', text)
    # Remove gate terminology
    text = re.sub(r'\b(?:mandatory|OR|AND)\s+gate\b', '', text, flags=re.IGNORECASE)
    # Remove "the ___ gate is positive/met/satisfied"
    text = re.sub(r'the\s+gate\s+is\s+(?:positive|met|satisfied|negative)', '', text, flags=re.IGNORECASE)
    # Remove "satisfying the ... gate"
    text = re.sub(r'satisfying\s+the\s+[\w\s]*gate', '', text, flags=re.IGNORECASE)
    # Remove "the mandatory ... gate"
    text = re.sub(r'the\s+mandatory\s+[\w_]*\s*gate', 'the diagnostic criteria', text, flags=re.IGNORECASE)
    # Clean up orphaned parens, extra spaces, double periods
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'\.\s*\.', '.', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\s+([,.])', r'\1', text)
    return text.strip()


def _sanitize_distractor(dist: dict) -> dict:
    """Clean internal FM codes from student-facing distractor analysis."""
    fm = dist.get("failure_mode", "")
    ft = dist.get("failure_type", "")
    remediation = dist.get("remediation", "") or ""

    # Map failure_type codes to readable labels
    type_labels = {
        "confounder_blindness": "Missed a key differentiating factor",
        "threshold_confusion": "Applied the wrong threshold or cutoff",
        "subtype_collapse": "Confused two conditions with overlapping features",
        "temporal_error": "Misread the timeline or sequence of events",
        "anchoring_bias": "Fixated on one finding and missed the full picture",
        "omission_error": "Missed a required step or criterion",
        "priority_inversion": "Chose a correct action but not the most urgent one",
    }

    return {
        "failure_mode": type_labels.get(ft, ft.replace("_", " ").title() if ft else "Clinical reasoning error"),
        "failure_type": None,
        "remediation": _sanitize_rationale(remediation),
    }


def _question_to_dict(q: Question) -> dict:
    return {
        "question_id": q.question_id,
        "formula_id": q.formula_id,
        "domain": q.domain,
        "subdomain": q.subdomain,
        "difficulty": q.difficulty,
        "module_number": q.module_number,
        "stem": q.stem,
        "correct_answer": q.correct_answer,
        "correct_rationale": q.correct_rationale,
        "gates_tested": q.gates_tested,
        "distractors": q.distractors,
        "fm_tags": q.fm_tags or [],
    }


def _sanitize_answer_text(text: str) -> str:
    """Strip formula notation like (C1=1), (C2=0) from answer/distractor text."""
    import re
    text = re.sub(r'\s*\([A-Z]\d+(?:_\w+)?=\d\)', '', text)
    text = re.sub(r'\b[Cc]\d+(?:_(?:Corrected|New|Raw))?(?:=\d)?\b', '', text)
    text = re.sub(r'Φ\w+', '', text)
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'\s+([,.])', r'\1', text)
    return text.strip()


def _make_choices(q_dict: dict) -> list[str]:
    """Create shuffled answer choices with formula notation stripped."""
    choices = [_sanitize_answer_text(q_dict["correct_answer"])]
    for d in q_dict["distractors"]:
        raw = d["text"] if isinstance(d, dict) else d
        choices.append(_sanitize_answer_text(raw))
    random.shuffle(choices)
    return choices


@router.post("/start")
async def start_assessment(
    req: StartAssessmentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Load all questions
    result = await db.execute(select(Question))
    all_questions = [_question_to_dict(q) for q in result.scalars().all()]

    # Get previously used question IDs for this user
    result = await db.execute(
        select(AssessmentSession).where(AssessmentSession.user_id == user.id)
    )
    prev_sessions = result.scalars().all()
    used_ids = set()
    for s in prev_sessions:
        used_ids.update(s.question_ids or [])

    fm_profile = user.fm_profile or {}

    # Generate questions based on type
    if req.assessment_type == "pretest":
        questions = assessment_engine.generate_pretest_questions(all_questions)
    elif req.assessment_type == "posttest":
        questions = assessment_engine.generate_posttest_questions(all_questions, fm_profile, used_ids)
    elif req.assessment_type == "module_quiz":
        if req.module_number is None:
            raise HTTPException(status_code=400, detail="module_number required for module_quiz")
        questions = assessment_engine.generate_module_quiz(all_questions, req.module_number, fm_profile, used_ids)
    elif req.assessment_type == "practice":
        # Practice mode: adaptive from full bank
        available = [q for q in all_questions if q["question_id"] not in used_ids]
        questions = assessment_engine._select_adaptive(available, 25, "medium", fm_profile)
    else:
        raise HTTPException(status_code=400, detail="Invalid assessment_type")

    if not questions:
        raise HTTPException(status_code=404, detail="No questions available")

    q_ids = [q["question_id"] for q in questions]

    # Create session
    session = AssessmentSession(
        user_id=user.id,
        assessment_type=req.assessment_type,
        module_number=req.module_number,
        question_ids=q_ids,
        total_questions=len(questions),
        current_band="medium" if req.assessment_type != "pretest" else "easy",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    # Return first question
    first_q = questions[0]
    return {
        "session_id": session.id,
        "total_questions": len(questions),
        "question": QuestionResponse(
            question_id=first_q["question_id"],
            stem=first_q["stem"],
            choices=_make_choices(first_q),
            domain=first_q["domain"],
            difficulty=first_q["difficulty"],
            question_number=1,
            total_questions=len(questions),
        ),
    }


@router.post("/answer", response_model=AnswerResponse)
async def submit_answer(
    req: AnswerRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Load session
    result = await db.execute(
        select(AssessmentSession).where(
            AssessmentSession.id == req.session_id,
            AssessmentSession.user_id == user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Assessment session not found")
    if session.status != "in_progress":
        raise HTTPException(status_code=400, detail="Assessment already completed")

    # Load the current question
    result = await db.execute(
        select(Question).where(Question.question_id == req.question_id)
    )
    question = result.scalar_one_or_none()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    q_dict = _question_to_dict(question)
    correct = req.selected_answer == _sanitize_answer_text(q_dict["correct_answer"])

    # Compute server-side response time
    # For the first answer, measure from session start. For subsequent,
    # measure from previous answer's server timestamp.
    now = datetime.now(timezone.utc)
    existing_answers = list(session.answers or [])
    if existing_answers and existing_answers[-1].get("server_timestamp"):
        prev_ts = datetime.fromisoformat(existing_answers[-1]["server_timestamp"])
        server_response_ms = int((now - prev_ts).total_seconds() * 1000)
    else:
        server_response_ms = int((now - session.started_at).total_seconds() * 1000)

    # Build answer record with telemetry — both client and server timing
    answer_record = {
        "question_id": req.question_id,
        "selected_answer": req.selected_answer,
        "correct": correct,
        "domain": q_dict["domain"],
        "difficulty": q_dict["difficulty"],
        "response_time_ms": req.response_time_ms,
        "server_response_ms": server_response_ms,
        "click_history": req.click_history,
        "timestamp": now.isoformat(),
        "server_timestamp": now.isoformat(),
    }

    answers = list(session.answers or [])
    answers.append(answer_record)
    session.answers = answers
    session.score = sum(1 for a in answers if a.get("correct"))
    session.current_index = len(answers)

    # Update score breakdowns
    score_by_diff = dict(session.score_by_difficulty or {})
    d = q_dict["difficulty"]
    if d not in score_by_diff:
        score_by_diff[d] = {"correct": 0, "total": 0}
    score_by_diff[d]["total"] += 1
    if correct:
        score_by_diff[d]["correct"] += 1
    session.score_by_difficulty = score_by_diff

    score_by_dom = dict(session.score_by_domain or {})
    dom = q_dict["domain"]
    if dom not in score_by_dom:
        score_by_dom[dom] = {"correct": 0, "total": 0}
    score_by_dom[dom]["total"] += 1
    if correct:
        score_by_dom[dom]["correct"] += 1
    session.score_by_domain = score_by_dom

    # Update FM profile
    fm_profile = assessment_engine.update_fm_profile(
        dict(user.fm_profile or {}), q_dict, correct, req.selected_answer
    )
    user.fm_profile = fm_profile
    session.fm_profile = fm_profile

    # Check band transition (every 5 questions, for adaptive types)
    if session.assessment_type != "pretest" and len(answers) % 5 == 0:
        new_band, accuracy = assessment_engine.evaluate_band_transition(
            answers, session.current_band
        )
        if new_band != session.current_band:
            band_history = list(session.band_history or [])
            band_history.append({
                "index": len(answers),
                "from_band": session.current_band,
                "to_band": new_band,
                "accuracy_last_5": accuracy,
            })
            session.band_history = band_history
            session.current_band = new_band

    # Distractor analysis for student (sanitized — no internal gate/formula references)
    distractor_analysis = None
    if not correct:
        for dist in q_dict["distractors"]:
            if isinstance(dist, dict) and dist.get("text") == req.selected_answer:
                distractor_analysis = [_sanitize_distractor({
                    "failure_mode": dist.get("failure_mode"),
                    "failure_type": dist.get("failure_type"),
                    "remediation": dist.get("remediation_target"),
                })]
                break

    # Check if assessment is complete
    question_ids = session.question_ids or []
    assessment_complete = len(answers) >= len(question_ids)

    next_question = None
    if not assessment_complete:
        next_idx = len(answers)
        if next_idx < len(question_ids):
            next_qid = question_ids[next_idx]
            result = await db.execute(
                select(Question).where(Question.question_id == next_qid)
            )
            next_q = result.scalar_one_or_none()
            if next_q:
                next_dict = _question_to_dict(next_q)
                next_question = QuestionResponse(
                    question_id=next_dict["question_id"],
                    stem=next_dict["stem"],
                    choices=_make_choices(next_dict),
                    domain=next_dict["domain"],
                    difficulty=next_dict["difficulty"],
                    question_number=next_idx + 1,
                    total_questions=len(question_ids),
                )

    if assessment_complete:
        session.status = "completed"
        session.completed_at = datetime.now(timezone.utc)

        # Generate benchmark
        benchmark = assessment_engine.generate_benchmark_report(
            answers, fm_profile, session.assessment_type
        )
        session.benchmark_report = benchmark

        # Tier assignment for pretest
        if session.assessment_type == "pretest":
            tier = assessment_engine.calculate_tier(answers)
            session.tier_assigned = tier
            user.tier = tier

            # Initialize user progress for all modules
            from app.models.course import CourseModule
            result = await db.execute(select(CourseModule))
            modules = result.scalars().all()
            for mod in modules:
                # First 3 modules always available
                mod_status = "available" if mod.module_number <= 3 else "locked"
                if tier == 2 and mod.module_number > 3:
                    # Check if domain is mastered
                    mod_status = "available"  # simplified; refine with domain mapping

                progress = UserProgress(
                    user_id=user.id,
                    module_number=mod.module_number,
                    status=mod_status,
                )
                db.add(progress)

        # Send benchmark email
        try:
            send_benchmark_email(
                user.email,
                session.assessment_type,
                benchmark["score"],
                benchmark["total"],
                benchmark["strengths"],
                benchmark["weaknesses"],
            )
        except Exception:
            pass  # Don't fail the request if email fails

    await db.commit()

    return AnswerResponse(
        correct=correct,
        correct_answer=_sanitize_answer_text(q_dict["correct_answer"]),
        rationale=_sanitize_rationale(q_dict["correct_rationale"]),
        distractors_analysis=distractor_analysis,
        next_question=next_question,
        assessment_complete=assessment_complete,
    )


@router.get("/result/{session_id}")
async def get_result(
    session_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AssessmentSession).where(
            AssessmentSession.id == session_id,
            AssessmentSession.user_id == user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "completed":
        raise HTTPException(status_code=400, detail="Assessment not yet completed")

    benchmark = session.benchmark_report or {}

    # Translate any raw FM codes in stored benchmarks (backward compat)
    from app.services.assessment_engine import FM_LABELS
    raw_fms = benchmark.get("critical_fms", [])
    translated_fms = []
    for fm in raw_fms:
        label = FM_LABELS.get(fm)
        if not label:
            for key, val in FM_LABELS.items():
                if key in fm:
                    label = val
                    break
        translated_fms.append(label or fm.replace("_", " ").title())

    return AssessmentResult(
        session_id=session.id,
        assessment_type=session.assessment_type,
        score=session.score,
        total_questions=session.total_questions,
        percentage=benchmark.get("percentage", 0),
        tier_assigned=session.tier_assigned,
        score_by_domain=benchmark.get("score_by_domain", {}),
        score_by_difficulty=benchmark.get("score_by_difficulty", {}),
        time_per_question_avg_ms=benchmark.get("avg_response_time_ms", 0),
        total_time_ms=benchmark.get("total_time_ms", 0),
        strengths=benchmark.get("strengths", []),
        weaknesses=benchmark.get("weaknesses", []),
        fm_critical=translated_fms,
        completed_at=session.completed_at,
    )
