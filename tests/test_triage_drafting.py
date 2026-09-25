from __future__ import annotations

import json

from app import drafting, facts, triage
from app.config import CONFIG_DIR
from tests.conftest import GOOD_POST


def score(nid, p, cluster=(), cat="Industry Transparency"):
    return {"note_id": nid, "publishability": p, "category": cat, "core_gap": "Same formula is not same product.",
            "opening_type": "B", "needs_facts": [], "risk_flags": ["none"], "cluster_with": list(cluster),
            "reason": "Concrete batch record and a clear gap.", "not_publishable_reason": "" if p > 3 else "Venting."}


def add_notes(db, n):
    return [db.upsert_note(source="import", chat_id=None, message_id=None, type_="text", text=f"note {i}")[0]
            for i in range(n)]


async def test_triage_scores_and_shortlists(db, fake, settings):
    ids = add_notes(db, 5)
    fake.queue("triage", {"scores": [score(ids[0], 9, cluster=[ids[1], 999]), score(ids[1], 7), score(ids[2], 6),
                                     score(ids[3], 5), score(ids[4], 2), score(12345, 10)]})
    assert await triage.score_pending(db, fake, settings) == 5
    assert db.note(ids[4])["status"] == "unpublishable"

    cands = triage.build_shortlist(db, settings)
    assert [c.note_id for c in cands] == [ids[0], ids[2]]       # ids[1] merged into ids[0]; 5 and 2 below threshold
    assert cands[0].cluster == [ids[1]] and cands[0].risk_flags == []
    assert "9/10" in triage.candidate_text(cands[0])

    triage.mark_shown(db, cands[:1])
    assert db.note(ids[0])["status"] == "shortlisted"
    db.set_note_status([ids[2]], "skipped", park_days=30)
    assert [c.note_id for c in triage.build_shortlist(db, settings)] == [ids[0]]


async def test_nothing_above_threshold_gives_empty_shortlist(db, fake, settings):
    ids = add_notes(db, 2)
    fake.queue("triage", {"scores": [score(ids[0], 4), score(ids[1], 5)]})
    await triage.score_pending(db, fake, settings)
    assert triage.build_shortlist(db, settings) == []


async def test_voice_note_without_transcript_is_not_scored(db):
    db.upsert_note(source="telegram", chat_id=-100, message_id=1, type_="voice", text=None, file_id="f")
    assert triage.pending_notes(db) == []


def draft_json(text, opening="B", verify=()):
    return {"post_text": text, "word_count": 400, "category": "Industry Transparency", "opening_type": opening,
            "extensions_used": ["E1", "E2"], "placeholders": [], "verify_flags": list(verify),
            "evidence_ledger": [{"claim": "pH dropped 0.4", "tier": "T5"}],
            "self_qa": {"hard_fails": [], "scores": [{"dimension": 1, "score": 2}], "total": 17},
            "note_to_meera": "No placeholders."}


async def test_lint_failure_triggers_retry_with_errors(db, fake, settings):
    nid = add_notes(db, 1)[0]
    req = drafting.start_request(db, [nid])
    db.run("UPDATE draft_requests SET news_ranking='[]', news_pos=-1, news_status='none' WHERE id=?", (req,))
    fake.queue("draft", draft_json("Game-changer serum! " * 5))
    fake.queue("draft", draft_json(GOOD_POST + " [VERIFY CURRENT REGULATION]"))
    did = await drafting.generate(db, fake, settings, req)
    d = db.draft(did)
    assert d["lint_retries"] == 1 and d["lint_hard_fail_first"] == 1
    assert json.loads(d["lint_json"])["ok"]
    assert "[VERIFY" not in d["post_text"]
    assert "[VERIFY CURRENT REGULATION]" in json.loads(d["meta_json"])["verify_flags"]
    retry_prompt = [c for c in fake.calls if c["purpose"].startswith("draft")][1]["prompt"]
    assert "FAILED THESE HARD VOICE CHECKS" in retry_prompt and "Exclamation" in retry_prompt
    # system instruction is the full voice skill + wrapper
    assert "VOICE SKILL: MEERA PILLAI" in fake.calls[0]["system"] and "Drafting wrapper" in fake.calls[0]["system"]


async def test_after_two_retries_draft_is_delivered_with_failures(db, fake, settings):
    nid = add_notes(db, 1)[0]
    req = drafting.start_request(db, [nid])
    for _ in range(3):
        fake.queue("draft", draft_json("Too short and hyped! "))
    did = await drafting.generate(db, fake, settings, req)
    d = db.draft(did)
    assert d["lint_retries"] == 2 and not json.loads(d["lint_json"])["ok"]


async def test_prompt_contains_only_canonical_facts_and_conflicts_marked(db, fake, settings):
    nid = add_notes(db, 1)[0]
    req = drafting.start_request(db, [nid])
    fake.queue("draft", draft_json(GOOD_POST))
    await drafting.generate(db, fake, settings, req)
    prompt = fake.calls[0]["prompt"]
    assert "Skinstinct serum pH is 5.5-5.8" in prompt
    assert "CONFLICTS (DO NOT USE" in prompt and "[C3]" in prompt
    assert "NEWS ITEM: none" in prompt


