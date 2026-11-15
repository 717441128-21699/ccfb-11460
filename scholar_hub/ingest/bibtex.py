"""BibTeX 导出。"""
from __future__ import annotations

import re
import sqlite3

_BIB_ESCAPE = {
    "&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}",
}


def _escape(value: str) -> str:
    return "".join(_BIB_ESCAPE.get(ch, ch) for ch in (value or ""))


def _bib_key(work_id: int, year: str | None, first_author: str | None) -> str:
    tail = re.sub(r"[^A-Za-z]+", "", first_author or "anon")[:12] or "anon"
    return f"work{work_id}_{tail or 'anon'}_{year or 'nd'}"


def _entry_type(rec_type: str | None) -> str:
    return {
        "journal-article": "article",
        "conference": "inproceedings",
        "preprint": "unpublished",
        "report": "techreport",
    }.get(rec_type or "", "misc")


def export_work_bibtex(conn: sqlite3.Connection, work_id: int) -> str:
    work = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    if work is None:
        raise ValueError(f"work {work_id} 不存在")
    records = conn.execute(
        """SELECT r.* FROM records r
           JOIN record_work rw ON rw.record_id=r.id
           WHERE rw.work_id=? ORDER BY r.date DESC, r.id""", (work_id,)).fetchall()
    if not records:
        return ""
    primary = next((r for r in records if r["type"] != "preprint"), records[0])
    authors = conn.execute(
        """SELECT p.display_name FROM record_creators rc
           JOIN persons p ON p.id=rc.person_id
           WHERE rc.record_id=? ORDER BY rc.position""",
        (primary["id"],)).fetchall()
    author_str = " and ".join(_escape(a["display_name"]) for a in authors)
    fields: list[tuple[str, str]] = [
        ("title", _escape(work["title"] or primary["title"] or "")),
        ("author", author_str),
        ("year", work["year"] or primary["date"] or ""),
    ]
    if work["canonical_doi"]:
        fields.append(("doi", work["canonical_doi"]))
    if primary["publisher"]:
        key = "journal" if primary["type"] == "journal-article" else "publisher"
        fields.append((key, _escape(primary["publisher"])))
    if primary["language"]:
        fields.append(("language", primary["language"]))
    subjects = [s["subject"] for s in conn.execute(
        "SELECT subject FROM record_subjects WHERE record_id=?",
        (primary["id"],))]
    if subjects:
        fields.append(("keywords", _escape("; ".join(subjects))))
    versions = [r["doi"] for r in records if r["doi"] and r["id"] != primary["id"]]
    if versions:
        fields.append(("note", _escape("other versions: " + ", ".join(versions))))

    lines = [f"@{_entry_type(primary['type'])}{{{_bib_key(work_id, work['year'], authors[0]['display_name'] if authors else None)},"]
    for k, v in fields:
        lines.append(f"  {k} = {{{v}}},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


def export_person_bibtex(conn: sqlite3.Connection, person_id: int) -> str:
    work_ids = [r["work_id"] for r in conn.execute(
        """SELECT DISTINCT rw.work_id FROM record_work rw
           JOIN record_creators rc ON rc.record_id=rw.record_id
           WHERE rc.person_id=? ORDER BY rw.work_id""", (person_id,))]
    return "\n\n".join(export_work_bibtex(conn, wid) for wid in work_ids)
