"""The four investigation stages, the dashboard, and saved investigations."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..db.repositories import DatasetRepository, InvestigationRepository
from ..deps import active_dataset, current_user
from ..engines.contest import contest as contest_stage
from ..engines.investigate import investigate as investigate_stage
from ..engines.act import act as act_stage
from ..engines.observe import available_timeframes
from ..llm.client import get_llm
from ..models.schemas import AnalysisRequest, QuestionRequest
from ..personas import persona_for_role
from ..query import interpret_question_cached
from ..services import dataset_service, pipeline
from .redact import (
    is_analyst,
    redact_contest,
    redact_investigation,
    redact_observation,
    redact_result,
)

router = APIRouter(prefix="/api", tags=["analysis"])


def _dataset_for(user: Dict[str, Any], dataset_id: Optional[str]) -> Dict[str, Any]:
    repo = DatasetRepository()
    ds = repo.get(user["uid"], dataset_id) if dataset_id else repo.active(user["uid"])
    if not ds:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No dataset available. Upload a business metrics CSV on the Data page first.",
        )
    return ds


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (ValueError, dataset_service.DatasetError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------
@router.get("/dashboard")
def dashboard(year: Optional[int] = Query(default=None),
              quarter: Optional[int] = Query(default=None, ge=1, le=4),
              kpi: Optional[str] = Query(default=None),
              comparison: str = Query(default="previous_period"),
              user: Dict[str, Any] = Depends(current_user),
              dataset: Dict[str, Any] = Depends(active_dataset)) -> Dict[str, Any]:
    """Everything the dashboard needs for one (year, quarter) selection."""
    df, schema = dataset_service.load(dataset, user["uid"])
    observation = _guard(pipeline.run_observe, dataset, kpi, year, quarter, comparison,
                         user["uid"])
    return {
        "dataset": {"id": dataset["_id"], "filename": dataset.get("filename"),
                    "rows": schema.row_count, "grain": schema.grain},
        "timeframes": available_timeframes(df),
        "schema": schema.to_dict(),
        "observe": redact_observation(observation, is_analyst(user)),
        "view": {"role": user.get("role"), "analyst_detail_included": is_analyst(user)},
    }


@router.get("/meta/timeframes")
def timeframes(user: Dict[str, Any] = Depends(current_user),
               dataset: Dict[str, Any] = Depends(active_dataset)) -> Dict[str, Any]:
    df, schema = dataset_service.load(dataset, user["uid"])
    return {"timeframes": available_timeframes(df), "kpis": schema.to_dict()["kpi_catalogue"]}


# ---------------------------------------------------------------------------
# single-stage endpoints — intent-driven
# ---------------------------------------------------------------------------
def _resolve_stage_target(user: Dict[str, Any], ds: Dict[str, Any], body: AnalysisRequest):
    """
    What KPI/period a stage should run against, and how it was decided.

    A caller may name the KPI directly (`kpi`/`year`/`quarter`, the original
    contract these endpoints shipped with) or ask a question and let it resolve
    against the KPI contract, the same way `/api/questions/investigate` does.
    The question wins when both are given, since it is the more specific ask.

    Returns `(kpi, year, quarter, comparison, intent, clarification)`.
    `clarification` is the payload to return as-is when a question could not
    be resolved — the caller checks this before doing anything else. Unlike
    the KPI-driven path, an unresolvable question must not fall through to
    "not an approved KPI"; that error names a KPI, and there isn't one yet.
    """
    if not body.question:
        return body.kpi, body.year, body.quarter, body.comparison, None, None

    df, schema = dataset_service.load(ds, user["uid"])
    llm = get_llm() if body.use_llm else None
    intent = interpret_question_cached(body.question, schema, df, ds["_id"], llm=llm)
    if intent.blocked or not intent.outcome:
        clarification = {
            "status": "needs_clarification",
            "question": body.question,
            "intent": intent.model_dump(),
            "ambiguities": [a.model_dump() for a in intent.ambiguities if a.blocking],
        }
        return None, None, None, None, intent, clarification

    period = intent.period
    return (intent.outcome.kpi_key,
            period.year if period else body.year,
            period.quarter if period else body.quarter,
            period.comparison if period else body.comparison,
            intent, None)


@router.post("/observe")
def observe_endpoint(body: AnalysisRequest,
                     user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """STAGE 1 alone — for a named KPI, or for a question resolved against the contract."""
    ds = _dataset_for(user, body.dataset_id)
    kpi, year, quarter, comparison, intent, clarification = _resolve_stage_target(user, ds, body)
    if clarification:
        return clarification
    observation = _guard(pipeline.run_observe, ds, kpi, year, quarter, comparison, user["uid"])
    result = {"stage": "observe", "observe": redact_observation(observation, is_analyst(user))}
    if intent:
        result["intent"] = intent.model_dump()
    return result


@router.post("/investigate")
def investigate_endpoint(body: AnalysisRequest,
                         user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """STAGES 1-2 — the material-signal filter and driver graph run exactly as in the full pipeline."""
    ds = _dataset_for(user, body.dataset_id)
    kpi, year, quarter, comparison, intent, clarification = _resolve_stage_target(user, ds, body)
    if clarification:
        return clarification
    df, schema = dataset_service.load(ds, user["uid"])
    observation = _guard(pipeline.run_observe, ds, kpi, year, quarter, comparison, user["uid"])
    ctx = pipeline.prepare_stage_context(df, schema, observation, intent, comparison)
    llm = get_llm() if body.use_llm else None
    investigation = investigate_stage(df, schema, observation, user["uid"], llm=llm,
                                      signals=ctx["signals"], graph=ctx["graph"])
    analyst = is_analyst(user)
    result = {"stage": "investigate",
             "observe": redact_observation(observation, analyst),
             "investigate": redact_investigation(investigation, analyst)}
    if intent:
        result["intent"] = intent.model_dump()
    return result


@router.post("/contest")
def contest_endpoint(body: AnalysisRequest,
                     user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """STAGES 1-3."""
    ds = _dataset_for(user, body.dataset_id)
    kpi, year, quarter, comparison, intent, clarification = _resolve_stage_target(user, ds, body)
    if clarification:
        return clarification
    df, schema = dataset_service.load(ds, user["uid"])
    observation = _guard(pipeline.run_observe, ds, kpi, year, quarter, comparison, user["uid"])
    ctx = pipeline.prepare_stage_context(df, schema, observation, intent, comparison)
    llm = get_llm() if body.use_llm else None
    investigation = investigate_stage(df, schema, observation, user["uid"], llm=llm,
                                      signals=ctx["signals"], graph=ctx["graph"])
    contested = contest_stage(df, schema, observation, investigation, user["uid"], llm=llm)
    analyst = is_analyst(user)
    result = {"stage": "contest",
             "observe": redact_observation(observation, analyst),
             "investigate": redact_investigation(investigation, analyst),
             "contest": redact_contest(contested, analyst)}
    if intent:
        result["intent"] = intent.model_dump()
    return result


@router.post("/act")
def act_endpoint(body: AnalysisRequest,
                 user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """STAGE 4 (runs 1-3 internally), reframed for the caller's persona."""
    ds = _dataset_for(user, body.dataset_id)
    kpi, year, quarter, comparison, intent, clarification = _resolve_stage_target(user, ds, body)
    if clarification:
        return clarification
    df, schema = dataset_service.load(ds, user["uid"])
    observation = _guard(pipeline.run_observe, ds, kpi, year, quarter, comparison, user["uid"])
    ctx = pipeline.prepare_stage_context(df, schema, observation, intent, comparison)
    llm = get_llm() if body.use_llm else None
    investigation = investigate_stage(df, schema, observation, user["uid"], llm=llm,
                                      signals=ctx["signals"], graph=ctx["graph"])
    contested = contest_stage(df, schema, observation, investigation, user["uid"], llm=llm)
    persona = body.persona or user.get("persona") or persona_for_role(user.get("role"))
    action = act_stage(df, observation, investigation, contested, llm=llm,
                       resolver=schema.contract_resolver, persona=persona)
    result = {"stage": "act", "act": action}
    if intent:
        result["intent"] = intent.model_dump()
    return result


