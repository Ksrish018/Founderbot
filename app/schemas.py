"""Pydantic response schemas for every structured Gemini call, plus the prompt-file loader."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field

from .config import PROMPTS_DIR

Category = Literal["Ingredient Deep-Dive", "Founder Story", "India-Specific Context", "Industry Transparency",
                   "Formulation Science", "Brand Philosophy", "Consumer Education"]
OpeningType = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I"]
RiskFlag = Literal["medical", "regulatory", "unverifiable_claim", "names_competitor", "personal_outside_band", "none"]
Tier = Literal["T0", "T1", "T2", "T3", "T4", "T5", "T6"]

CATEGORIES: tuple[str, ...] = Category.__args__  # type: ignore[attr-defined]
OPENING_TYPES: tuple[str, ...] = OpeningType.__args__  # type: ignore[attr-defined]


# ---- triage ---------------------------------------------------------------
class NoteScore(BaseModel):
    note_id: int
    publishability: int = Field(ge=0, le=10)
    category: Category
    core_gap: str
    opening_type: OpeningType
    needs_facts: list[str] = []
    risk_flags: list[RiskFlag] = []
    cluster_with: list[int] = []
    reason: str
    not_publishable_reason: str = ""


class TriageBatch(BaseModel):
    scores: list[NoteScore]


# ---- news -----------------------------------------------------------------
class NewsQueries(BaseModel):
    queries: list[str]


class AngleChoice(BaseModel):
    ranked_indices: list[int]
    reason: str


# ---- drafting -------------------------------------------------------------
class Placeholder(BaseModel):
    tag: str
    what: str
    why: str


class LedgerEntry(BaseModel):
    claim: str
    tier: Tier


class QAScore(BaseModel):
    dimension: int = Field(ge=1, le=10)
    score: int = Field(ge=0, le=2)


class SelfQA(BaseModel):
    hard_fails: list[str] = []
    scores: list[QAScore] = []
    total: int = 0


class DraftOut(BaseModel):
    post_text: str
    word_count: int
    category: Category
    opening_type: OpeningType
    extensions_used: list[str] = []
    placeholders: list[Placeholder] = []
    verify_flags: list[str] = []
    evidence_ledger: list[LedgerEntry] = []
    self_qa: SelfQA
    note_to_meera: str = ""


# ---- fact check -------------------------------------------------------------
class ClaimProblem(BaseModel):
    claim: str
    issue: Literal["unsupported", "hedge_upgraded", "misused_fact", "news_overreach"]
    fix: str


class FactAudit(BaseModel):
    problems: list[ClaimProblem] = []


# ---- prompt files ---------------------------------------------------------
@lru_cache
def load_prompt(name: str) -> tuple[str, str]:
    """Returns (text, version) for prompts/<name>.md, or the voice skill verbatim for 'voice_skills'."""
    if name == "voice_skills":
        text = (PROMPTS_DIR / "voice_skills.txt").read_text(encoding="utf-8")
        m = re.search(r"Version\s+([\d.]+)", text)
        return text, m.group(1) if m else "?"
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    m = re.search(r"version:\s*([\w.]+)", text)
    return text, m.group(1) if m else "?"


def system_for(wrapper: str) -> tuple[str, str]:
    """Voice skill + a wrapper prompt, as one system instruction. Returns (text, combined version)."""
    voice, vv = load_prompt("voice_skills")
    w, wv = load_prompt(wrapper)
    return f"{voice}\n\n{'=' * 80}\n\n{w}", f"voice-{vv}/{wrapper}-{wv}"
