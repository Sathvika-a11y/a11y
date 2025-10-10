#!/usr/bin/env python3
# core/report_builders/excel_report.py
import json, re
from pathlib import Path
import pandas as pd

# --------------------------- helpers ---------------------------

def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default

_SC_RE = re.compile(r"(\d)\.(\d)\.(\d)")
_WCAG_TAG_RE = re.compile(r"wcag(\d)(\d)(\d)$", re.IGNORECASE)

def _norm_sc(s: str) -> str:
    if not s: return ""
    s = str(s).strip()
    m = _SC_RE.search(s)
    if m: return ".".join(m.groups())
    m = _WCAG_TAG_RE.search(s)
    if m: return ".".join(m.groups())
    s2 = s.lower().replace("sc-","").strip()
    m = _SC_RE.search(s2)
    return ".".join(m.groups()) if m else ""

def _topic_to_sc(topic: str) -> str:
    return _norm_sc(topic or "")

def _ensure_cols(df: pd.DataFrame, cols):
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df

def _safe_select(df: pd.DataFrame, wanted):
    have = [c for c in wanted if c in df.columns]
    return df[have] if have else pd.DataFrame(columns=wanted)

def _norm_path(out_dir: Path, v: str):
    """Return absolute path for screenshot if it exists, else None."""
    if not v:
        return None
    p = Path(v)
    if not p.is_absolute():
        p = out_dir / v
    try:
        return str(p.resolve()) if p.exists() else None
    except Exception:
        return None

def _add_file_hyperlinks(xw, sheet_name: str, df: pd.DataFrame, path_col: str, out_dir: Path, link_text: str = "open"):
    """
    Turn df[path_col] into clickable hyperlinks using xlsxwriter's write_url.
    Call this *after* df.to_excel for that sheet, while the workbook is still open.
    """
    if df.empty or path_col not in df.columns:
        return
    ws = xw.sheets.get(sheet_name)
    if ws is None:
        return
    col_idx = list(df.columns).index(path_col)
    rows = len(df)
    for i in range(rows):
        val = df.iloc[i, col_idx]
        if not val:
            continue
        full = _norm_path(out_dir, val)
        if not full:
            continue
        try:
            ws.write_url(i + 1, col_idx, f"external:{full}", string=str(link_text))
        except Exception:
            pass

def _ai_needs_change(val) -> bool:
    try:
        return bool((val or {}).get("verdict") == "needs-change")
    except Exception:
        return False

# --------------------------- main builder ---------------------------