async def test_revision_keeps_chain_and_short_mode(db, fake, settings):
    nid = add_notes(db, 1)[0]
    req = drafting.start_request(db, [nid])
    fake.queue("draft", draft_json(GOOD_POST))
    first = await drafting.generate(db, fake, settings, req)
    short_text = " ".join(GOOD_POST.split()[:200]).rstrip(".,") + "."
    fake.queue("draft", draft_json(short_text, opening="A"))
    second = await drafting.generate(db, fake, settings, req, parent_id=first, instruction="Make it shorter",
                                     mode="revise")
    d2 = db.draft(second)
    assert db.draft(first)["status"] == "revised"
    assert d2["version"] == 2 and d2["parent_draft_id"] == first
    assert json.loads(d2["meta_json"])["short"] is True
    revise_call = [c for c in fake.calls if c["purpose"] == "draft:revise"][-1]
    assert "Revision wrapper" in revise_call["system"] and "MEERA'S INSTRUCTION" in revise_call["prompt"]
    assert fake.calls[-1]["purpose"] == "audit" and "Make it shorter" in fake.calls[-1]["prompt"]


def test_approve_guard_and_edit_ratio():
    assert drafting.unresolved_placeholders("pH is [DATA NEEDED: value, from Meera] here") == \
        ["[DATA NEEDED: value, from Meera]"]
    assert drafting.unresolved_placeholders("[SOURCE NEEDED broken") == ["[SOURCE NEEDED broken"]
    assert drafting.unresolved_placeholders(GOOD_POST) == []
    assert drafting.edit_ratio(GOOD_POST, GOOD_POST) == 0
    assert 0 < drafting.edit_ratio(GOOD_POST, GOOD_POST.replace("Batch fourteen", "Batch 14")) < 0.05


def test_facts_resolve_is_stored_in_db_not_yaml(db):
    before = (CONFIG_DIR / "facts.yaml").read_text(encoding="utf-8")
    c = facts.resolve(db, "C1", "Skinstinct launched in March 2025.")
    assert c["status"] == "canonical" and c["resolved_at"]
    assert (CONFIG_DIR / "facts.yaml").read_text(encoding="utf-8") == before   # yaml untouched
    block = facts.canonical_block(facts.load(db))
    assert "Skinstinct launched in March 2025." in block
    assert "[C1]" not in block.split("CONFLICTS")[1]
    assert "[C1]" in facts.canonical_block(facts.load())                        # baseline still has it
    assert db.one("SELECT new_status FROM facts_log WHERE fact_key='C1'")["new_status"] == "canonical"
    facts.resolve(db, "C1", "Updated answer.")
    assert "Updated answer." in facts.canonical_block(facts.load(db))


async def test_fact_check_fixes_unsupported_claims_once(db, fake, settings):
    nid = add_notes(db, 1)[0]
    req = drafting.start_request(db, [nid])
    fake.queue("draft", draft_json(GOOD_POST))                                        # first draft
    fake.queue("audit", {"problems": [{"claim": "measurably increasing photosensitivity", "issue": "hedge_upgraded",
                                       "fix": "say 'can increase'"}]})
    fake.queue("draft", draft_json(GOOD_POST.replace("Batch fourteen", "Batch 14")))  # fixed draft
    fake.queue("audit", {"problems": []})                                             # re-check passes
    did = await drafting.generate(db, fake, settings, req)
    d = db.draft(did)
    meta = json.loads(d["meta_json"])
    assert meta["fact_check"] == {"found": 1, "fixed": True, "remaining": []}
    assert d["post_text"].startswith("Batch 14")
    purposes = [c["purpose"] for c in fake.calls]
    assert purposes == ["draft:draft", "audit", "draft:factfix", "audit"]
    assert "measurably increasing photosensitivity" in fake.calls[2]["prompt"]
    assert "MEERA'S NOTE(S)" in fake.calls[1]["prompt"] and "CANONICAL FACTS" in fake.calls[1]["prompt"]


async def test_fact_check_leftovers_become_verify_flags(db, fake, settings):
    nid = add_notes(db, 1)[0]
    req = drafting.start_request(db, [nid])
    fake.queue("draft", draft_json(GOOD_POST))
    bad = {"claim": "many of our customers layer other brands", "issue": "unsupported", "fix": "remove"}
    fake.queue("audit", {"problems": [bad]})
    fake.queue("draft", draft_json(GOOD_POST))
    fake.queue("audit", {"problems": [bad]})
    did = await drafting.generate(db, fake, settings, req)
    meta = json.loads(db.draft(did)["meta_json"])
    assert meta["fact_check"]["remaining"] == [bad]
    assert any(f.startswith("[CHECK CLAIM (unsupported): many of our customers") for f in meta["verify_flags"])


async def test_fact_fix_never_replaces_a_voice_clean_draft_with_a_failing_one(db, fake, settings):
    nid = add_notes(db, 1)[0]
    req = drafting.start_request(db, [nid])
    fake.queue("draft", draft_json(GOOD_POST))
    fake.queue("audit", {"problems": [{"claim": "x", "issue": "unsupported", "fix": "remove"}]})
    fake.queue("draft", draft_json("Too short and hyped! "))
    did = await drafting.generate(db, fake, settings, req)
    d = db.draft(did)
    assert d["post_text"] == GOOD_POST and json.loads(d["lint_json"])["ok"]
    assert json.loads(d["meta_json"])["fact_check"]["fixed"] is False
