"""Deterministic voice linter. Rules come from config/lint_rules.yaml (voice skill §2, §3, §8, §17).

Hard fails trigger regeneration. Soft warnings are shown on the review card only.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

from .config import CONFIG_DIR

PLACEHOLDER_RE = re.compile(r"\[(DATA NEEDED|SOURCE NEEDED):[^\[\]\n]+\]")
VERIFY_RE = re.compile(r"\s*\[VERIFY[^\[\]\n]*\]")
QUOTE_RES = [
    re.compile(r'"[^"\n]*"'),
    re.compile(r"“[^”\n]*”"),
    re.compile(r"‘[^’\n]*’"),
    re.compile(r"(?<![\w])'[^'\n]+?'(?![\w])"),
    # Reported thought/speech after a colon ("I thought: what would it look like...?")
    re.compile(r"\b(?:said|says|thought|asked|asking|wondered|wrote|writes)\s*:\s*[^.?!\n]*\?"),
    # A quoted banner in capitals after a colon ("a banner that said: CLINICALLY TESTED.")
    re.compile(r":\s*(?:[A-Z]{2,}(?:[ \t,'-]+[A-Z]{2,})*\.\s*)+"),
]
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⬀-⯿←-⇿️‍⌀-⏿]")
BULLET_RE = re.compile(r"^\s*([-*•●▪◦–]|\d{1,2}[.)])\s+", re.M)
HEADER_RE = re.compile(r"^\s*#{1,6}\s", re.M)
BOLD_RE = re.compile(r"\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*")
HASHTAG_RE = re.compile(r"(?<![\w&/])#[A-Za-z]\w*")
CAPS_RE = re.compile(r"\b[A-Z]{3,}\b")
WORD_RE = re.compile(r"[A-Za-z0-9₹][\w'’.%-]*")
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])[\"”’']?\s+(?=[\"“‘'(]?[A-Z0-9])")
NUMBER_CONTEXT_RE = re.compile(
    r"(\d[\d.,-]*\s?(%|mg|ml|g\b|months?|years?|weeks?|days?|hours?|degrees|°|units?|cm|batch|batches|"
    r"customers|returns|times|in\s+\d)|pH\s?\d|\b(19|20)\d{2}\b)", re.I)

# Nouns that make a first sentence concrete (§3): ingredients, documents, places, numbers.
CONCRETE_TERMS = {
    "serum", "moisturiser", "sunscreen", "spf", "label", "bottle", "jar", "pump", "batch", "coa", "spec", "sheet",
    "supplier", "manufacturer", "customer", "niacinamide", "ceramide", "retinol", "vitamin", "peptide", "acid",
    "preservative", "emollient", "humectant", "fragrance", "paraben", "ph", "stability", "formulation", "document",
    "report", "file", "log", "record", "meeting", "review", "fair", "mumbai", "chennai", "kochi", "kolkata",
    "delhi", "bengaluru", "india", "lab", "study", "trial", "return", "returns", "product", "ingredient",
    "inci", "cdsco", "email", "message", "question", "banner", "packaging", "texture", "barrier", "skin",
}


@dataclass
class LintResult:
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    word_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.hard

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


@lru_cache
def load_rules(path: str | None = None) -> dict:
    p = Path(path) if path else CONFIG_DIR / "lint_rules.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _phrase_re(phrase: str) -> re.Pattern:
    esc = re.escape(phrase.lower()).replace(r"\ ", r"\s+")
    start = r"(?<![\w-])" if phrase[0].isalnum() else ""
    end = r"(?![\w-])" if phrase[-1].isalnum() else ""
    return re.compile(start + esc + end, re.I)


@lru_cache
def _compiled(rules_key: int) -> dict:
    rules = load_rules()
    banned = []
    for group in ("banned_hype", "banned_growth", "banned_sales", "banned_closers"):
        for p in rules.get(group, []) or []:
            banned.append((str(p), _phrase_re(str(p))))
    spell = [(k, v, re.compile(rf"(?<![\w-]){re.escape(k)}(?![\w-])", re.I))
             for k, v in (rules.get("american_spellings") or {}).items() if k != v]
    soft = [(w, _phrase_re(w)) for w in rules.get("soft_words", []) or []]
    return {"banned": banned, "spell": spell, "soft": soft,
            "caps": {c.upper() for c in rules.get("allowed_caps", []) or []},
            "boundary": [b.lower() for b in rules.get("boundary_patterns", []) or []]}


def strip_quotes(text: str) -> str:
    for r in QUOTE_RES:
        text = r.sub(lambda m: " " * len(m.group(0)), text)
    return text


def find_placeholders(text: str) -> list[str]:
    return [m.group(0) for m in PLACEHOLDER_RE.finditer(text)]


def extract_verify(text: str) -> tuple[str, list[str]]:
    """[VERIFY ...] tags belong on the review card, never in the post. Move them out."""
    flags = [m.group(0).strip() for m in VERIFY_RE.finditer(text)]
    clean = VERIFY_RE.sub("", text)
    clean = re.sub(r"[ \t]+([.,;:])", r"\1", clean)
    return clean, flags


def split_sentences(text: str) -> list[str]:
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        para = " ".join(para.split())
        if para:
            out.extend(s.strip() for s in SENT_SPLIT_RE.split(para) if s.strip())
    return out


def words(text: str) -> list[str]:
    return WORD_RE.findall(text)


def lint(text: str, *, short: bool = False, check_length: bool = True, allow_hashtags: bool = False,
         opening_type: str | None = None, recent_opening_types: Iterable[str] = ()) -> LintResult:
    rules = load_rules()
    c = _compiled(id(rules))
    res = LintResult()

    no_ph = PLACEHOLDER_RE.sub(" ", text)
    no_ph = VERIFY_RE.sub(" ", no_ph)
    unquoted = strip_quotes(no_ph)
    res.word_count = len(words(no_ph))

    # --- punctuation & formatting ---
    if "!" in no_ph:
        res.hard.append("Exclamation mark found (she uses none).")
    emojis = set(EMOJI_RE.findall(no_ph))
    if emojis:
        res.hard.append(f"Emoji or symbol found: {' '.join(sorted(emojis))}")
    if "—" in no_ph:
        res.hard.append("Em dash (—) found. Her dash is a spaced hyphen ' - '.")
    if BULLET_RE.search(no_ph):
        res.hard.append("Bullet or numbered-list line found. Everything must be prose.")
    if HEADER_RE.search(no_ph) or BOLD_RE.search(no_ph):
        res.hard.append("Markdown bold or header found.")

    tags = HASHTAG_RE.findall(no_ph)
    if tags:
        if not allow_hashtags:
            res.hard.append(f"Hashtags are not allowed: {' '.join(tags)}")
        else:
            lines = [ln for ln in no_ph.strip().splitlines() if ln.strip()]
            last_tags = HASHTAG_RE.findall(lines[-1]) if lines else []
            if len(tags) > 3 or len(last_tags) != len(tags):
                res.hard.append("Hashtags: at most 3, and only on the final line.")

    if "?" in unquoted:
        res.hard.append("Question mark outside quotation marks (no rhetorical or engagement questions).")
    sentences = split_sentences(no_ph)
    if sentences and sentences[-1].rstrip("\"”’' ").endswith("?"):
        res.hard.append("Final sentence ends in a question.")

    # --- banned language ---
    hits = sorted({p for p, r in c["banned"] if r.search(unquoted)})
    if hits:
        res.hard.append(f"Banned phrase(s): {', '.join(hits)}")
    caps = sorted({w for w in CAPS_RE.findall(unquoted) if w.upper() not in c["caps"]})
    if caps:
        res.hard.append(f"ALL CAPS emphasis: {', '.join(caps)}")

    us = sorted({f"{m.group(0)}→{v}" for k, v, r in c["spell"] for m in r.finditer(no_ph)})
    if us:
        res.hard.append(f"American spelling(s): {', '.join(us)}")

    # --- placeholders ---
    residue = PLACEHOLDER_RE.sub("", VERIFY_RE.sub("", text))
    if "[" in residue or "]" in residue:
        res.hard.append("Malformed placeholder. Use [DATA NEEDED: what, from whom] or [SOURCE NEEDED: claim, search].")

    # --- length ---
    if check_length:
        lo = rules["word_count"]["short_min"] if short else rules["word_count"]["min"]
        hi = rules["word_count"]["max"]
        if not lo <= res.word_count <= hi:
            res.hard.append(f"Word count {res.word_count} is outside {lo}-{hi}.")

    # --- soft warnings ---
    if sentences:
        lens = [len(words(s)) for s in sentences]
        short_share = sum(1 for n in lens if n <= 8) / len(lens)
        if short_share < 0.20:
            res.soft.append(f"Rhythm: only {short_share:.0%} of sentences are 8 words or fewer (target 20%+).")
        if max(lens) <= 30:
            res.soft.append("Rhythm: no sentence over 30 words (she uses long mechanism sentences).")
        opening = [x for x in sentences if not re.fullmatch(r"(Hi|Hello|Dear)\b.{0,20}", x)]
        first = (opening or sentences)[0]
        if not (re.search(r"\d", first) or any(re.search(rf"\b{t}\b", first.lower()) for t in CONCRETE_TERMS)
                or re.search(r'["“]', first)):
            res.soft.append("Opening sentence lacks a concrete noun (scene, number, product, document).")
    low = no_ph.lower()
    if not any(b in low for b in c["boundary"]):
        res.soft.append("No boundary statement (\"I'm not saying... What I'm saying is...\").")
    if not NUMBER_CONTEXT_RE.search(no_ph):
        res.soft.append("No number with context (time window, threshold, unit).")
    soft_hits = sorted({w for w, r in c["soft"] if r.search(unquoted)})
    if soft_hits:
        res.soft.append(f"Check usage is not praise: {', '.join(soft_hits)}")
    if opening_type and opening_type in set(recent_opening_types):
        res.soft.append(f"Opening type {opening_type} was used in one of the last 5 approved posts.")
    return res