def build_excel(out_dir: Path, xlsx_path: Path):
    """
    Inputs the builder expects in out_dir:
      - axe_results.json (with key 'axe_issues': list[dict])
      - candidates.json
      - ai_verdicts.json
      - ai/2_4_6/results.json  (optional)
      - keyboard_probe.json    (optional)
      - metadata.json          (for page_url)
    """
    out_dir = Path(out_dir)
    axe = _load_json(out_dir / "axe_results.json", {})
    meta = _load_json(out_dir / "metadata.json", {})
    page_url = meta.get("page_url", "")
    cands = _load_json(out_dir / "candidates.json", [])
    ai = _load_json(out_dir / "ai_verdicts.json", [])
    kb = _load_json(out_dir / "keyboard_probe.json", {})  # optional
    ai246_all = _load_json(out_dir / "ai" / "2_4_6" / "results.json", [])  # optional

    # ---------- DataFrames ----------
    df_cands = pd.DataFrame(cands)
    df_ai = pd.DataFrame(ai)
    df_overall = pd.DataFrame(axe.get("axe_issues", []))  # NEW: mechanical + hybrid pass/fail/review

    # Normalize SC & status fields across sources
    if not df_overall.empty:
        # Try to build SC column
        if "SC" not in df_overall.columns:
            df_overall["SC"] = None
        # Fill SC from sc/topic/axe_rule_id hints
        def _infer_sc(row):
            for k in ("SC","sc","topic","axe_rule_id","rule_id"):
                if k in row and row[k]:
                    val = str(row[k])
                    sc = _norm_sc(val)
                    if sc:
                        return sc
            return ""
        df_overall["SC"] = df_overall.apply(_infer_sc, axis=1)
        # Standardize status (pass/fail/review) if present
        if "status" not in df_overall.columns:
            if "verdict" in df_overall.columns:
                df_overall["status"] = df_overall["verdict"]
            else:
                df_overall["status"] = None

    # Derive SC for AI verdicts when missing
    if not df_ai.empty:
        if "SC" not in df_ai.columns or df_ai["SC"].isna().all():
            df_ai["SC"] = df_ai["topic"].map(_topic_to_sc)

    # ---------- Summary ----------
    df_summary = pd.DataFrame([{
        "Pages": 1,
        "page_url": page_url,
        "overall_issues": int(len(df_overall)) if not df_overall.empty else 0,
        "candidates": int(len(df_cands)) if not df_cands.empty else 0,
        "ai_reviewed": int(len(df_ai)) if not df_ai.empty else 0,
        "ai_2_4_6_items": int(len(ai246_all))
    }])

    # ---------- WCAG_Summary (per SC, from Overall_Issues) ----------
    if not df_overall.empty:
        # counts per SC x status (pass/fail/review)
        wcag = df_overall.copy()
        # Ensure 'status' present
        if "status" not in wcag.columns:
            wcag["status"] = "review"
        wcag["status"] = wcag["status"].astype(str).str.lower().replace({"": "review"})
        wcag["SC"] = wcag["SC"].astype(str).fillna("")
        wcag = wcag[wcag["SC"] != ""]
        pivot_counts = wcag.pivot_table(
            index="SC", columns="status", values="selector" if "selector" in wcag.columns else wcag.columns[0],
            aggfunc="count", fill_value=0
        ).reset_index()
    else:
        pivot_counts = pd.DataFrame(columns=["SC","fail","pass","review"])

    if not df_ai.empty:
        needs = (df_ai[df_ai["ai_verdict"].map(_ai_needs_change)]
                 .groupby("SC", dropna=False)["selector"].count()
                 .reset_index()
                 .rename(columns={"selector":"AI_needs_change"}))
        df_wcag_summary = pivot_counts.merge(needs, on="SC", how="left").fillna(0)
    else:
        df_wcag_summary = pivot_counts
        if "AI_needs_change" not in df_wcag_summary.columns:
            df_wcag_summary["AI_needs_change"] = 0

    # ---------- Candidates (clean view) ----------
    cand_cols = [
        "page_url","topic","sc_list","axe_rule_id","impact",
        "selector","screenshot","axe_help_url","failureSummary",
        "why_any","why_all","why_none"
    ]
    df_cands_out = _safe_select(df_cands, cand_cols)

    # ---------- Issue_Backlog (dev-ready from AI) ----------
    if not df_ai.empty:
        join_left = ["selector","axe_rule_id"]
        right = _safe_select(df_cands, join_left + ["page_url","screenshot","axe_help_url"]).drop_duplicates()
        df_backlog = df_ai.merge(right, on=[c for c in join_left if c in right.columns], how="left")

        def V(x,k):
            try: return (x or {}).get(k)
            except: return None
        df_backlog["verdict"]     = df_backlog["ai_verdict"].map(lambda x: V(x,"verdict"))
        df_backlog["reason"]      = df_backlog["ai_verdict"].map(lambda x: V(x,"reason"))
        df_backlog["confidence"]  = df_backlog["ai_verdict"].map(lambda x: V(x,"confidence"))

        df_backlog = _ensure_cols(df_backlog, [
            "page_url","SC","axe_rule_id","impact","selector",
            "screenshot","axe_help_url","verdict","confidence","reason"
        ])
        keep = ["page_url","SC","axe_rule_id","impact","selector","screenshot","axe_help_url","verdict","confidence","reason"]
        df_backlog = _safe_select(df_backlog, keep).drop_duplicates()
        sort_keys = [c for c in ["SC","impact","axe_rule_id","selector"] if c in df_backlog.columns]
        if sort_keys:
            df_backlog = df_backlog.sort_values(sort_keys, na_position="last")
    else:
        df_backlog = pd.DataFrame(columns=[
            "page_url","SC","axe_rule_id","impact","selector","screenshot","axe_help_url","verdict","confidence","reason"
        ])

    # ---------- Overall_Issues (from axe_results.axe_issues) ----------
    if not df_overall.empty:
        # Standardize a nice set of columns for display
        df_overall = df_overall.copy()
        # prefer 'rule_id' then 'axe_rule_id'
        if "rule_id" not in df_overall.columns and "axe_rule_id" in df_overall.columns:
            df_overall["rule_id"] = df_overall["axe_rule_id"]
        display_cols = [
            "page_url","SC","status","rule_id","impact",
            "selector","screenshot","note","detector","source","axe_help_url","evidence"
        ]
        # Fill page_url default
        if "page_url" not in df_overall.columns:
            df_overall["page_url"] = page_url
        df_overall_out = _safe_select(df_overall, display_cols)
    else:
        df_overall_out = pd.DataFrame(columns=[
            "page_url","SC","status","rule_id","impact","selector","screenshot","note","detector","source","axe_help_url","evidence"
        ])

    # ---------- Keyboard sheets ----------
    kb_summary = pd.DataFrame([kb.get("summary", {})]) if kb else pd.DataFrame([{}])
    df_kb_unreach = pd.DataFrame(kb.get("unreachable", [])) if kb else pd.DataFrame([])
    df_kb_actfail = pd.DataFrame(kb.get("activations", [])) if kb else pd.DataFrame([])
    df_kb_tabneg  = pd.DataFrame(kb.get("tabindex_neg1", [])) if kb else pd.DataFrame([])

    # ---------- AI_2_4_6 worksheet ----------
    df_ai246 = pd.DataFrame(ai246_all)
    if not df_ai246.empty:
        df_ai246 = _ensure_cols(df_ai246, ["selector","type","sc","verdict","reasons","suggestion","screenshot"])
        # attach screenshots from candidates for 2.4.6 (runner:ai-2.4.6-local)
        c2 = df_cands[df_cands.get("axe_rule_id","").astype(str).isin(["ai:2.4.6","runner:ai-2.4.6-local"])] if not df_cands.empty else pd.DataFrame([])
        if not c2.empty:
            c2 = _safe_select(c2, ["selector","screenshot"]).drop_duplicates()
            df_ai246 = df_ai246.merge(c2, on="selector", how="left", suffixes=("","_cand"))

            # prefer per-item screenshot from AI, else from candidate map
            if "screenshot_cand" in df_ai246.columns:
                df_ai246["screenshot"] = df_ai246["screenshot"].fillna(df_ai246["screenshot_cand"])
                df_ai246.drop(columns=["screenshot_cand"], inplace=True, errors="ignore")

    # ---------- write workbook ----------
    with pd.ExcelWriter(xlsx_path, engine="xlsxwriter") as xw:
        # Base tabs
        df_summary.to_excel(xw, sheet_name="Summary", index=False)
        df_wcag_summary.to_excel(xw, sheet_name="WCAG_Summary", index=False)
        df_overall_out.to_excel(xw, sheet_name="Overall_Issues", index=False)
        df_cands_out.to_excel(xw, sheet_name="Candidates", index=False)
        df_backlog.to_excel(xw, sheet_name="Issue_Backlog", index=False)
        df_ai.to_excel(xw, sheet_name="AI_Verdicts", index=False)

        # Keyboard tabs
        kb_summary.to_excel(xw, sheet_name="Keyboard_Summary", index=False)
        df_kb_unreach.to_excel(xw, sheet_name="Keyboard_Unreachable", index=False)
        df_kb_actfail.to_excel(xw, sheet_name="Keyboard_Activations", index=False)
        df_kb_tabneg.to_excel(xw, sheet_name="Keyboard_TabIndex_-1", index=False)

        # AI 2.4.6
        if not df_ai246.empty:
            df_ai246.to_excel(xw, sheet_name="AI_2_4_6", index=False)

        # Autofilters
        for sheet in [
            "Summary","WCAG_Summary","Overall_Issues","Candidates","Issue_Backlog","AI_Verdicts",
            "Keyboard_Summary","Keyboard_Unreachable","Keyboard_Activations","Keyboard_TabIndex_-1","AI_2_4_6"
        ]:
            try:
                ws = xw.sheets.get(sheet)
                if ws:
                    ws.autofilter(0, 0, ws.dim_rowmax, ws.dim_colmax)
            except Exception:
                pass

        # Make screenshot cells clickable (best-effort)
        try: _add_file_hyperlinks(xw, "Overall_Issues",       df_overall_out, "screenshot", out_dir)
        except Exception: pass
        try: _add_file_hyperlinks(xw, "Candidates",            df_cands_out,   "screenshot", out_dir)
        except Exception: pass
        try: _add_file_hyperlinks(xw, "Issue_Backlog",         df_backlog,     "screenshot", out_dir)
        except Exception: pass
        try: _add_file_hyperlinks(xw, "AI_Verdicts",           df_ai,          "screenshot", out_dir)
        except Exception: pass
        try: _add_file_hyperlinks(xw, "Keyboard_Unreachable",  df_kb_unreach,  "screenshot", out_dir)
        except Exception: pass
        try: _add_file_hyperlinks(xw, "Keyboard_Activations",  df_kb_actfail,  "screenshot", out_dir)
        except Exception: pass
        try: _add_file_hyperlinks(xw, "Keyboard_TabIndex_-1",  df_kb_tabneg,   "screenshot", out_dir)
        except Exception: pass
        try: _add_file_hyperlinks(xw, "AI_2_4_6",              df_ai246,       "screenshot", out_dir)
        except Exception: pass
