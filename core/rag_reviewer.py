#!/usr/bin/env python3
import json, os, pathlib, re
from typing import Dict, Any, List, Tuple
from hashlib import sha256

BASE = pathlib.Path(__file__).parent.parent.resolve()

# ===================== helpers =====================

_SC_REGEX = re.compile(r"(\d)\.(\d)\.(\d)")
_WCAG_TAG_REGEX = re.compile(r"wcag(\d)(\d)(\d)$", re.IGNORECASE)

def _norm_sc_from_topic(topic: str) -> str:
    """Accept 'SC-1.1.1', '1.1.1', 'wcag111' → '1.1.1' (or '' if none)."""
    t = (topic or "").strip()
    m = _SC_REGEX.search(t)
    if m:
        return ".".join(m.groups())
    m = _WCAG_TAG_REGEX.search(t)
    if m:
        return ".".join(m.groups())
    t = t.lower().replace("sc-", "").strip()
    m = _SC_REGEX.search(t)
    return ".".join(m.groups()) if m else ""

def _scs_from_candidate(c: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    Extract primary SC and all SCs from candidate.
    Prefers candidate['sc_list'] (from axe tags). Falls back to topic.
    """
    sc_list: List[str] = []
    for t in (c.get("sc_list") or []):
        m = _WCAG_TAG_REGEX.match(str(t))
        if m:
            sc_list.append(".".join(m.groups()))
    sc_primary = sc_list[0] if sc_list else (_norm_sc_from_topic(c.get("topic", "")) or "")
    return sc_primary, sc_list

def load_techniques() -> List[Dict[str, Any]]:
    lib = []
    for p in (BASE / "wcag_lib").glob("*.json"):
        try:
            lib.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return lib

def retrieve_for_sc(sc: str, techniques: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Find a techniques doc for the SC (e.g., '1.3.1'). Fallback to {}."""
    sc_l = sc.lower()
    for t in techniques:
        tags = {str(x).lower() for x in t.get("tags", [])}
        topic = (t.get("topic") or "").lower()
        if f"sc-{sc_l}" in tags or sc_l in tags or sc_l in topic:
            return t
    return {}

def _synth_context(sc: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal context if wcag_lib lacks a doc."""
    return {
        "topic": f"SC {sc}" if sc else "Unmapped rule",
        "do": [
            "Apply WCAG techniques conservatively for this SC.",
            "Prefer 'needs-change' when meaning or programmatic name is unclear."
        ],
        "dont": [
            "Do not approve ambiguous or redundant alternatives.",
            "Do not rely on visual presentation alone."
        ],
        "edge_cases": []
    }

# ===================== prompt builder =====================

def build_prompt(template_path: pathlib.Path, sc: str, tech_doc: Dict[str, Any], c: Dict[str, Any]) -> str:
    """
    Build the semantic review prompt for one candidate.
    Appends AXE_DIAGNOSTICS after formatting. Raises a clear error if the template has unescaped braces.
    """
    # If no technique doc, synthesize a tiny one and fold in axe help as guidance
    if not tech_doc:
        tech_doc = _synth_context(sc, c)
        if c.get("axe_help"):
            tech_doc.setdefault("do", []).append(f"Consider axe help: {c.get('axe_help')}")
        if c.get("axe_help_url"):
            tech_doc.setdefault("do", []).append(f"Ref: {c.get('axe_help_url')}")

    techniques_context = json.dumps({
        "topic": tech_doc.get("topic"),
        "do": tech_doc.get("do"),
        "dont": tech_doc.get("dont"),
        "edge_cases": tech_doc.get("edge_cases"),
    }, indent=2, ensure_ascii=False)

    tpl = template_path.read_text(encoding="utf-8")

    # Include axe WHY fields to improve semantic judgment
    why_pack = {
        "failureSummary": c.get("failureSummary"),
        "why_any": c.get("why_any"),
        "why_all": c.get("why_all"),
        "why_none": c.get("why_none"),
        "page_url": c.get("page_url"),
    }

    # Variables to interpolate
    fmt_vars = {
        "topic_label": (f"SC {sc}" if sc else (c.get("topic") or "Unmapped")),
        "techniques_context": techniques_context,
        "selector": c.get("selector", ""),
        "html_snippet": (c.get("html_snippet", "")[:1200] or ""),
        "attributes": json.dumps(c.get("attributes", {}), ensure_ascii=False),
        "role_name": c.get("role_name_guess", ""),
        "nearby_text": (c.get("nearby_text", "")[:800] or ""),
        "acc_snapshot": json.dumps(c.get("acc_snapshot", {}), ensure_ascii=False)[:1200],
        "rule_id": c.get("axe_rule_id", ""),
        "axe_help": c.get("axe_help", ""),
        "impact": c.get("impact", ""),
    }

    try:
        prompt = tpl.format(**fmt_vars)
    except KeyError as e:
        # Most common cause: literal { } in the template JSON block not escaped as {{ }}
        raise RuntimeError(
            "Template formatting error near placeholder "
            f"{e!s}. You likely have an unescaped '{{' or '}}' in the template. "
            "Double all literal braces in JSON examples (use '{{' and '}}')."
        ) from e
    except ValueError as e:
        # Unmatched '{' or '}' or invalid format string
        raise RuntimeError(
            "Template formatting error (invalid format string). "
            "Ensure all literal braces are escaped as '{{' and '}}'."
        ) from e

    # Append diagnostics after successful formatting
    prompt += "\n\nAXE_DIAGNOSTICS:\n" + json.dumps(why_pack, ensure_ascii=False, indent=2)
    return prompt

# ===================== LLM switch =====================

def _use_live_llm() -> bool:
    return os.environ.get("A11Y_USE_LLM") == "1" and bool(os.environ.get("OPENAI_API_KEY"))

def _run_llm_openai(prompt: str) -> Dict[str, Any]:
    """
    Minimal OpenAI call using Chat Completions.
    Requires OPENAI_API_KEY; model from OPENAI_MODEL or defaults to gpt-4o-mini.
    """
    try:
        from openai import OpenAI
        client = OpenAI()
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content":
                 "You are an accessibility reviewer. "
                 "Return ONLY a compact JSON object with keys: "
                 "type, verdict, reason, confidence, techniques_used."},
                {"role": "user", "content": prompt}
            ],
        )
        text = resp.choices[0].message.content.strip()
        # Extract JSON
        s = text.find("{"); e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            text = text[s:e+1]
        data = json.loads(text)
        for k in ["type","verdict","reason","confidence","techniques_used"]:
            if k not in data:
                raise ValueError(f"Missing key: {k}")
        return data
    except Exception as e:
        return {
            "type": "informative",
            "verdict": "needs-change",
            "reason": f"LLM fallback (parse/error): {e}",
            "confidence": 0.4,
            "techniques_used": ["fallback"]
        }

def run_llm(prompt: str) -> Dict[str, Any]:
    if _use_live_llm():
        return _run_llm_openai(prompt)
    # Offline-safe stub
    return {
        "type": "informative",
        "verdict": "needs-change",
        "reason": "Demo verdict (no live LLM or A11Y_USE_LLM=0).",
        "confidence": 0.5,
        "techniques_used": ["demo-only"]
    }

# ===================== main review =====================

def review(out_dir: pathlib.Path):
    candidates_path = out_dir / "candidates.json"
    if not candidates_path.exists():
        raise FileNotFoundError("candidates.json not found; run axe_runner first.")
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    techniques = load_techniques()

    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    skip_best_practice = os.environ.get("A11Y_SKIP_BEST_PRACTICE") == "1"

    results = []
    for i, c in enumerate(candidates):
        # Determine SCs
        sc_primary, sc_all = _scs_from_candidate(c)

        # Optionally skip non-WCAG candidates
        is_wcag = bool(sc_primary)
        if skip_best_practice and not is_wcag:
            continue

        # Retrieve technique doc for primary SC (if any)
        tech_doc = retrieve_for_sc(sc_primary, techniques) if is_wcag else {}

        # Build prompt
        template_path = BASE / "prompts" / "semantic_review_template.txt"
        prompt = build_prompt(template_path, sc_primary, tech_doc, c)

        # Persist prompt for audit
        pfile = prompts_dir / f"{i:03d}_{sc_primary or (c.get('topic') or 'UNMAPPED')}_{c.get('axe_rule_id','')}.txt"
        pfile.write_text(prompt, encoding="utf-8")

        # Run AI (or stub)
        verdict = run_llm(prompt)

        # Traceability hash
        prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()[:16]

        results.append({
            "page_url": c.get("page_url"),
            "topic": c.get("topic"),
            "SC": sc_primary,            # normalized SC (e.g., '1.1.1'), '' if non-WCAG
            "sc_list": sc_all,           # all SCs from axe tags
            "selector": c.get("selector"),
            "axe_rule_id": c.get("axe_rule_id"),
            "impact": c.get("impact"),
            "ai_verdict": verdict,
            "screenshot": c.get("screenshot"),
            "axe_help_url": c.get("axe_help_url"),
            "prompt_hash": prompt_hash
        })

    (out_dir / "ai_verdicts.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return {"reviewed": len(results)}
