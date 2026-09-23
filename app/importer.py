"""Backlog importer.

    python -m app.importer notes data/notes/
    python -m app.importer published data/published/

The Telegram Bot API can't read channel history from before the bot joined, so the backlog comes from files.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings
from .db import DB

# A line made only of ---, ===, *** or ━━━ separates fragments inside one export file.
DELIM_RE = re.compile(r"^\s*(-{3,}|={3,}|\*{3,}|━{3,})\s*$", re.M)
PIECE_HEADER_RE = re.compile(r"^\s*──\s*([a-z]+_(?:post_)?\d+)\s*──\s*$", re.M)


@dataclass
class Piece:
    name: str
    kind: str      # linkedin | newsletter
    category: str
    subject: str | None
    text: str


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace(" ", " ")
    text = text.replace("—", " - ").replace("�", "-")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix.lower() == ".rtf":
        from striprtf.striprtf import rtf_to_text
        return _clean(rtf_to_text(raw.decode("latin-1"), encoding="cp1252", errors="replace"))
    for enc in ("utf-8", "cp1252"):
        try:
            return _clean(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return _clean(raw.decode("utf-8", errors="replace"))


def split_fragments(text: str) -> list[str]:
    parts = [p.strip() for p in DELIM_RE.split(text) if p and p.strip() and not DELIM_RE.fullmatch(p)]
    return [p for p in parts if not re.fullmatch(r"-+|=+|\*+|━+", p)]


def load_note_files(folder: Path) -> list[tuple[str, str]]:
    """Returns (source_name, text). One note per file, or split on a clear delimiter line."""
    out: list[tuple[str, str]] = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in {".txt", ".md", ".rtf"}:
            continue
        frags = split_fragments(read_text_file(path))
        for i, frag in enumerate(frags, 1):
            out.append((path.name if len(frags) == 1 else f"{path.name}#{i}", frag))
    return out


def _unwrap(block: str) -> str:
    """Join PDF-wrapped lines into paragraphs; blank lines separate paragraphs."""
    paras = re.split(r"\n\s*\n", block.strip())
    return "\n\n".join(" ".join(ln.strip() for ln in p.splitlines() if ln.strip()) for p in paras if p.strip())


def parse_published_text(text: str) -> list[Piece]:
    """Split the seed document on '── linkedin_post_001 ──' style headers."""
    pieces: list[Piece] = []
    matches = list(PIECE_HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end]
        body = re.split(r"^\s*━{3,}", body, flags=re.M)[0]  # drop section banners / END OF DOCUMENT
        cat = re.search(r"^\s*Category:\s*(.+)$", body, re.M)
        subj = re.search(r"^\s*Subject:\s*(.+)$", body, re.M)
        body = re.sub(r"^\s*(Category|Subject):.*$", "", body, flags=re.M)
        name = m.group(1)
        pieces.append(Piece(name=name, kind="linkedin" if name.startswith("linkedin") else "newsletter",
                            category=cat.group(1).strip() if cat else "Unknown",
                            subject=subj.group(1).strip() if subj else None, text=_unwrap(body)))
    return pieces


def load_published(folder: Path) -> list[Piece]:
    pieces: list[Piece] = []
    for path in sorted(folder.iterdir()):
        suf = path.suffix.lower()
        if suf == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(p.extract_text(extraction_mode="layout") for p in PdfReader(str(path)).pages)
            pieces.extend(parse_published_text(text))
        elif suf in {".txt", ".md"}:
            text = read_text_file(path)
            if PIECE_HEADER_RE.search(text):
                pieces.extend(parse_published_text(text))
            else:
                cat = re.search(r"^\s*Category:\s*(.+)$", text, re.M)
                body = re.sub(r"^\s*(Category|Subject):.*$", "", text, flags=re.M)
                kind = "linkedin" if "linkedin" in path.stem.lower() else "newsletter"
                pieces.append(Piece(path.stem, kind, cat.group(1).strip() if cat else "Unknown", None,
                                    _unwrap(body)))
    return pieces


def import_notes(db: DB, folder: Path) -> int:
    n = 0
    for name, text in load_note_files(folder):
        exists = db.one("SELECT id FROM notes WHERE source='import' AND file_id=?", (name,))
        if exists:
            continue
        # file_id doubles as the import key so re-running the importer is idempotent
        db.upsert_note(source="import", chat_id=None, message_id=None, type_="text", text=text, file_id=name)
        n += 1
    db.event("import_notes", count=n, folder=str(folder))
    return n


def import_published(db: DB, folder: Path) -> int:
    n = 0
    for p in load_published(folder):
        cur = db.one("SELECT id FROM exemplars WHERE name=?", (p.name,))
        if cur:
            db.run("UPDATE exemplars SET kind=?, category=?, text=? WHERE id=?", (p.kind, p.category, p.text, cur["id"]))
        else:
            db.run("INSERT INTO exemplars(kind, name, category, text) VALUES (?,?,?,?)",
                   (p.kind, p.name, p.category, p.text))
            n += 1
    db.event("import_published", count=n, folder=str(folder))
    return n


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"notes", "published"}:
        print(__doc__)
        return 2
    folder = Path(argv[1])
    if not folder.is_dir():
        print(f"Not a folder: {folder}")
        return 2
    db = DB(get_settings().db_file)
    if argv[0] == "notes":
        n = import_notes(db, folder)
        total = db.one("SELECT COUNT(*) c FROM notes WHERE source='import'")["c"]
        print(f"Imported {n} new notes ({total} imported notes in DB).")
    else:
        n = import_published(db, folder)
        rows = db.q("SELECT kind, COUNT(*) c FROM exemplars GROUP BY kind")
        print(f"Imported {n} new exemplars. " + ", ".join(f"{r['kind']}: {r['c']}" for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
