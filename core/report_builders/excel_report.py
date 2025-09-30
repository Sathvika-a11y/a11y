#!/usr/bin/env python3
import json, pathlib, re, os
import pandas as pd

# ----------------- helpers -----------------

def _sc_from_tags(tags):
    scs = []
    for t in tags or []:
        m = re.match(r"wcag(\d)(\d)(\d)$", str(t).lower())
        if m:
            scs.append(".".join(m.groups()))
    return scs

def _flatten_bucket(axe, bucket, page_url: str):
    out = []
    for r in axe.get(bucket, []):
        scs = _sc_from_tags(r.get("tags", []))
        nodes = r.get("nodes", []) or []
        for n in nodes:
            out.append({
                "page_url": page_url,
                "bucket": bucket,
                "rule_id": r.get("id"),
                "impact": r.get("impact"),
                "help": r.get("help"),
                "helpUrl": r.get("helpUrl"),
                "selector": (n.get("target") or [""])[0],
                "html": n.get("html"),
                "SC": scs[0] if scs else "",       # primary SC for display
                "sc_list": scs,                    # all mapped SCs
                "failureSummary": n.get("failureSummary"),
                "why_any": "; ".join([(it.get("message") or it.get("id","")) for it in (n.get("any") or [])]),
                "why_all": "; ".join([(it.get("message") or it.get("id","")) for it in (n.get("all") or [])]),
                "why_none": "; ".join([(it.get("message") or it.get("id","")) for it in (n.get("none") or [])]),
            })
    return out

def _topic_to_sc(topic: str) -> str:
    if not topic: return ""
    m = re.search(r"(\d)\.(\d)\.(\d)", topic)
    return ".".join(m.groups()) if m else ""

def _ensure_cols(df: pd.DataFrame, cols):
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df

def _safe_select(df: pd.DataFrame, wanted):
    have = [c for c in wanted if c in df.columns]
    return df[have] if have else pd.DataFrame(columns=wanted)

def _write_hyperlinks(xw, sheet_name, df, col_name, xlsx_path):
    """Turn a file path column into clickable links labeled 'Open'."""
    if sheet_name not in xw.sheets or df is None or df.empty: return
    if col_name not in df.columns: return
    ws = xw.sheets[sheet_name]
    col_idx = df.columns.get_loc(col_name)
    for i, p in enumerate(df[col_name].tolist(), start=1):  # +1 for header
        if not p: 
            continue
        try:
            rel = os.path.relpath(str(p), start=str(xlsx_path.parent))
            ws.write_url(i, col_idx, f"external:{rel}", string="Open")
        except Exception:
            # leave plain text if hyperlink fails
            pass

# ----------------- main builder -----------------

