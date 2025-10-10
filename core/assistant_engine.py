# core/assistant_engine.py
import os, json, re
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from difflib import SequenceMatcher
from openai import OpenAI

# ------------------ Config / Paths ------------------
A11Y_OUT_BASE = Path(os.getenv("A11Y_OUT_BASE", "out")).resolve()
WCAG_LIB_DIR = Path("wcag_lib").resolve()

# ------------------ Data Model ------------------
@dataclass
class ElementRecord:
    page_url: str
    rule_id: str
    impact: Optional[str]
    topic: Optional[str]          # e.g., "1.1.1"
    sc: Optional[str]             # normalized "1.1.1"
    selector: str
    name_guess: Optional[str]
    nearby_text: Optional[str]
    snippet: Optional[str]
    help_url: Optional[str]
    screenshot: Optional[str]
    source_slug: str
    ai_verdict: Optional[str]
    ai_reason: Optional[str]
    axe_help: Optional[str]

# ------------------ Regex / Helpers ------------------
_SC_REGEX = re.compile(r"\b([1-3]\.[0-9]\.[0-9])\b")
_WCAG_TAG_REGEX = re.compile(r"\bwcag([1-3])([0-9])([0-9])\b", re.I)
_CONTINUE_RE = re.compile(r"^\s*(continue|more|go on|keep going|elaborate|details?)\s*$", re.I)
_SCREENSHOT_RE = re.compile(
    r"\b(screenshot|image|show (?:pic|photo|image))\b(?:\s*(?:for|of)\s*(?P<target>.+))?$",
    re.I,
)
_TAG_RE = re.compile(r"\b(h[1-6]|button|a|img|input|label|nav|main|footer|header|svg)\b", re.I)

def _norm_sc(val: Optional[str]) -> Optional[str]:
    if not val: return None
    m = _SC_REGEX.search(val)
    if m: return m.group(1)
    m2 = _WCAG_TAG_REGEX.search(val)
    if m2: return ".".join(m2.groups())
    return None

def _read_json(p: Path) -> Optional[Any]:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def list_runs(base: Path = A11Y_OUT_BASE) -> List[Path]:
    if not base.exists(): return []
    return sorted([p for p in base.iterdir() if p.is_dir()], key=lambda x: x.stat().st_mtime, reverse=True)

# ------------------ Load a run ------------------
def load_run(slug_dir: Path) -> Tuple[List[ElementRecord], Dict[str, Any], Dict[str, Any]]:
    meta = _read_json(slug_dir / "metadata.json") or {}
    axe = _read_json(slug_dir / "axe_results.json") or {}
    page_url = meta.get("page_url", "")

    ai = _read_json(slug_dir / "ai_verdicts.json") or []
    cand = _read_json(slug_dir / "candidates.json") or []

    key_ai = {}
    for rec in ai:
        key = (rec.get("selector") or "", rec.get("axe_rule_id") or rec.get("rule_id") or "")
        key_ai[key] = rec

    records: List[ElementRecord] = []
    for c in cand:
        selector = c.get("selector") or ""
        rule = c.get("axe_rule_id") or c.get("rule_id") or ""
        help_url = c.get("help_url") or c.get("help") or None
        screenshot_rel = c.get("screenshot") or None
        topic = c.get("topic") or None
        sc = _norm_sc(topic) or _norm_sc(c.get("tags_str") or "") or None
        ai_rec = key_ai.get((selector, rule), {})

        records.append(
            ElementRecord(
                page_url=page_url,
                rule_id=rule,
                impact=c.get("impact") or ai_rec.get("impact"),
                topic=topic,
                sc=sc,
                selector=selector,
                name_guess=c.get("role_name_guess") or None,
                nearby_text=c.get("nearby_text") or None,
                snippet=c.get("snippet") or None,
                help_url=help_url,
                screenshot=str((slug_dir / screenshot_rel).resolve()) if screenshot_rel else None,
                source_slug=slug_dir.name,
                ai_verdict=(ai_rec.get("verdict") or ai_rec.get("status") or "").lower() or None,
                ai_reason=ai_rec.get("reason"),
                axe_help=c.get("help") or None,
            )
        )

    return records, meta, axe

# ------------------ Index builders / Matching ------------------
def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()

def build_element_index(records: List[ElementRecord]) -> List[Tuple[str, float, ElementRecord]]:
    hay: List[Tuple[str, float, ElementRecord]] = []
    for r in records:
        text_blob = " | ".join([
            r.selector or "",
            r.name_guess or "",
            r.nearby_text or "",
            r.snippet or "",
            r.rule_id or "",
            r.page_url or "",
        ])
        base = 0.0
        if r.name_guess: base = max(base, 0.65)
        if r.nearby_text: base = max(base, 0.50)
        if "button" in (r.snippet or "").lower(): base = max(base, 0.55)
        hay.append((text_blob, base, r))
    return hay

