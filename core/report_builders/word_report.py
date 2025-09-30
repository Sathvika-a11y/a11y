#!/usr/bin/env python3
import json, pathlib, os
from typing import Dict, Any, Tuple
from docx import Document
from docx.shared import Pt, Inches
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE

def _abs_file_url(p: pathlib.Path) -> str:
    ap = pathlib.Path(p).resolve()
    return "file:///" + str(ap).replace("\\", "/")  # use fwd slashes for Word

def _add_hyperlink(paragraph, url: str, text: str, color="1155CC", underline=True):
    part = paragraph.part
    r_id = part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    if color:
        c = OxmlElement("w:color"); c.set(qn("w:val"), color); rPr.append(c)
    if underline:
        u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rPr.append(u)
    new_run.append(rPr)

    t = OxmlElement("w:t"); t.text = text
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)
    return hyperlink

def _resolve_screenshot(out_dir: pathlib.Path, shot_val: str) -> pathlib.Path:
    """Return a concrete path to the screenshot if it exists; else Path('')."""
    if not shot_val:
        return pathlib.Path("")
    p = pathlib.Path(str(shot_val).strip())
    if not p.is_absolute():
        p = (out_dir / p).resolve()
    return p if p.exists() else pathlib.Path("")

def build_word(out_dir: pathlib.Path, docx_path: pathlib.Path, url: str):
    out_dir = pathlib.Path(out_dir)
    axe = json.loads((out_dir / "axe_results.json").read_text(encoding="utf-8"))
    cands = json.loads((out_dir / "candidates.json").read_text(encoding="utf-8"))
    ai = json.loads((out_dir / "ai_verdicts.json").read_text(encoding="utf-8"))

    # Build a fallback map from candidates: (selector, rule) -> screenshot path
    cand_lookup: Dict[Tuple[str,str], str] = {}
    for c in cands:
        sel = c.get("selector") or ""
        rid = c.get("axe_rule_id") or c.get("rule_id") or ""
        if sel and rid and c.get("screenshot"):
            cand_lookup[(sel, rid)] = c["screenshot"]

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(11)

    doc.add_heading("Accessibility Audit Report", level=0)
    doc.add_paragraph(f"Target URL: {url}")
    doc.add_paragraph("Methodology: axe-core baseline + semantic review (RAG).")

    # Overview
    doc.add_heading("Overview", level=1)
    t = doc.add_table(rows=1, cols=4)
    hdr = t.rows[0].cells
    hdr[0].text = "Bucket"
    hdr[1].text = "Count"
    hdr[2].text = "AI Candidates"
    hdr[3].text = "AI Verdicts"
    rows = [
        ("Violations", str(len(axe.get("violations", []))), "", ""),
        ("Incomplete", str(len(axe.get("incomplete", []))), "", ""),
        ("Passes",     str(len(axe.get("passes", []))),     "", ""),
        ("AI Candidates", str(len(cands)), "", ""),
        ("AI Verdicts",   str(len(ai)),    "", ""),
    ]
    for r in rows:
        row = t.add_row().cells
        row[0].text, row[1].text, row[2].text, row[3].text = r

    # Findings
    doc.add_heading("Findings (AI verdicts)", level=1)
    images_ok = 0
    for i, rec in enumerate(ai, start=1):
        topic = rec.get("topic") or rec.get("SC") or "Unmapped"
        rule  = rec.get("axe_rule_id") or ""
        sel   = rec.get("selector") or ""

        doc.add_heading(f"{i}. {topic} — {rule}", level=2)
        doc.add_paragraph(f"Selector: {sel}")
        v = rec.get("ai_verdict") or {}
        doc.add_paragraph(f"Verdict: {v.get('verdict')} (confidence {v.get('confidence')})")
        if v.get("reason"):
            doc.add_paragraph(f"Reason: {v.get('reason')}")
        if rec.get("axe_help_url"):
            doc.add_paragraph(f"Ref: {rec.get('axe_help_url')}")

        # Resolve screenshot with fallback to candidates
        shot_val = rec.get("screenshot") or cand_lookup.get((sel, rule)) or ""
        shot_path = _resolve_screenshot(out_dir, shot_val)

        if shot_path:
            doc.add_paragraph("Screenshot:")
            try:
                doc.add_picture(str(shot_path), width=Inches(3.6))
                images_ok += 1
            except Exception as e:
                doc.add_paragraph(f"(Could not embed image: {shot_path.name} — {e})")
            # Clickable link for full-size
            p = doc.add_paragraph()
            try:
                _add_hyperlink(p, _abs_file_url(shot_path), "Open full-size image")
            except Exception:
                p.add_run(str(shot_path))
        else:
            doc.add_paragraph("(No screenshot available for this item)")

    # Footer note with count
    doc.add_paragraph(f"\nEmbedded screenshots: {images_ok} of {len(ai)}")

    doc.save(docx_path)
