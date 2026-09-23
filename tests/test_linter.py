"""Linter: every published piece passes the hard checks; every §16 off-voice sample fails."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import DATA_DIR, PROMPTS_DIR
from app.importer import load_published
from app.linter import extract_verify, find_placeholders, lint
from tests.conftest import GOOD_POST

PUBLISHED = load_published(DATA_DIR / "published")


def _section16(kind: str) -> list[str]:
    """Pull the OFF-VOICE / IN-VOICE samples straight out of voice_skills.txt §16."""
    text = (PROMPTS_DIR / "voice_skills.txt").read_text(encoding="utf-8")
    sec = text.split("SECTION 16")[1].split("SECTION 17")[0]
    blocks = re.findall(rf"{kind}:\s*\n(.*?)(?=\n\s*(?:IN-VOICE|OFF-VOICE|What changed):)", sec, re.S)
    return [" ".join(b.split()).strip().strip('"') for b in blocks]


OFF_VOICE = _section16("OFF-VOICE")
IN_VOICE = _section16("IN-VOICE")


def test_corpus_loaded():
    assert len(PUBLISHED) == 15
    assert sum(p.kind == "linkedin" for p in PUBLISHED) == 4
    assert len(OFF_VOICE) == 3 and len(IN_VOICE) == 3


@pytest.mark.parametrize("piece", PUBLISHED, ids=lambda p: p.name)
def test_published_pieces_pass_hard_checks(piece):
    res = lint(piece.text, check_length=False)
    assert res.hard == [], f"{piece.name}: {res.hard}"


@pytest.mark.parametrize("piece", [p for p in PUBLISHED if p.kind == "linkedin"], ids=lambda p: p.name)
def test_published_linkedin_posts_are_close_to_length_band(piece):
    # Her LinkedIn posts run 420-535 words (§2). The draft band is 380-520.
    assert 400 <= lint(piece.text, check_length=False).word_count <= 545


@pytest.mark.parametrize("sample", OFF_VOICE)
def test_off_voice_samples_fail(sample):
    assert lint(sample, check_length=False).hard


@pytest.mark.parametrize("sample", IN_VOICE)
def test_in_voice_samples_pass(sample):
    assert lint(sample, check_length=False).hard == []


def test_good_post_passes_everything():
    res = lint(GOOD_POST)
    assert res.ok and 380 <= res.word_count <= 520


@pytest.mark.parametrize("bad, fragment", [
    ("This is a real change!", "Exclamation"),
    ("The pH dropped — and nobody noticed.", "Em dash"),
    ("The pH dropped 😬 overnight.", "Emoji"),
    ("Three reasons:\n- pH\n- base\n- batch", "Bullet"),
    ("Three reasons:\n1. pH\n2. base", "Bullet"),
    ("This is **important** to know.", "bold"),
    ("## Label versus lab", "header"),
    ("Worth reading. #skincare", "Hashtags"),
    ("Why does this happen? Because of pH.", "Question mark"),
    ("This serum is a game-changer for oily skin.", "Banned"),
    ("It gives you glass skin in a week.", "Banned"),
    ("Thoughts? I would love to hear.", "Banned"),
    ("Use code SKIN10 for a discount.", "Banned"),
    ("The vitamin C will oxidize in light.", "American"),
    ("A good moisturizer matters in Mumbai.", "American"),
    ("Stay glowing, everyone.", "Banned"),
    ("This is USELESS for most people.", "ALL CAPS"),
    ("We need [DATA NEEDED the pH value] here.", "Malformed"),
])
def test_specific_hard_fails(bad, fragment):
    hard = lint(bad, check_length=False).hard
    assert any(fragment.lower() in h.lower() for h in hard), hard


def test_final_question_fails_even_in_quotes():
    hard = lint('The customer asked me one thing. "Is it safe?"', check_length=False).hard
    assert any("Final sentence" in h for h in hard)


def test_question_inside_quotes_mid_post_is_allowed():
    text = 'Her question was specific and good: "Are they compatible?" The honest answer is that it depends.'
    assert lint(text, check_length=False).hard == []


def test_banned_word_inside_quotes_is_allowed():
    text = 'Labels love the phrase "skin-loving". It is not a formulation term.'
    assert lint(text, check_length=False).hard == []


def test_hashtags_allowed_only_on_last_line_when_enabled():
    ok = "A plain sentence about pH 5.5.\n\n#FormulationScience #IndianSkincare"
    assert not any("Hashtag" in h for h in lint(ok, check_length=False, allow_hashtags=True).hard)
    bad = "A #plain sentence.\n\n#FormulationScience"
    assert any("Hashtag" in h for h in lint(bad, check_length=False, allow_hashtags=True).hard)
    too_many = "Text.\n\n#A1 #B2 #C3 #D4"
    assert any("Hashtag" in h for h in lint(too_many, check_length=False, allow_hashtags=True).hard)


def test_word_count_bands():
    assert any("Word count" in h for h in lint("Too short. " * 20).hard)
    short = " ".join(["word"] * 200) + "."
    assert not any("Word count" in h for h in lint(short, short=True).hard)
    assert any("Word count" in h for h in lint(short).hard)


def test_well_formed_placeholders_pass_and_are_found():
    text = "Our niacinamide is at [DATA NEEDED: niacinamide %, from Meera] in the serum."
    assert lint(text, check_length=False).hard == []
    assert find_placeholders(text) == ["[DATA NEEDED: niacinamide %, from Meera]"]


def test_verify_tags_are_moved_out_of_post():
    clean, flags = extract_verify("In India this is not regulated [VERIFY CURRENT REGULATION]. Next sentence.")
    assert "[VERIFY" not in clean and flags == ["[VERIFY CURRENT REGULATION]"]
    assert clean.startswith("In India this is not regulated.")


def test_soft_warnings():
    flat = " ".join(["The formulation sits at a stable level across the full batch run today."] * 30)
    soft = lint(flat, check_length=False).soft
    assert any("Rhythm" in w for w in soft)
    assert any("boundary" in w for w in soft)
    res = lint(GOOD_POST, opening_type="B", recent_opening_types=["B", "A"])
    assert any("Opening type B" in w for w in res.soft)