def _best_match(query: str, records: List[ElementRecord]) -> Optional[ElementRecord]:
    q = (query or "").lower().strip()
    # Rule id
    rid = [r for r in records if r.rule_id and r.rule_id.lower() in q]
    if rid: return rid[0]
    # Selector exact
    sel = [r for r in records if r.selector and r.selector.lower() in q]
    if sel: return sel[0]
    # Tag
    tm = _TAG_RE.search(q)
    if tm:
        tag = tm.group(1).lower()
        for r in records:
            if (r.snippet or "").lower().find(f"<{tag}") >= 0:
                return r
    # Fuzzy
    hay = build_element_index(records)
    scored = [(max(base, _similar(q, text)), rec) for (text, base, rec) in hay]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored else None

# ------------------ WCAG KB ------------------
def build_wcag_index() -> Dict[str, Dict[str, Any]]:
    idx = {}
    for j in WCAG_LIB_DIR.glob("sc-*.json"):
        sc = _norm_sc(j.stem.replace("sc-", ""))
        if sc:
            data = _read_json(j) or {}
            idx[sc] = data
    return idx

# ------------------ Run analytics (for SC queries) ------------------
def _elements_for_sc(records: List[ElementRecord], sc: str) -> List[ElementRecord]:
    return [r for r in records if (r.sc or "") == sc]

def _failed_for_sc(records: List[ElementRecord], sc: str) -> List[ElementRecord]:
    elems = _elements_for_sc(records, sc)
    def is_fail(r: ElementRecord) -> bool:
        v = (r.ai_verdict or "").lower().replace(" ", "_").replace("-", "_")
        return v in {"fail","failed","needs_change","needs_review","manual_review"} or (r.impact or "").lower() in {"serious","critical"}
    return [r for r in elems if is_fail(r)]

# ------------------ Screenshots ------------------
def _screenshots_dir(run_dir: Path) -> Path:
    return run_dir / "screenshots"

def _find_screenshots_by_token(run_dir: Path, token: str, limit:int=6) -> List[str]:
    shots = _screenshots_dir(run_dir)
    if not shots.exists(): return []
    t = token.lower().strip()
    hits = []
    for p in sorted(shots.glob("*.png")):
        if t in p.name.lower():
            hits.append(str(p.resolve()))
            if len(hits) >= limit: break
    return hits


def _client(api_key: Optional[str] = None) -> OpenAI:
    return OpenAI(api_key=api_key) if api_key else OpenAI()

def _model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT_GROUNDED = """You are an Accessibility QA assistant.

STRICT RULES:
- Use only the CONTEXT provided each turn (run artifacts + wcag_lib). Do not invent facts.
- Use chat history only for continuity/pronoun resolution. Re-derive facts from CONTEXT each turn.
- Never flip a deterministic PASS; you may add notes/risks only.

STYLE:
- Friendly, concise, technically accurate. Short paragraphs and bullets where helpful.
- When listing elements, show selector + short label (nearby text or name guess).
- 250–350 words unless the user asks for a deep dive.
"""

USER_TEMPLATE = """QUESTION:
{question}

CHAT HISTORY (last turns, truncated):
{history}

CONTEXT:
[PAGE] URL: {page_url}

[SC SUMMARY]
SC: {sc}
WCAG Topic: {wcag_topic}
Do:
{wcag_do}

Don't:
{wcag_dont}

Edge cases:
{wcag_edge}

Techniques:
{wcag_tech}

[SC ELEMENTS IN THIS RUN]
Total: {sc_total}
Elements:
{sc_elements}

[FAILED/MANUAL ITEMS FOR THIS SC]
Count: {sc_failed_total}
Failed elements:
{sc_failed}

[FOCUSED ELEMENT]
Selector: {selector}
Name guess: {name_guess}
Nearby text: {nearby_text}
Rule id: {rule_id}
Impact: {impact}
AI verdict: {ai_verdict}
AI reason: {ai_reason}
Help URL: {help_url}
HTML snippet (trimmed):
{snippet}

INSTRUCTIONS:
- If the user asked "what is SC ... and which elements failed", give the explanation then list elements and failed ones clearly.
- If they asked for a screenshot, say you will show it (the UI handles images).
- If no SC is identified, answer based on the focused element only; if none, say what is missing.
- Keep it grounded to this CONTEXT.
"""

SYSTEM_PROMPT_POLISH = """You are a careful copy editor.
- Improve grammar and flow.
- Do NOT add or remove facts.
- Keep within ~350 words unless the input exceeds it.
Return plain text.
"""

