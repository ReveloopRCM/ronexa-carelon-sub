"""Analytics API — Data Moat visibility across three feedback loops.

Loop 1: Answer Quality — rep overrides on AI-suggested answers
Loop 2: Outcome Signal — approval/denial rates by CPT, denial reasons
Loop 3: Pattern Intelligence — CPT×ICD coverage, RAG retrieval depth
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import case as sql_case, distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import Case, CaseState, OutcomePattern, Question

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ── Overview ──


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db)):
    """Top-level dataset metrics."""
    total = await _scalar(db, select(func.count(OutcomePattern.id)))
    overrides = await _scalar(
        db,
        select(func.count(OutcomePattern.id)).where(
            OutcomePattern.was_rep_override == True
        ),
    )
    completed = await _scalar(
        db,
        select(func.count(Case.id)).where(
            Case.state.in_([CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED])
        ),
    )
    unique_cpts = await _scalar(
        db, select(func.count(distinct(OutcomePattern.cpt_code)))
    )
    unique_combos = await _scalar(
        db,
        select(
            func.count(
                distinct(
                    func.concat(OutcomePattern.cpt_code, ":", OutcomePattern.icd1)
                )
            )
        ),
    )

    return {
        "total_outcomes": total,
        "total_cases_completed": completed,
        "total_rep_overrides": overrides,
        "override_rate": round(overrides / total, 3) if total > 0 else 0,
        "unique_cpt_codes": unique_cpts,
        "unique_cpt_icd_combos": unique_combos,
    }


# ── Loop 1: Answer Quality (Overrides) ──


@router.get("/overrides")
async def overrides(db: AsyncSession = Depends(get_db)):
    """Loop 1: Rep override rates by CPT + recent overrides."""

    # Override rate by CPT
    result = await db.execute(
        select(
            OutcomePattern.cpt_code,
            func.count(OutcomePattern.id).label("total"),
            func.sum(
                sql_case(
                    (OutcomePattern.was_rep_override == True, 1), else_=0
                )
            ).label("overrides"),
        )
        .group_by(OutcomePattern.cpt_code)
        .order_by(func.count(OutcomePattern.id).desc())
        .limit(20)
    )
    by_cpt = []
    for row in result.all():
        total = row[1]
        ovr = row[2] or 0
        by_cpt.append({
            "cpt_code": row[0],
            "total": total,
            "overrides": ovr,
            "rate": round(ovr / total, 3) if total > 0 else 0,
        })

    # Recent overrides (last 20)
    result = await db.execute(
        select(OutcomePattern)
        .where(OutcomePattern.was_rep_override == True)
        .order_by(OutcomePattern.created_at.desc())
        .limit(20)
    )
    recent = [
        {
            "case_id": p.case_id,
            "cpt_code": p.cpt_code,
            "question_text": (p.question_text or "")[:120],
            "answer_text": p.answer_text,
            "evidence_text": (p.evidence_text or "")[:200],
            "outcome": p.outcome,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in result.scalars().all()
    ]

    # Top overridden questions
    result = await db.execute(
        select(
            OutcomePattern.question_text,
            func.count(OutcomePattern.id).label("total"),
            func.sum(
                sql_case(
                    (OutcomePattern.was_rep_override == True, 1), else_=0
                )
            ).label("override_count"),
        )
        .group_by(OutcomePattern.question_text)
        .having(
            func.sum(
                sql_case(
                    (OutcomePattern.was_rep_override == True, 1), else_=0
                )
            )
            > 0
        )
        .order_by(
            func.sum(
                sql_case(
                    (OutcomePattern.was_rep_override == True, 1), else_=0
                )
            ).desc()
        )
        .limit(10)
    )
    top_overridden = [
        {
            "question_text": (row[0] or "")[:120],
            "total": row[1],
            "override_count": row[2] or 0,
        }
        for row in result.all()
    ]

    return {
        "by_cpt": by_cpt,
        "recent": recent,
        "top_overridden": top_overridden,
    }


# ── Loop 2: Outcome Signal ──


@router.get("/outcomes")
async def outcomes(db: AsyncSession = Depends(get_db)):
    """Loop 2: Approval rates by CPT, top denial reasons."""

    # Outcome by CPT — aggregate from cases table
    result = await db.execute(
        select(
            Case.cpt_code,
            func.sum(sql_case((Case.state == CaseState.APPROVED, 1), else_=0)).label(
                "approved"
            ),
            func.sum(sql_case((Case.state == CaseState.DENIED, 1), else_=0)).label(
                "denied"
            ),
            func.sum(sql_case((Case.state == CaseState.PENDED, 1), else_=0)).label(
                "pended"
            ),
            func.count(Case.id).label("total"),
        )
        .where(Case.state.in_([CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED]))
        .group_by(Case.cpt_code)
        .order_by(func.count(Case.id).desc())
        .limit(20)
    )
    by_cpt = []
    for row in result.all():
        total = row[4]
        approved = row[1] or 0
        by_cpt.append({
            "cpt_code": row[0],
            "approved": approved,
            "denied": row[2] or 0,
            "pended": row[3] or 0,
            "total": total,
            "rate": round(approved / total, 3) if total > 0 else 0,
        })

    # Top denial reasons
    result = await db.execute(
        select(
            OutcomePattern.denial_reason,
            func.count(OutcomePattern.id),
        )
        .where(
            OutcomePattern.outcome == "DENIED",
            OutcomePattern.denial_reason.isnot(None),
            OutcomePattern.denial_reason != "",
        )
        .group_by(OutcomePattern.denial_reason)
        .order_by(func.count(OutcomePattern.id).desc())
        .limit(10)
    )
    top_denials = [
        {"reason": row[0], "count": row[1]} for row in result.all()
    ]

    return {
        "by_cpt": by_cpt,
        "top_denial_reasons": top_denials,
    }


# ── Loop 3: Pattern Coverage ──


@router.get("/coverage")
async def coverage(db: AsyncSession = Depends(get_db)):
    """Loop 3: CPT×ICD coverage depth, RAG retrieval readiness."""

    # Coverage by CPT × ICD
    result = await db.execute(
        select(
            OutcomePattern.cpt_code,
            OutcomePattern.icd1,
            func.count(OutcomePattern.id).label("outcome_count"),
        )
        .group_by(OutcomePattern.cpt_code, OutcomePattern.icd1)
        .order_by(func.count(OutcomePattern.id).desc())
        .limit(50)
    )
    cpt_icd_coverage = [
        {
            "cpt_code": row[0],
            "icd1": row[1],
            "outcome_count": row[2],
        }
        for row in result.all()
    ]

    # Coverage tiers
    tiers = {"100+": 0, "50-99": 0, "10-49": 0, "1-9": 0}
    for item in cpt_icd_coverage:
        c = item["outcome_count"]
        if c >= 100:
            tiers["100+"] += 1
        elif c >= 50:
            tiers["50-99"] += 1
        elif c >= 10:
            tiers["10-49"] += 1
        else:
            tiers["1-9"] += 1

    return {
        "cpt_icd_coverage": cpt_icd_coverage,
        "coverage_tiers": tiers,
    }


# ── CPT+ICD Submission Analytics (for Automation Decisions) ──


@router.get("/cpt-icd-submissions")
async def cpt_icd_submissions(db: AsyncSession = Depends(get_db)):
    """Submission history by CPT+ICD combo — used to inform automation toggles.

    Returns every CPT+ICD combination that has completed submissions,
    with approval/denial/pend counts and success rate.
    """
    result = await db.execute(
        select(
            Case.cpt_code,
            Case.icd1,
            func.count(Case.id).label("total"),
            func.sum(sql_case((Case.state == CaseState.APPROVED, 1), else_=0)).label("approved"),
            func.sum(sql_case((Case.state == CaseState.DENIED, 1), else_=0)).label("denied"),
            func.sum(sql_case((Case.state == CaseState.PENDED, 1), else_=0)).label("pended"),
            func.max(Case.submitted_at).label("last_submitted"),
        )
        .where(Case.state.in_([CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED]))
        .group_by(Case.cpt_code, Case.icd1)
        .order_by(func.count(Case.id).desc())
    )

    combos = []
    for row in result.all():
        total = row.total or 0
        approved = row.approved or 0
        combos.append({
            "cpt_code": row.cpt_code,
            "icd_code": row.icd1,
            "total": total,
            "approved": approved,
            "denied": row.denied or 0,
            "pended": row.pended or 0,
            "success_rate": round(approved / total * 100, 1) if total > 0 else 0,
            "last_submitted": row.last_submitted.isoformat() if row.last_submitted else None,
        })

    return combos


# ── Approval Breakdown ──


@router.get("/approval-breakdown")
async def approval_breakdown(db: AsyncSession = Depends(get_db)):
    """Approval type breakdown — gold card vs algorithm vs manual vs pend/deny."""

    # Totals by approval_type
    result = await db.execute(
        select(
            Case.approval_type,
            func.count(Case.id),
        )
        .where(Case.state.in_([CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED]))
        .group_by(Case.approval_type)
    )
    totals = {"gold_card": 0, "algorithm": 0, "manual": 0, "pended": 0, "denied": 0}
    for row in result.all():
        atype = row[0] or "manual"
        totals[atype] = totals.get(atype, 0) + row[1]

    # Also count pended and denied from state (orthogonal to approval_type)
    pended_count = await _scalar(
        db, select(func.count(Case.id)).where(Case.state == CaseState.PENDED)
    )
    denied_count = await _scalar(
        db, select(func.count(Case.id)).where(Case.state == CaseState.DENIED)
    )
    totals["pended"] = pended_count
    totals["denied"] = denied_count

    # By CPT with approval type breakdown
    result = await db.execute(
        select(
            Case.cpt_code,
            Case.approval_type,
            Case.state,
            func.count(Case.id),
        )
        .where(Case.state.in_([CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED]))
        .group_by(Case.cpt_code, Case.approval_type, Case.state)
        .order_by(Case.cpt_code)
    )

    cpt_map: dict[str, dict] = {}
    for row in result.all():
        cpt = row[0]
        atype = row[1] or "manual"
        state = row[2]
        count = row[3]
        if cpt not in cpt_map:
            cpt_map[cpt] = {"cpt_code": cpt, "gold_card": 0, "algorithm": 0, "manual": 0, "pended": 0, "denied": 0, "total": 0}
        cpt_map[cpt]["total"] += count
        if state == CaseState.PENDED:
            cpt_map[cpt]["pended"] += count
        elif state == CaseState.DENIED:
            cpt_map[cpt]["denied"] += count
        else:
            cpt_map[cpt][atype] = cpt_map[cpt].get(atype, 0) + count

    by_cpt = sorted(cpt_map.values(), key=lambda x: x["total"], reverse=True)

    return {"totals": totals, "by_cpt": by_cpt}


# ── Pathway Intelligence ──


@router.get("/pathway-intelligence")
async def pathway_intelligence(db: AsyncSession = Depends(get_db)):
    """Pathway×ICD approval rates — the 'smoking gun' for right pathway selection."""

    # Pathways with outcomes
    result = await db.execute(
        select(
            Case.pathway_id,
            Case.pathway_name,
            Case.icd1,
            Case.state,
            func.count(Case.id),
        )
        .where(
            Case.state.in_([CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED]),
            Case.pathway_id.isnot(None),
        )
        .group_by(Case.pathway_id, Case.pathway_name, Case.icd1, Case.state)
    )

    # Build pathway summary
    pathway_map: dict[str, dict] = {}
    icd_pathway_map: dict[str, list] = {}

    for row in result.all():
        pid, pname, icd, state, count = row
        key = pid or "unknown"

        if key not in pathway_map:
            pathway_map[key] = {
                "pathway_id": pid,
                "pathway_name": pname,
                "icd_codes": set(),
                "total_cases": 0,
                "approved": 0,
                "pended": 0,
                "denied": 0,
            }
        pathway_map[key]["icd_codes"].add(icd)
        pathway_map[key]["total_cases"] += count
        if state == CaseState.APPROVED:
            pathway_map[key]["approved"] += count
        elif state == CaseState.PENDED:
            pathway_map[key]["pended"] += count
        elif state == CaseState.DENIED:
            pathway_map[key]["denied"] += count

        # ICD→pathway matrix
        if icd not in icd_pathway_map:
            icd_pathway_map[icd] = []
        icd_pathway_map[icd].append({
            "pathway_id": pid,
            "pathway_name": pname,
            "outcome": state.value,
            "count": count,
        })

    # Format pathways
    pathways = []
    for p in pathway_map.values():
        total = p["total_cases"]
        approved = p["approved"]
        pathways.append({
            "pathway_id": p["pathway_id"],
            "pathway_name": p["pathway_name"],
            "icd_codes": sorted(p["icd_codes"]),
            "total_cases": total,
            "approved": approved,
            "pended": p["pended"],
            "denied": p["denied"],
            "approval_rate": round(approved / total, 3) if total > 0 else 0,
        })

    # Sort by total cases desc
    pathways.sort(key=lambda x: x["total_cases"], reverse=True)

    # Mark recommended pathway per ICD (highest approval rate with ≥2 cases)
    icd_best: dict[str, str] = {}
    for icd, entries in icd_pathway_map.items():
        # Aggregate per pathway for this ICD
        pw_stats: dict[str, dict] = {}
        for e in entries:
            pid = e["pathway_id"]
            if pid not in pw_stats:
                pw_stats[pid] = {"approved": 0, "total": 0, "name": e["pathway_name"]}
            pw_stats[pid]["total"] += e["count"]
            if e["outcome"] == "APPROVED":
                pw_stats[pid]["approved"] += e["count"]

        best_pid = None
        best_rate = -1
        for pid, stats in pw_stats.items():
            rate = stats["approved"] / stats["total"] if stats["total"] > 0 else 0
            if rate > best_rate:
                best_rate = rate
                best_pid = pid
        if best_pid:
            icd_best[icd] = best_pid

    # Add is_recommended flag
    for p in pathways:
        # Recommended if it's the best for ANY of its ICD codes
        p["is_recommended"] = any(
            icd_best.get(icd) == p["pathway_id"] for icd in p["icd_codes"]
        )

    # ICD→pathway matrix
    icd_matrix = []
    for icd, entries in sorted(icd_pathway_map.items()):
        # Deduplicate pathway entries
        pw_seen: dict[str, dict] = {}
        for e in entries:
            pid = e["pathway_id"]
            if pid not in pw_seen:
                pw_seen[pid] = {"pathway_name": e["pathway_name"], "outcomes": {}, "total": 0}
            pw_seen[pid]["outcomes"][e["outcome"]] = pw_seen[pid]["outcomes"].get(e["outcome"], 0) + e["count"]
            pw_seen[pid]["total"] += e["count"]

        pathways_tried = [
            {
                "pathway_id": pid,
                "pathway_name": d["pathway_name"],
                "outcomes": d["outcomes"],
                "total": d["total"],
            }
            for pid, d in pw_seen.items()
        ]
        icd_matrix.append({
            "icd_code": icd,
            "pathways_tried": pathways_tried,
            "recommended_pathway": icd_best.get(icd),
        })

    return {
        "pathways": pathways,
        "icd_pathway_matrix": icd_matrix,
    }


# ── Submission Signatures ──


@router.get("/submission-signatures")
async def submission_signatures(db: AsyncSession = Depends(get_db)):
    """CPT+ICD+Pathway submission signatures — learnable outcome patterns."""

    result = await db.execute(
        select(
            Case.cpt_code,
            Case.icd1,
            Case.pathway_name,
            Case.pathway_id,
            func.count(Case.id).label("total"),
            func.sum(sql_case((Case.state == CaseState.APPROVED, 1), else_=0)).label("approved"),
            func.sum(sql_case((Case.state == CaseState.DENIED, 1), else_=0)).label("denied"),
            func.sum(sql_case((Case.state == CaseState.PENDED, 1), else_=0)).label("pended"),
            func.max(Case.submitted_at).label("last_submitted"),
            func.max(Case.state).label("last_state"),
        )
        .where(
            Case.state.in_([CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED]),
            Case.submitted_at.isnot(None),
        )
        .group_by(Case.cpt_code, Case.icd1, Case.pathway_name, Case.pathway_id)
        .order_by(func.count(Case.id).desc())
        .limit(100)
    )

    signatures = []
    for row in result.all():
        total = row.total or 0
        approved = row.approved or 0
        signatures.append({
            "cpt_code": row.cpt_code,
            "icd_code": row.icd1,
            "pathway_name": row.pathway_name,
            "pathway_id": row.pathway_id,
            "total": total,
            "approved": approved,
            "denied": row.denied or 0,
            "pended": row.pended or 0,
            "approval_rate": round(approved / total * 100, 1) if total > 0 else 0,
            "last_submitted": row.last_submitted.isoformat() if row.last_submitted else None,
        })

    return {"signatures": signatures}


# ── Helpers ──


async def _scalar(db: AsyncSession, stmt) -> int:
    result = await db.execute(stmt)
    return result.scalar() or 0