# ---------------------------------------------------------------------------
# full pipeline
# ---------------------------------------------------------------------------
@router.post("/investigations/run")
def run_investigation(body: AnalysisRequest,
                      user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """
    OBSERVE -> INVESTIGATE -> CONTEST -> ACT for an explicitly chosen KPI.

    Superseded as the primary entry point by `/questions/investigate`, which
    resolves the KPI from a question instead. Kept because the dashboard still
    drills in from a named KPI, and because saved investigations link here.
    """
    ds = _dataset_for(user, body.dataset_id)
    persona = user.get("persona") or persona_for_role(user.get("role"))
    result = _guard(pipeline.run_full, user["uid"], ds, body.kpi, body.year, body.quarter,
                    body.comparison, body.persist, body.use_llm, persona)
    return redact_result(result, user)


# ---------------------------------------------------------------------------
# question-driven investigation
# ---------------------------------------------------------------------------
@router.post("/questions/interpret")
def interpret(body: QuestionRequest,
              user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """
    How the system reads a question, without running anything.

    Cheap on purpose: it powers the panel that shows which KPI and period were
    understood, so a misreading can be corrected before an investigation runs.
    """
    ds = _dataset_for(user, body.dataset_id)
    df, schema = _guard(dataset_service.load, ds, user["uid"])
    intent = interpret_question_cached(body.question, schema, df, ds["_id"],
                                       llm=get_llm() if body.use_llm else None)
    return {"intent": intent.model_dump(), "blocked": intent.blocked,
            "assumptions": intent.assumptions}


@router.post("/questions/investigate")
def investigate_question(body: QuestionRequest,
                         user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """
    Ask a business question and get the full investigation.

    Returns `status: "needs_clarification"` instead of a result when the
    question cannot be resolved to a KPI this dataset measures, or names a
    period it does not hold. Answering the nearest question instead would be a
    confident answer to something the user did not ask.
    """
    ds = _dataset_for(user, body.dataset_id)
    persona = body.persona or user.get("persona") or persona_for_role(user.get("role"))
    result = _guard(pipeline.run_question, user["uid"], ds, body.question,
                    persona=persona, persist=body.persist, use_llm=body.use_llm)
    if result.get("status") == "needs_clarification":
        return result
    return redact_result(result, user)


@router.get("/investigations")
def list_investigations(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    items = InvestigationRepository().list_for_user(user["uid"])
    return {
        "investigations": [
            {
                "id": i["_id"],
                # Older rows predate the question-driven flow and have no
                # question; the KPI they were run on is the right label for them.
                "question": i.get("question") or "",
                "title": i.get("question") or i.get("kpi_label") or i.get("kpi"),
                "kpi": i.get("kpi"), "kpi_label": i.get("kpi_label"),
                "timeframe": i.get("timeframe"), "baseline_timeframe": i.get("baseline_timeframe"),
                "change_pct": i.get("change_pct"), "verdict": i.get("verdict"),
                "headline": i.get("headline"),
                "leading_hypothesis": i.get("leading_hypothesis"),
                "leading_confidence": i.get("leading_confidence"),
                "persona": i.get("persona"),
                "created_at": i.get("created_at"),
            }
            for i in items
        ]
    }


@router.get("/investigations/{investigation_id}")
def get_investigation(investigation_id: str,
                      user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    doc = InvestigationRepository().get(user["uid"], investigation_id)
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Investigation not found.")
    return redact_result(doc["result"], user)


@router.delete("/investigations/{investigation_id}")
def delete_investigation(investigation_id: str,
                         user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if not InvestigationRepository().delete(user["uid"], investigation_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Investigation not found.")
    return {"deleted": investigation_id}