def _format_elem_line(r: ElementRecord) -> str:
    label = r.name_guess or r.nearby_text or ""
    label = f' — "{label}"' if label else ""
    verdict = f" [{r.ai_verdict}]" if r.ai_verdict else ""
    impact = f" (impact: {r.impact})" if r.impact else ""
    return f"- {r.selector}{label} — rule: {r.rule_id}{verdict}{impact}"

# ------------------ Public API ------------------
def answer_query(
    query: str,
    chosen_run_dir: Optional[Path],
    history: List[Dict[str, str]],
    last_elem_key: Optional[Dict[str, str]] = None,
    last_sc: Optional[str] = None,
    fallback_to_latest: bool = True,
    max_images: int = 6,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Returns:
      {
        "text": <assistant answer>,
        "images": [paths],
        "element_key": {"selector","rule_id","source_slug"} | None,
        "sc": "1.4.3" | None
      }
    """
    # Resolve run
    if not chosen_run_dir or not chosen_run_dir.exists():
        if not fallback_to_latest:
            return {"text": "Pick a valid run under 'out/'.", "images": [], "element_key": None, "sc": None}
        runs = list_runs()
        if not runs:
            return {"text": "No runs found in 'out/'. Run an audit first.", "images": [], "element_key": None, "sc": None}
        chosen_run_dir = runs[0]

    records, meta, axe = load_run(chosen_run_dir)
    if not records:
        return {"text": "Run loaded but has no candidates. Re-run the audit with violations or incomplete items.", "images": [], "element_key": None, "sc": None}

    # Build WCAG context + rule->SC map, then fill missing SCs
    wcag_idx = build_wcag_index()
    rule_to_sc: Dict[str, str] = {}
    for sc_key, w in wcag_idx.items():
        for rule in (w.get("axe_rules_hint") or []):
            rule_to_sc[str(rule).lower()] = sc_key
    for r in records:
        if not r.sc and r.rule_id:
            mapped = rule_to_sc.get(r.rule_id.lower())
            if mapped:
                r.sc = mapped

    # Intent parsing
    q = (query or "").strip()
    want_continue = bool(_CONTINUE_RE.match(q))
    ss_match = _SCREENSHOT_RE.search(q)
    want_screenshot = bool(ss_match)
    screenshot_target = (ss_match.group("target") if ss_match and ss_match.group("target") else "").strip()
    # Greeting/small talk
    if re.search(r"\b(hi|hello|hey|howdy|thanks|thank you)\b", q, re.I) and not (want_screenshot or _SC_REGEX.search(q)):
        return {"text": "Hi! I’m your WCAG assistant. Ask me about a guideline (e.g., “what is 1.1.1?”), an element (e.g., “inspect .nav-active”), or say “show screenshot” for the current item.", "images": [], "element_key": last_elem_key, "sc": last_sc}

    # Determine SC
    sc_from_q = None
    m = _SC_REGEX.search(q)
    if m: sc_from_q = m.group(1)
    if not sc_from_q:
        m2 = _WCAG_TAG_REGEX.search(q)
        if m2: sc_from_q = ".".join(m2.groups())

    # Resolve focused element
    best: Optional[ElementRecord] = None
    if want_continue and last_elem_key:
        best = next((r for r in records if r.selector == last_elem_key.get("selector") and r.rule_id == last_elem_key.get("rule_id") and r.source_slug == last_elem_key.get("source_slug")), None)
    elif want_screenshot and screenshot_target:
        # filename token first
        imgs = _find_screenshots_by_token(chosen_run_dir, screenshot_target, limit=max_images)
        if imgs:
            elem_key = None
            cand = _best_match(screenshot_target, records)
            if cand:
                elem_key = {"selector": cand.selector, "rule_id": cand.rule_id, "source_slug": cand.source_slug}
            return {"text": f"Screenshot(s) matching “{screenshot_target}”.", "images": imgs, "element_key": elem_key, "sc": cand.sc if cand else sc_from_q or last_sc}
        # otherwise best match by text
        best = _best_match(screenshot_target, records)
    elif "screenshot" in q.lower() and last_elem_key:
        best = next((r for r in records if r.selector == last_elem_key.get("selector") and r.rule_id == last_elem_key.get("rule_id") and r.source_slug == last_elem_key.get("source_slug")), None)
        if best and best.screenshot and Path(best.screenshot).exists():
            return {
                "text": "Here’s the screenshot for the current element.",
                "images": [best.screenshot],
                "element_key": {"selector": best.selector, "rule_id": best.rule_id, "source_slug": best.source_slug},
                "sc": best.sc or sc_from_q or last_sc,
            }
    else:
        best = _best_match(q, records)

    # Developer inspect mode (tag/selector)
    if _TAG_RE.search(q) or re.search(r"\b(inspect|details|show me|info for)\b", q, re.I):
        token = _TAG_RE.search(q).group(1) if _TAG_RE.search(q) else q
        matches = []
        for r in records:
            if (r.selector and token.lower() in r.selector.lower()) or ((r.snippet or "").lower().find(f"<{token.lower()}") >= 0):
                matches.append(r)
        if matches:
            imgs = []
            for r in matches:
                if r.screenshot and Path(r.screenshot).exists():
                    imgs.append(r.screenshot)
                if len(imgs) >= max_images: break
            lines = []
            for r in matches[:10]:
                sc_txt = r.sc or "unknown"
                verdict = r.ai_verdict or "unknown"
                lines.append(f"- {r.selector} — rule {r.rule_id}, SC {sc_txt}, verdict {verdict}")
            text = "Element details:\n" + "\n".join(lines)
            ek = {"selector": matches[0].selector, "rule_id": matches[0].rule_id, "source_slug": matches[0].source_slug}
            return {"text": text, "images": imgs, "element_key": ek, "sc": matches[0].sc or sc_from_q or last_sc}

    # Build WCAG context for effective SC
    effective_sc = (sc_from_q or (best.sc if best else None) or last_sc)
    wcag_topic = wcag_do = wcag_dont = wcag_edge = wcag_tech = ""
    if effective_sc and effective_sc in wcag_idx:
        w = wcag_idx[effective_sc]
        wcag_topic = (w.get("topic") or f"SC {effective_sc}")
        wcag_do   = "\n- ".join(w.get("do", [])) if isinstance(w.get("do"), list) else str(w.get("do") or "")
        wcag_dont = "\n- ".join(w.get("dont", [])) if isinstance(w.get("dont"), list) else str(w.get("dont") or "")
        wcag_edge = "\n- ".join(w.get("edgecases", [])) if isinstance(w.get("edgecases"), list) else str(w.get("edgecases") or "")
        wcag_tech = "\n- ".join(w.get("techniques", [])) if isinstance(w.get("techniques"), list) else str(w.get("techniques") or "")

    # Deterministic SC analytics
    sc_elements = []
    sc_failed = []
    if effective_sc:
        sc_elements = _elements_for_sc(records, effective_sc)
        sc_failed = _failed_for_sc(records, effective_sc)

    sc_elem_text = "\n".join(_format_elem_line(r) for r in sc_elements[:25]) if sc_elements else "—"
    sc_failed_text = "\n".join(_format_elem_line(r) for r in sc_failed[:25]) if sc_failed else "—"

    # Compose grounded prompt
    page_url = (meta or {}).get("page_url", "")
    snippet = (best.snippet or "")[:1800] if best else ""
    user_prompt = USER_TEMPLATE.format(
        question=("Continue with deeper details for the same element and/or SC." if want_continue else query),
        history="\n".join([f"{m['role']}: {m['content']}" for m in history[-6:]]),
        page_url=page_url,
        sc=(effective_sc or "—"),
        wcag_topic=wcag_topic,
        wcag_do=wcag_do,
        wcag_dont=wcag_dont,
        wcag_edge=wcag_edge,
        wcag_tech=wcag_tech,
        sc_total=len(sc_elements),
        sc_elements=sc_elem_text,
        sc_failed_total=len(sc_failed),
        sc_failed=sc_failed_text,
        selector=best.selector if best else "",
        name_guess=best.name_guess if best else "",
        nearby_text=best.nearby_text if best else "",
        rule_id=best.rule_id if best else "",
        impact=best.impact if best else "",
        ai_verdict=best.ai_verdict if best else "",
        ai_reason=best.ai_reason if best else "",
        help_url=best.help_url if best else "",
        snippet=snippet,
    )

    client = _client(api_key=api_key)
    model = _model()
    grounded = client.chat.completions.create(
        model=model,
        temperature=0.15,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_GROUNDED},
            {"role": "user", "content": user_prompt},
        ],
    ).choices[0].message.content.strip()

    polished = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_POLISH},
            {"role": "user", "content": grounded},
        ],
    ).choices[0].message.content.strip()

        # Only attach images when the user explicitly asks for screenshots.
    images = []
    if want_screenshot and best and best.screenshot and Path(best.screenshot).exists():
        images.append(best.screenshot)

    elem_key = None
    if best:
        elem_key = {"selector": best.selector, "rule_id": best.rule_id, "source_slug": best.source_slug}

    return {"text": polished, "images": images[:max_images], "element_key": elem_key, "sc": effective_sc}