def build_excel(out_dir: pathlib.Path, xlsx_path: pathlib.Path):
    axe = json.loads((out_dir / "axe_results.json").read_text(encoding="utf-8"))

    meta = {}
    mp = out_dir / "metadata.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    page_url = meta.get("page_url", "")

    cands = json.loads((out_dir / "candidates.json").read_text(encoding="utf-8"))
    ai = json.loads((out_dir / "ai_verdicts.json").read_text(encoding="utf-8"))

    # ---------- base sheets ----------
    df_issues = pd.DataFrame(
        _flatten_bucket(axe, "violations", page_url) +
        _flatten_bucket(axe, "incomplete", page_url) +
        _flatten_bucket(axe, "passes", page_url)
    )
    # Normalize rule id column name for joins
    if "axe_rule_id" not in df_issues.columns and "rule_id" in df_issues.columns:
        df_issues = df_issues.rename(columns={"rule_id": "axe_rule_id"})

    df_cands = pd.DataFrame(cands)
    df_ai = pd.DataFrame(ai)

    # Ensure optional cols exist
    df_cands = _ensure_cols(df_cands, ["selector","axe_rule_id","page_url","screenshot","axe_help_url"])
    df_ai    = _ensure_cols(df_ai,    ["selector","axe_rule_id","impact","ai_verdict","topic","screenshot","page_url","SC"])

    # ---- Join screenshots into Axe_Issues (match on selector+rule when available) ----
    shots = _safe_select(df_cands, ["selector","axe_rule_id","screenshot"]).dropna(subset=["selector"]).drop_duplicates()
    if not df_issues.empty and not shots.empty:
        if {"selector","axe_rule_id"}.issubset(df_issues.columns) and {"selector","axe_rule_id"}.issubset(shots.columns):
            df_issues = df_issues.merge(shots, on=["selector","axe_rule_id"], how="left")
        else:
            # Fallback: merge on selector only
            df_issues = df_issues.merge(shots[["selector","screenshot"]], on="selector", how="left")
    else:
        if "screenshot" not in df_issues.columns:
            df_issues["screenshot"] = None

    # Normalize AI to SC + page_url
    if not df_ai.empty:
        if "SC" not in df_ai.columns or df_ai["SC"].isna().all():
            df_ai["SC"] = df_ai["topic"].map(_topic_to_sc)
        if "page_url" not in df_ai.columns or df_ai["page_url"].isna().all():
            df_ai = df_ai.merge(
                _safe_select(df_cands, ["selector","axe_rule_id","page_url"]).drop_duplicates(),
                on=["selector","axe_rule_id"], how="left"
            )

    # Summary
    rows_summary = [{
        "Pages": 1,
        "page_url": page_url,
        "axe.rules_count": len(axe.get("violations",[])) + len(axe.get("incomplete",[])) + len(axe.get("passes",[])),
        "candidates": len(df_cands),
        "ai_reviewed": len(df_ai)
    }]
    df_summary = pd.DataFrame(rows_summary)

    # ---------- WCAG_Summary ----------
    df_wcag = df_issues[df_issues["SC"].astype(str) != ""].copy() if not df_issues.empty else pd.DataFrame(columns=["SC"])
    if not df_wcag.empty:
        pivot_counts = df_wcag.pivot_table(
            index="SC",
            columns="bucket",
            values="axe_rule_id",
            aggfunc="count",
            fill_value=0
        ).reset_index()
    else:
        pivot_counts = pd.DataFrame(columns=["SC","incomplete","passes","violations"])

    if not df_ai.empty:
        def _needs(x):
            try: return (x or {}).get("verdict") == "needs-change"
            except: return False
        needs = (df_ai[df_ai["ai_verdict"].map(_needs)]
                 .groupby("SC", dropna=False)["selector"].count()
                 .reset_index().rename(columns={"selector":"AI_needs_change"}))
        df_wcag_summary = pivot_counts.merge(needs, on="SC", how="left").fillna(0)
    else:
        df_wcag_summary = pivot_counts
        if "AI_needs_change" not in df_wcag_summary.columns:
            df_wcag_summary["AI_needs_change"] = 0

    # ---------- Issue_Backlog ----------
    if not df_ai.empty:
        join_left = ["selector","axe_rule_id"]
        right = _safe_select(df_cands, join_left + ["page_url","screenshot","axe_help_url"]).drop_duplicates()
        if {"selector","axe_rule_id"}.issubset(right.columns):
            df_backlog = df_ai.merge(right, on=["selector","axe_rule_id"], how="left")
        else:
            df_backlog = df_ai.copy()

        def V(x,k):
            try: return (x or {}).get(k)
            except: return None
        df_backlog["verdict"]     = df_backlog["ai_verdict"].map(lambda x: V(x,"verdict"))
        df_backlog["reason"]      = df_backlog["ai_verdict"].map(lambda x: V(x,"reason"))
        df_backlog["confidence"]  = df_backlog["ai_verdict"].map(lambda x: V(x,"confidence"))

        df_backlog = _ensure_cols(df_backlog, [
            "page_url","SC","axe_rule_id","impact","selector","screenshot","axe_help_url","verdict","confidence","reason"
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

    # ---------- write workbook ----------
    with pd.ExcelWriter(xlsx_path, engine="xlsxwriter") as xw:
        df_summary.to_excel(xw, sheet_name="Summary", index=False)
        df_issues.to_excel(xw, sheet_name="Axe_Issues", index=False)
        df_cands.to_excel(xw, sheet_name="Candidates", index=False)
        df_ai.to_excel(xw, sheet_name="AI_Verdicts", index=False)
        df_wcag_summary.to_excel(xw, sheet_name="WCAG_Summary", index=False)
        df_backlog.to_excel(xw, sheet_name="Issue_Backlog", index=False)

        # Turn screenshot file paths into hyperlinks labeled "Open"
        _write_hyperlinks(xw, "Axe_Issues",     df_issues, "screenshot", xlsx_path)
        _write_hyperlinks(xw, "Candidates",     df_cands,  "screenshot", xlsx_path)
        _write_hyperlinks(xw, "AI_Verdicts",    df_ai,     "screenshot", xlsx_path)
        _write_hyperlinks(xw, "Issue_Backlog",  df_backlog,"screenshot", xlsx_path)

        # Add filters on key sheets
        for sheet in ["Summary","WCAG_Summary","Issue_Backlog","Axe_Issues"]:
            try:
                ws = xw.sheets[sheet]
                ws.autofilter(0, 0, ws.dim_rowmax, ws.dim_colmax)
            except Exception:
                pass
