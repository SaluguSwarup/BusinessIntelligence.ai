"""
Orchestration of the four-stage investigation.

    OBSERVE -> INVESTIGATE -> CONTEST -> ACT

Deliberately a plain deterministic sequence, not an autonomous agent loop: the
order of the stages is the product's core idea, so it is expressed in code rather
than left to a model to decide.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..db.repositories import InvestigationRepository
from ..engines.act import act
from ..engines.contest import contest
from ..engines.investigate import investigate
from ..engines.observe import Timeframe, available_timeframes, observe
from ..llm.client import get_llm
from . import dataset_service


# The headline KPI a business would lead with, in order of preference. Revenue
# when there is one; otherwise the measure that best represents what the
# organisation *does* — admissions for a hospital, shipments for a carrier —
# rather than whichever KPI happens to sort first. A cost is never the headline.
HEADLINE_TAGS = ["topline", "demand_value", "demand_volume", "activity", "throughput"]


def default_kpi(schema) -> str:
    """Pick the KPI to lead with when the caller did not name one."""
    available = list(schema.available_kpis)
    if not available:
        raise ValueError("This dataset has no approved KPIs to analyse.")
    if "revenue" in available:
        return "revenue"

    resolver = schema.contract_resolver or {}

    def tags(key: str) -> List[str]:
        definition = getattr(resolver.get(key), "definition", None)
        return list(getattr(definition, "semantic_tags", []) or [])

    for tag in HEADLINE_TAGS:
        for key in available:
            if tag in tags(key):
                return key
    # Nothing declared a headline role: fall back to the first measure that is a
    # plain additive quantity and is not a cost.
    for key in available:
        spec = resolver.get(key)
        if spec is not None and spec.kind == "sum" and spec.higher_is_better:
            return key
    return available[0]


def resolve_timeframe(df, year: Optional[int], quarter: Optional[int]) -> Timeframe:
    frames = available_timeframes(df)
    if not frames:
        raise ValueError("The dataset contains no usable dates.")
    if year is None:
        latest = frames[-1]
        return Timeframe(latest["year"], latest["quarter"])
    return Timeframe(int(year), int(quarter) if quarter else None)


def run_observe(dataset: Dict[str, Any], metric: Optional[str], year: Optional[int],
                quarter: Optional[int], comparison: str = "previous_period",
                uid: Optional[str] = None) -> Dict[str, Any]:
    df, schema = dataset_service.load(dataset, uid)
    kpi = metric or default_kpi(schema)
    if kpi not in schema.available_kpis:
        raise ValueError(
            f"'{kpi}' is not an approved KPI for this dataset. Available: "
            f"{', '.join(schema.available_kpis)}. Define or approve it in the KPI contract."
        )
    tf = resolve_timeframe(df, year, quarter)
    return observe(df, schema, kpi, tf, comparison)


def run_full(uid: str, dataset: Dict[str, Any], metric: Optional[str], year: Optional[int],
             quarter: Optional[int], comparison: str = "previous_period",
             persist: bool = True, use_llm: bool = True) -> Dict[str, Any]:
    started = time.time()
    df, schema = dataset_service.load(dataset, uid)
    llm = get_llm() if use_llm else None

    observation = run_observe(dataset, metric, year, quarter, comparison, uid)
    stage_times = {"observe": round(time.time() - started, 3)}

    t = time.time()
    investigation = investigate(df, schema, observation, uid, llm=llm)
    stage_times["investigate"] = round(time.time() - t, 3)

    t = time.time()
    contested = contest(df, schema, observation, investigation, uid, llm=llm)
    stage_times["contest"] = round(time.time() - t, 3)

    t = time.time()
    action = act(df, observation, investigation, contested, llm=llm,
                 resolver=schema.contract_resolver)
    stage_times["act"] = round(time.time() - t, 3)

    result = {
        "dataset": {
            "id": dataset["_id"],
            "filename": dataset.get("filename"),
            "rows": dataset.get("schema", {}).get("row_count"),
        },
        "observe": observation,
        "investigate": investigation,
        "contest": contested,
        "act": action,
        "engine": {
            "llm": (llm.status if llm else {"enabled": False, "mode": "disabled"}),
            "stage_seconds": stage_times,
            "total_seconds": round(time.time() - started, 3),
            "pipeline": ["observe", "investigate", "contest", "act"],
        },
    }

    if persist:
        top = (contested.get("ranking") or [{}])[0]
        saved = InvestigationRepository().create(uid, {
            "kpi": observation["kpi"],
            "kpi_label": observation["kpi_label"],
            "timeframe": observation["timeframe"],
            "baseline_timeframe": observation["baseline_timeframe"],
            "comparison": comparison,
            "change_pct": observation.get("change_pct"),
            "verdict": observation.get("verdict"),
            "headline": action["narrative"]["headline"],
            "leading_hypothesis": top.get("title"),
            "leading_confidence": top.get("confidence"),
            "dataset_id": dataset["_id"],
            "result": result,
        })
        result["investigation_id"] = saved["_id"]
    return result
