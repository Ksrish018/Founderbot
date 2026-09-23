"""/stats and the Sunday summary: progress against 3 posts a week and 15 minutes a post."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from .config import Settings
from .db import DB, iso, parse_iso

TARGET_PER_WEEK = 3


def _week_start(settings: Settings) -> datetime:
    local = datetime.now(settings.tz)
    start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start


def compute(db: DB, settings: Settings) -> dict:
    since = iso(_week_start(settings))

    def c(sql: str, col: str, window: bool) -> int:
        if window:
            return db.one(f"{sql} AND {col} >= ?", (since,))["c"]
        return db.one(sql)["c"]

    out: dict = {}
    for label, window in (("week", True), ("all", False)):
        out[label] = {
            "captured": c("SELECT COUNT(*) c FROM notes WHERE 1=1", "created_at", window),
            "shortlisted": c("SELECT COUNT(DISTINCT note_id) c FROM events WHERE type='shortlisted'", "created_at",
                             window),
            "drafted": c("SELECT COUNT(DISTINCT request_id) c FROM drafts WHERE 1=1", "delivered_at", window),
            "approved": c("SELECT COUNT(*) c FROM drafts WHERE status='approved'", "decided_at", window),
            "rejected": c("SELECT COUNT(*) c FROM drafts WHERE status='rejected'", "decided_at", window),
        }
        reasons = db.q("SELECT reject_reason r, COUNT(*) c FROM drafts WHERE status='rejected' "
                       + ("AND decided_at >= ? " if window else "") + "GROUP BY reject_reason",
                       (since,) if window else ())
        out[label]["reject_reasons"] = {(r["r"] or "none given"): r["c"] for r in reasons}

    # Review time: first delivery in a request -> approval (proxy for Meera's time per post)
    mins, revs = [], []
    for a in db.q("SELECT * FROM drafts WHERE status='approved'"):
        first = db.one("SELECT MIN(delivered_at) t, COUNT(*) n FROM drafts WHERE request_id=?", (a["request_id"],))
        t0, t1 = parse_iso(first["t"]), parse_iso(a["decided_at"])
        if t0 and t1:
            mins.append((t1 - t0).total_seconds() / 60)
        revs.append(db.one("SELECT COUNT(*) c FROM drafts WHERE request_id=? AND revision_instruction IS NOT NULL",
                           (a["request_id"],))["c"])
    out["median_review_min"] = round(statistics.median(mins), 1) if mins else None
    out["revisions_per_approved"] = round(statistics.mean(revs), 2) if revs else None
    ratios = [r["edit_ratio"] for r in db.q("SELECT edit_ratio FROM finals WHERE edit_ratio IS NOT NULL")]
    out["median_edit_ratio"] = round(statistics.median(ratios), 3) if ratios else None
    light = [r for r in ratios if r <= 0.15]
    out["light_edit_share"] = round(len(light) / len(ratios), 2) if ratios else None

    d = db.one("SELECT COUNT(*) n, SUM(lint_hard_fail_first) f, AVG(cost_est) cost, AVG(tokens_in+tokens_out) tok "
               "FROM drafts")
    out["lint_first_fail_rate"] = round((d["f"] or 0) / d["n"], 2) if d["n"] else None
    out["avg_cost_per_draft"] = round(d["cost"] or 0, 4)
    out["avg_tokens_per_draft"] = int(d["tok"] or 0)
    total = db.one("SELECT SUM(cost_est) c, SUM(tokens_in) i, SUM(tokens_out) o FROM llm_calls")
    out["total_cost"] = round(total["c"] or 0, 4)
    out["total_tokens"] = (total["i"] or 0) + (total["o"] or 0)
    return out


def format_stats(s: dict) -> str:
    w, a = s["week"], s["all"]

    def fmt(v, suffix=""):
        return "n/a" if v is None else f"{v}{suffix}"

    reasons = ", ".join(f"{k}: {v}" for k, v in a["reject_reasons"].items()) or "none"
    return (
        f"This week: {w['approved']} of {TARGET_PER_WEEK} posts approved\n"
        f"  captured {w['captured']} · shortlisted {w['shortlisted']} · drafted {w['drafted']} · "
        f"rejected {w['rejected']}\n\n"
        f"All time: captured {a['captured']} · shortlisted {a['shortlisted']} · drafted {a['drafted']} · "
        f"approved {a['approved']} · rejected {a['rejected']}\n"
        f"Reject reasons: {reasons}\n\n"
        f"Median time from draft to approval: {fmt(s['median_review_min'], ' min')} (target 15 min or less)\n"
        f"Revisions per approved post: {fmt(s['revisions_per_approved'])}\n"
        f"Median edit ratio (draft vs what you posted): {fmt(s['median_edit_ratio'])}\n"
        f"Share posted with light or no edits: {fmt(s['light_edit_share'])} (target 0.6+)\n"
        f"Lint hard-fail rate before retry: {fmt(s['lint_first_fail_rate'])}\n"
        f"Avg Gemini cost per draft: ${s['avg_cost_per_draft']} ({s['avg_tokens_per_draft']} tokens) · "
        f"total ${s['total_cost']}"
    )
