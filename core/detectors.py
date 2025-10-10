#!/usr/bin/env python3
# core/detectors.py
"""
All mechanical/hybrid detectors + shared helpers.
These return:
- candidate dicts (for hybrid/mechanical fails), or
- (fail_list, pass_list) tuples for contrast rules.
"""

from __future__ import annotations
import json, os, re, pathlib
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image

# -------------------- small utils --------------------

def ensure_dir(p: pathlib.Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def write_json(path: pathlib.Path, obj: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")

def sanitize_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s or "")
    return s[:120]

def crop_element_screenshot(page, selector: str, out_path: pathlib.Path, enabled: bool=True) -> Optional[str]:
    """
    Try element.screenshot(); if that fails, crop from full-page screenshot.
    Skips when enabled=False. Returns path or None.
    """
    if not enabled:
        return None
    try:
        el = page.query_selector(selector)
        if not el:
            return None
        try:
            el.screenshot(path=str(out_path))
            return str(out_path)
        except Exception:
            pass

        box = el.bounding_box()
        if not box:
            return None
        clip = {
            "x": max(0, box["x"] - 2),
            "y": max(0, box["y"] - 2),
            "width": box["width"] + 4,
            "height": box["height"] + 4,
        }
        tmp = out_path.parent / (out_path.stem + "_full.png")
        page.screenshot(path=str(tmp), full_page=True)
        im = Image.open(tmp)
        x = int(clip["x"]); y = int(clip["y"])
        w = int(clip["width"]); h = int(clip["height"])
        w = max(0, min(w, im.width - x))
        h = max(0, min(h, im.height - y))
        if w <= 0 or h <= 0:
            return None
        crop = im.crop((x, y, x+w, y+h))
        crop.save(out_path)
        try: tmp.unlink()
        except Exception: pass
        return str(out_path)
    except Exception:
        return None

# -------------------- DOM helpers for candidates --------------------

def get_accessibility_snapshot(page, selector: str) -> Dict[str, Any]:
    try:
        el = page.query_selector(selector)
        if not el:
            return {}
        snap = page.accessibility.snapshot(root=el)
        def trim(node):
            if not isinstance(node, dict):
                return node
            keys = ["role", "name", "value", "description"]
            out = {k: node.get(k) for k in keys if k in node}
            if "children" in node:
                out["children"] = [trim(c) for c in node["children"][:4]]
            return out
        return trim(snap) or {}
    except Exception:
        return {}

def get_nearby_text(page, selector: str) -> str:
    js = """(sel) => {
      const el = document.querySelector(sel);
      if (!el) return "";
      const parent = el.closest('figure, a, button, label, td, th, p, div, section, article, li') || el.parentElement || document.body;
      const text = parent.innerText || "";
      return text.trim().slice(0, 600);
    }"""
    try:
        return page.evaluate(js, selector) or ""
    except Exception:
        return ""

def get_role_name_guess(page, selector: str) -> str:
    js = """(sel) => {
      const el = document.querySelector(sel);
      if (!el) return "";
      const role = el.getAttribute('role') || el.tagName.toLowerCase();
      function textOf(n){ return (n && (n.innerText || n.textContent) || "").trim(); }
      function byIds(ids){ return ids.map(id => document.getElementById(id)).filter(Boolean).map(textOf).join(" ").trim(); }
      const ariaLabel = el.getAttribute('aria-label') || "";
      const byLabel = (el.getAttribute('aria-labelledby') || "").split(/\\s+/).filter(Boolean);
      const labelled = byIds(byLabel);
      let fromLabelEl = "";
      if (el.id) {
        const lab = document.querySelector(`[for="${CSS.escape(el.id)}"]`);
        if (lab) fromLabelEl = textOf(lab);
      }
      const inline = textOf(el);
      const name = (ariaLabel || labelled || fromLabelEl || inline || "").trim();
      return (role + " — " + name).slice(0, 250);
    }"""
    try:
        return page.evaluate(js, selector) or ""
    except Exception:
        return ""

def _get_attrs(page, selector: str) -> Dict[str, Any]:
    try:
        if not page.query_selector(selector): return {}
        return page.eval_on_selector(selector, "(el)=>{const o={};for (const a of el.getAttributeNames()) o[a]=el.getAttribute(a); return o;}")
    except Exception:
        return {}

# -------------------- candidate factory --------------------

def _mk_candidate(
    page,
    url: str,
    topic_sc: str,
    det_id: str,
    selector: Optional[str],
    note: str,
    verdict: str="fail",
    evidence: Optional[dict]=None
) -> Dict[str, Any]:
    html_snippet, attrs, acc, nearby, role_name = "", {}, {}, "", ""
    try:
        if selector:
            el = page.query_selector(selector)
            html_snippet = el.evaluate("el=>el.outerHTML.slice(0,2000)") if el else ""
            attrs = _get_attrs(page, selector)
            acc = get_accessibility_snapshot(page, selector)
            nearby = get_nearby_text(page, selector)
            role_name = get_role_name_guess(page, selector)
    except Exception:
        pass
    return {
        "page_url": url,
        "bucket": "semantic_review",
        "topic": f"SC-{topic_sc}" if topic_sc else "BEST_PRACTICE",
        "sc_list": [topic_sc] if topic_sc else [],
        "axe_rule_id": f"{det_id}",
        "axe_help": note,
        "axe_help_url": "",
        "impact": "moderate",
        "selector": selector or "",
        "html_snippet": html_snippet,
        "attributes": attrs,
        "role_name_guess": role_name,
        "nearby_text": nearby,
        "acc_snapshot": acc,
        "screenshot": None,
        "failureSummary": note if verdict=="fail" else None,
        "why_any": [],
        "why_all": [note],
        "why_none": [],
        "verdict": verdict,
        "evidence": evidence or {}
    }

# -------------------- 2.4.6: collectors + local heuristic + prompt --------------------

GENERIC_HEADINGS = {"learn more","more","details","products","features","resources","solutions","explore","get started"}
GENERIC_LABELS  = {"learn more","more","details","click here","submit","go","ok"}

def _norm_text(t: str) -> str:
    t = (t or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s]", "", t)
    return t

def collect_headings_and_labels(frame) -> List[Dict[str,Any]]:
    return frame.evaluate("""() => {
      const out = [];
      const sel = el => {
        if (!el) return "";
        const id = el.getAttribute('id'); if (id) return `#${CSS.escape(id)}`;
        let s = el.tagName.toLowerCase();
        if (el.classList.length) s += '.' + Array.from(el.classList).slice(0,3).map(c=>CSS.escape(c)).join('.');
        return s;
      };
      const textOf = n => (n && (n.innerText || n.textContent) || "").trim();
      const regionOf = (el) => {
        let r = el.closest('main,nav,aside,header,footer');
        return r ? r.tagName.toLowerCase() : 'body';
      };
      const heads = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]')];
      for (const h of heads) {
        const lvl = h.tagName.match(/^H([1-6])$/) ? +RegExp.$1 : +(h.getAttribute('aria-level') || 0);
        if (!lvl) continue;
        out.push({ type: 'heading', selector: sel(h), level: lvl, visibleText: textOf(h), region: regionOf(h), nearbyText: textOf(h.closest('section,article,div')) });
      }
      const ctrls = [...document.querySelectorAll('button,a[href],input,select,textarea,[role="button"],[role="link"]')];
      for (const el of ctrls) {
        const rect = el.getBoundingClientRect();
        if (!(rect.width>16 && rect.height>12)) continue;
        const role = el.getAttribute('role') || el.tagName.toLowerCase();
        let name = el.getAttribute('aria-label') || "";
        if (!name) {
          const labelledby = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
          if (labelledby.length) {
            name = labelledby.map(id => (document.getElementById(id)?.innerText || "")).join(" ").trim();
          }
        }
        if (!name) name = textOf(el);
        let vis = "";
        if (el.id) {
          const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (lab) vis = textOf(lab);
        }
        if (!vis) vis = textOf(el);
        out.push({ type: 'label', selector: sel(el), controlType: role, visibleLabel: vis, accessibleName: name, region: regionOf(el), nearbyText: textOf(el.closest('form,section,article,div')) });
      }
      return out;
    }""")

def evaluate_2_4_6_locally(items: List[Dict[str,Any]]) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    fails, passes = [], []
    for it in items:
        if it["type"] == "heading":
            txt = _norm_text(it.get("visibleText",""))
            bad = (not txt) or (len(txt) <= 2) or (txt in GENERIC_HEADINGS)
            verdict = "fail" if bad else "pass"
            (fails if verdict=="fail" else passes).append({
                **it, "sc": "2.4.6", "verdict": verdict,
                "reasons": ["Heading text generic or empty"] if verdict=="fail" else []
            })
        else:
            vis = _norm_text(it.get("visibleLabel",""))
            name = _norm_text(it.get("accessibleName",""))
            generic = (vis in GENERIC_LABELS) or (not vis and name in GENERIC_LABELS)
            bad = generic or (vis and len(vis) <= 2)
            verdict = "fail" if bad else "pass"
            (fails if verdict=="fail" else passes).append({
                **it, "sc": "2.4.6", "verdict": verdict,
                "reasons": ["Label generic/too short"] if verdict=="fail" else []
            })
    return fails, passes

def write_2_4_6_ai_prompt(out_dir: pathlib.Path, url: str, fast_mode: bool) -> None:
    scope = "MAIN document only (fast mode; no iframes)" if fast_mode else "main document PLUS same-origin iframes"
    prompt = f"""You are an accessibility auditor focused on WCAG 2.4.6 (Headings and Labels).
Determine whether each heading/label clearly communicates PURPOSE or ACTION, using only given context.

Scope: Items extracted from {scope}.
Use 'nearbyText' and 'region' as primary evidence. Do not invent context.

Verdicts:
- PASS: specific & purpose-revealing for its context
- FAIL: generic/ambiguous (“learn more”, “click here”, “more”, “submit”, “ok”, etc.)
- REVIEW: borderline — context may suffice but evidence is thin
- If length ≤2 chars, usually FAIL unless icon+ARIA conveys purpose → REVIEW

Return STRICT JSON (array or {{results:[...]}}) of:
  {{
    "selector": "...",
    "type": "heading"|"label",
    "sc": "2.4.6",
    "verdict": "pass"|"fail"|"review",
    "reasons": ["..."],
    "suggestion": "..."
  }}

Judge based ONLY on: type, selector, level, visibleText/visibleLabel, accessibleName, region, nearbyText, source.
"""
    write_json(out_dir / "ai" / "2_4_6" / "prompt.txt", {"prompt": prompt})

# -------------------- contrast helpers + detectors --------------------

CONTRAST_SAMPLE_GRID = 3  # 3x3 band
CONTRAST_MIN_NORMAL = 4.5       # SC 1.4.3
CONTRAST_MIN_LARGE  = 3.0       # SC 1.4.3 (large/bold)
CONTRAST_NON_TEXT_UI = 3.0      # SC 1.4.11

def _relative_lum(rgb):
    def lin(c):
        c = c/255.0
        return (c/12.92) if (c<=0.03928) else (((c+0.055)/1.055)**2.4)
    r,g,b = rgb
    return 0.2126*lin(r) + 0.7152*lin(g) + 0.0722*lin(b)

def _contrast_ratio(fg_rgb, bg_rgb):
    L1 = _relative_lum(fg_rgb)
    L2 = _relative_lum(bg_rgb)
    Lmax, Lmin = (max(L1,L2), min(L1,L2))
    return (Lmax + 0.05) / (Lmin + 0.05)

def _parse_rgba_any(s: str):
    nums = re.findall(r"[\d.]+", s or "")
    if len(nums) >= 3:
        r,g,b = [int(float(nums[i])) for i in range(3)]
        a = float(nums[3]) if len(nums)>=4 else 1.0
        return (r,g,b,a)
    return (0,0,0,1.0)

def _sample_bg_from_image(im: Image.Image, rect) -> Tuple[int,int,int]:
    cx = int(rect["x"] + rect["w"]/2)
    cy = int(rect["y"] + rect["h"]/2)
    samples = []
    for dx in range(-CONTRAST_SAMPLE_GRID//2, CONTRAST_SAMPLE_GRID//2 + 1):
        for dy in range(-CONTRAST_SAMPLE_GRID//2, CONTRAST_SAMPLE_GRID//2 + 1):
            x = min(max(0, cx + dx*3), im.width-1)
            y = min(max(0, cy + dy*3), im.height-1)
            samples.append(im.getpixel((x, y))[:3])
    return tuple(sum(c[i] for c in samples)//len(samples) for i in (0,1,2))

def detect_contrast_text_general(page, url: str, out_dir: pathlib.Path) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    """
    SC 1.4.3 — text contrast on solid backgrounds (approx).
    Returns (fails, passes) as candidate dicts / pass records.
    """
    js = """() => {
      const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,a,button,label,li,dt,dd,th,td'));
      const res = [];
      for (const el of all) {
        const cs = getComputedStyle(el);
        const txt = (el.innerText || '').trim();
        if (!txt) continue;
        const rect = el.getBoundingClientRect();
        if (!(rect.width > 40 && rect.height > 14)) continue;
        res.push({ sel: (window.__a11ySel? window.__a11ySel(el) : ''), color: cs.color, fontSize: cs.fontSize, bg: cs.backgroundColor, rect:{x:rect.x,y:rect.y,w:rect.width,h:rect.height} });
      }
      return res.slice(0, 60);
    }"""
    fails, passes = [], []
    try:
        elems = page.evaluate(js)
        if not elems: return fails, passes
        full_path = out_dir / "screenshots" / "contrast_text_full.png"
        ensure_dir(full_path.parent)
        page.screenshot(path=str(full_path), full_page=True)
        im = Image.open(full_path)

        for e in elems:
            sel = e["sel"]; rect = e["rect"]
            fr, fg, fb, fa = _parse_rgba_any(e["color"])
            if fa == 0:  # fully transparent text (rare) — skip
                continue
            fg_rgb = (fr, fg, fb)
            bg_css = e.get("bg","")
            br, bg_g, bb, ba = _parse_rgba_any(bg_css)
            if ba == 0 or bg_css in ("transparent","rgba(0, 0, 0, 0)","rgba(0,0,0,0)"):
                bg_rgb = _sample_bg_from_image(im, rect)
            else:
                bg_rgb = (br, bg_g, bb)

            ratio = _contrast_ratio(fg_rgb, bg_rgb)
            fs_px = float(re.findall(r"[\d.]+", e.get("fontSize","16px"))[0])
            fw = page.eval_on_selector(sel, "(el)=>getComputedStyle(el).fontWeight") if sel else "400"
            try:
                is_bold = int(fw) >= 700
            except:
                is_bold = str(fw).lower() in ("bold","bolder")
            is_large = (fs_px >= 24) or (is_bold and fs_px >= 18.66)

            pass_thr = (CONTRAST_MIN_LARGE if is_large else CONTRAST_MIN_NORMAL)
            evidence = {"contrast_ratio": round(ratio,2), "is_large_text": is_large}
            note = f"Text contrast {ratio:.2f} {'<' if ratio<pass_thr else '>='} threshold {pass_thr:.1f}."
            if ratio < pass_thr:
                cand = _mk_candidate(page, url, "1.4.3", "runner:contrast-text", sel, note, verdict="fail", evidence=evidence)
                shot = out_dir / "screenshots" / (sanitize_filename(f"contrast_text__{(sel or '')[:60]}") + ".png")
                cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=True)
                fails.append(cand)
            else:
                passes.append({"selector": sel, "sc":"1.4.3", "note": note, "evidence": evidence})
    except Exception:
        pass
    return fails, passes

def detect_contrast_on_image_text(page, url: str, out_dir: pathlib.Path) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    """
    SC 1.4.3 — text over images/gradients (sampled background).
    """
    js = """() => {
      const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,a,button'));
      const res = [];
      for (const el of all) {
        const cs = getComputedStyle(el);
        const txt = (el.innerText || '').trim();
        if (!txt) continue;
        const rect = el.getBoundingClientRect();
        if (!(rect.width > 40 && rect.height > 16)) continue;
        const bgImg = cs.backgroundImage && cs.backgroundImage !== 'none';
        const hasAncestorBg = !!el.closest('[style*="background"], .bg, .hero, .banner');
        if (!bgImg && !hasAncestorBg) continue;
        res.push({ sel: (window.__a11ySel? window.__a11ySel(el) : ''), color: cs.color, fontSize: cs.fontSize, rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height} });
      }
      return res.slice(0, 30);
    }"""
    fails, passes = [], []
    try:
        elems = page.evaluate(js)
        if not elems: return fails, passes
        full_path = out_dir / "screenshots" / "contrast_over_image_full.png"
        ensure_dir(full_path.parent)
        page.screenshot(path=str(full_path), full_page=True)
        im = Image.open(full_path)

        for e in elems:
            sel = e["sel"]; rect = e["rect"]
            bg_rgb = _sample_bg_from_image(im, rect)
            fr, fg, fb, fa = _parse_rgba_any(e["color"])
            if fa == 0:  # fully transparent text — skip
                continue
            fg_rgb = (fr, fg, fb)
            ratio = _contrast_ratio(fg_rgb, bg_rgb)
            fs_px = float(re.findall(r"[\d.]+", e.get("fontSize","16px"))[0])
            fw = page.eval_on_selector(sel, "(el)=>getComputedStyle(el).fontWeight") if sel else "400"
            try:
                is_bold = int(fw) >= 700
            except:
                is_bold = str(fw).lower() in ("bold","bolder")
            is_large = (fs_px >= 24) or (is_bold and fs_px >= 18.66)
            pass_thr = (CONTRAST_MIN_LARGE if is_large else CONTRAST_MIN_NORMAL)
            evidence = {"contrast_ratio": round(ratio,2), "is_large_text": is_large}
            note = f"Text over image contrast {ratio:.2f} {'<' if ratio<pass_thr else '>='} threshold {pass_thr:.1f}."
            if ratio < pass_thr:
                cand = _mk_candidate(page, url, "1.4.3", "runner:contrast-over-image", sel, note, verdict="fail", evidence=evidence)
                shot = out_dir / "screenshots" / (sanitize_filename(f"contrast_over_img__{(sel or '')[:60]}") + ".png")
                cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=True)
                fails.append(cand)
            else:
                passes.append({"selector": sel, "sc":"1.4.3", "note": note, "evidence": evidence})
    except Exception:
        pass
    return fails, passes

def detect_contrast_nontext_ui(page, url: str, out_dir: pathlib.Path) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    """
    SC 1.4.11 — non-text UI components against background.
    """
    js = """() => {
      const res = [];
      const isUI = (el) => el.matches('button, input, select, textarea, [role="button"], [role="switch"], [role="checkbox"], [role="radio"], [role="tab"], [role="link"]');
      const nodes = Array.from(document.querySelectorAll('button, input, select, textarea, [role]'));
      for (const el of nodes) {
        if (!isUI(el)) continue;
        const rect = el.getBoundingClientRect();
        if (!(rect.width>20 && rect.height>16)) continue;
        const cs = getComputedStyle(el);
        const txt = (el.innerText || '').trim();
        res.push({ sel: (window.__a11ySel? window.__a11ySel(el) : ''), rect:{x:rect.x,y:rect.y,w:rect.width,h:rect.height}, bg: cs.backgroundColor, borderColor: cs.borderTopColor, hasText: !!txt });
      }
      return res.slice(0, 40);
    }"""
    fails, passes = [], []
    try:
        elems = page.evaluate(js)
        if not elems: return fails, passes
        full_path = out_dir / "screenshots" / "contrast_nontext_full.png"
        ensure_dir(full_path.parent)
        page.screenshot(path=str(full_path), full_page=True)
        im = Image.open(full_path)

        for e in elems:
            sel = e["sel"]; rect = e["rect"]
            inside_rgb = _sample_bg_from_image(im, rect)
            ring_rect = {
                "x": max(0, rect["x"] - 4),
                "y": max(0, rect["y"] - 4),
                "w": rect["w"] + 8,
                "h": rect["h"] + 8,
            }
            outside_rgb = _sample_bg_from_image(im, ring_rect)
            ratio = _contrast_ratio(inside_rgb, outside_rgb)
            evidence = {"contrast_ratio": round(ratio,2), "inside_rgb": inside_rgb, "outside_rgb": outside_rgb}
            note = f"Non-text (UI) contrast {ratio:.2f} {'<' if ratio<CONTRAST_NON_TEXT_UI else '>='} 3.0 threshold."
            if ratio < CONTRAST_NON_TEXT_UI:
                cand = _mk_candidate(page, url, "1.4.11", "runner:nontext-ui-contrast", sel, note, verdict="fail", evidence=evidence)
                shot = out_dir / "screenshots" / (sanitize_filename(f"contrast_nontext__{(sel or '')[:60]}") + ".png")
                cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=True)
                fails.append(cand)
            else:
                passes.append({"selector": sel, "sc":"1.4.11", "note": note, "evidence": evidence})
    except Exception:
        pass
    return fails, passes

# -------------------- other detectors --------------------

def detect_info_relationships(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 1.3.1 — list semantics for grouped nav links in header/footer.
    """
    js = """() => {
      const regions = Array.from(document.querySelectorAll('header, footer, nav, [role="navigation"]'));
      const res = [];
      const isListy = (el) => el.closest('ul,ol,[role="list"]');
      for (const reg of regions) {
        const links = Array.from(reg.querySelectorAll('a[href]')).filter(a => a.offsetWidth>0 && a.offsetHeight>0);
        if (links.length < 3) continue;
        const byParent = new Map();
        for (const a of links) {
          const p = a.parentElement;
          if (!byParent.has(p)) byParent.set(p, []);
          byParent.get(p).push(a);
        }
        for (const [p, arr] of byParent) {
          if (arr.length >= 3 && !isListy(p)) {
            res.push({containerSel: (window.__a11ySel? window.__a11ySel(p): ''), firstLinkSel: (window.__a11ySel? window.__a11ySel(arr[0]): '')});
          }
        }
      }
      return res.slice(0, 6);
    }"""
    out = []
    try:
        hits = page.evaluate(js)
        for h in hits:
            sel = h.get("firstLinkSel")
            cand = _mk_candidate(page, url, "1.3.1", "runner:info-relationships-list-missing", sel, "Navigation links appear grouped but not conveyed via list semantics.", verdict="fail")
            shot_name = sanitize_filename(f"rel_nav__{(sel or '')[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            ensure_dir(shot_path.parent)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
    except Exception:
        pass
    return out

def detect_meaningful_sequence(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 1.3.2 — crude DOM↔visual order anomalies.
    """
    js = """() => {
      const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li'));
      const res = [];
      let idx = 0;
      for (const el of nodes) {
        const rect = el.getBoundingClientRect();
        const txt = (el.innerText || '').trim();
        if (!txt) continue;
        if (!(rect.width>40 && rect.height>14)) continue;
        res.push({idx: idx++, y: rect.y, sel: (window.__a11ySel? window.__a11ySel(el): ''), text: txt.slice(0,100)});
      }
      return res;
    }"""
    out = []
    try:
        rows = page.evaluate(js)
        if len(rows) < 6:
            return out
        anomalies = []
        for i in range(1, len(rows)-1):
            prev, cur, nxt = rows[i-1], rows[i], rows[i+1]
            if (cur["y"] - prev["y"] > 600) and (nxt["y"] - prev["y"] < 200):
                anomalies.append(cur)
        for a in anomalies[:5]:
            sel = a["sel"]
            cand = _mk_candidate(page, url, "1.3.2", "runner:meaningful-sequence-inversion", sel, "Possible DOM↔visual reading order mismatch.", verdict="fail")
            shot_name = sanitize_filename(f"seq__{(sel or '')[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
    except Exception:
        pass
    return out

def detect_role_conflicts(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 4.1.2 — role conflicts / nested interactive.
    """
    js = """() => {
      const out = [];
      const all = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"]'));
      for (const el of all) {
        const tn = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || '';
        const hasNestedInteractive = !!el.querySelector('a, button, [role="button"], [role="link"]');
        if ((tn === 'a' && role === 'button') || hasNestedInteractive) {
          out.push({ sel: (window.__a11ySel? window.__a11ySel(el): '') });
        }
      }
      return out.slice(0, 20);
    }"""
    out = []
    try:
        hits = page.evaluate(js)
        for h in hits:
            sel = h["sel"]
            cand = _mk_candidate(page, url, "4.1.2", "runner:role-conflict-or-nested", sel, "Potential role conflict or nested interactive elements.", verdict="fail")
            shot_name = sanitize_filename(f"roleconf__{(sel or '')[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
    except Exception:
        pass
    return out

def detect_label_in_name(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 2.5.3 — visible label text should be in accessible name.
    """
    js = """() => {
      const els = Array.from(document.querySelectorAll('button, a[href], input, [role="button"], [role="link"]'));
      const res = [];
      for (const el of els) {
        const rect = el.getBoundingClientRect();
        if (!(rect.width>24 && rect.height>16)) continue;
        const acc = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '';
        let vis = '';
        if (el.id) {
          const lab = document.querySelector(`[for="${el.id}"]`);
          if (lab) vis = (lab.innerText || '').trim();
        }
        if (!vis) vis = (el.innerText || '').trim();
        if (!vis) continue;
        res.push({ sel: (window.__a11ySel? window.__a11ySel(el): ''), visible: vis.slice(0,120) });
      }
      return res.slice(0, 60);
    }"""
    out = []
    try:
        rows = page.evaluate(js)
        for r in rows:
            sel = r["sel"]
            acc = get_accessibility_snapshot(page, sel)
            def _norm(s: str) -> str:
                s = (s or "").lower()
                s = re.sub(r"\s+", " ", s)
                s = re.sub(r"[^\w\s]", "", s)
                return s.strip()
            acc_name = _norm(acc.get("name") or "")
            v = _norm(r["visible"] or "")
            if v and acc_name and v not in acc_name:
                cand = _mk_candidate(page, url, "2.5.3", "runner:label-in-name", sel, "Visible label text not present in the accessible name.", verdict="fail")
                shot_name = sanitize_filename(f"lin__{(sel or '')[:60]}") + ".png"
                shot_path = (out_dir / "screenshots" / shot_name)
                cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
                out.append(cand)
    except Exception:
        pass
    return out

def detect_link_purpose_generic(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 2.4.4 — generic link text (“learn more”, “get started”, etc.).
    """
    GENERIC = {"learn more","get started","read more","see more","view more","more","details","click here"}
    def _clean_link_text(s:str)->str:
        s = (s or "").lower()
        s = re.sub(r"[\u2190-\u21ff\u25a0-\u25ff\u2700-\u27bf➡️•►»›‹«→]+", "", s)
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"[^a-z ]", "", s)
        return s.strip()
    js = """() => {
      const res = [];
      const links = Array.from(document.querySelectorAll('a[href]'));
      for (const a of links) {
        const txt = (a.innerText || '').trim().toLowerCase();
        const rect = a.getBoundingClientRect();
        if (!(rect.width>16 && rect.height>12)) continue;
        res.push({ sel: (window.__a11ySel? window.__a11ySel(a): ''), text: txt });
      }
      return res;
    }"""
    out = []
    try:
        rows = page.evaluate(js)
        for r in rows:
            text = _clean_link_text(r["text"])
            if text in GENERIC:
                sel = r["sel"]
                cand = _mk_candidate(page, url, "2.4.4", "runner:link-purpose-generic", sel, f'Generic link text: "{text}".', verdict="fail")
                shot_name = sanitize_filename(f"lp__{(sel or '')[:60]}") + ".png"
                shot_path = (out_dir / "screenshots" / shot_name)
                cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
                out.append(cand)
    except Exception:
        pass
    return out

def detect_link_indicator_style(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 1.4.1 — inline links must be visually distinguishable.
    """
    js = """() => {
      const out = [];
      const links = Array.from(document.querySelectorAll('a[href]'));
      for (const a of links) {
        const rect = a.getBoundingClientRect();
        if (!(rect.width>16 && rect.height>12)) continue;
        if (a.querySelector('img')) continue;
        const cs = getComputedStyle(a);
        const p = a.parentElement || document.body;
        const cps = getComputedStyle(p);
        const underline = (cs.textDecorationLine || '').includes('underline');
        const colorDiff = cs.color !== cps.color;
        if (!underline && !colorDiff) {
          out.push({ sel: (window.__a11ySel? window.__a11ySel(a): '') });
        }
      }
      return out.slice(0, 40);
    }"""
    out = []
    try:
        rows = page.evaluate(js)
        for r in rows:
            sel = r["sel"]
            cand = _mk_candidate(page, url, "1.4.1", "link-indicator:style", sel, "Inline link not visually distinguished (no underline and no color difference).", verdict="fail")
            shot_name = sanitize_filename(f"linkind__{(sel or '')[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
    except Exception:
        pass
    return out

# -------------------- timeouts --------------------

TIMEOUT_20H_SECONDS = 20 * 60 * 60      # 72000s
TIMEOUT_20H_MS = TIMEOUT_20H_SECONDS * 1000

def detect_timeouts(page, url: str, out_dir: pathlib.Path) -> List[Dict[str, Any]]:
    """
    SC 2.2.6 — timeouts (meta refresh or setTimeout that redirects/logs out).
    """
    js = """() => {
      const metas = [];
      document.querySelectorAll('meta[http-equiv]').forEach(m => {
        const hev = (m.getAttribute('http-equiv') || m.getAttribute('httpEquiv') || '').toLowerCase();
        if (hev === 'refresh') {
          const content = m.getAttribute('content') || '';
          metas.push({ type:'meta', content });
        }
      });
      const inlineScripts = Array.from(document.querySelectorAll('script:not([src])'))
        .slice(0,80)
        .map(s => s.textContent || '')
        .join('\\n');
      return { metas, inlineScripts: inlineScripts.slice(0, 200000) };
    }"""
    out = []
    try:
        res = page.evaluate(js)
        for m in res.get("metas", []):
            c = m.get("content","")
            secs = None
            try: secs = int((c.split(";")[0] or "").strip())
            except Exception: secs = None
            if secs is not None and secs < TIMEOUT_20H_SECONDS:
                cand = _mk_candidate(page, url, "2.2.6", "timeout:meta-refresh", "", f"Meta refresh present (~{secs}s) without user control/warning.", verdict="fail")
                out.append(cand)
        code = res.get("inlineScripts","") or ""
        for m in re.finditer(r"setTimeout\s*\(([^,]+),\s*(\d+)\s*\)", code, flags=re.S|re.I):
            delay_ms = int(m.group(2))
            callback_src = m.group(1)
            if delay_ms < TIMEOUT_20H_MS and re.search(r"location|redirect|logout|session", callback_src, flags=re.I):
                cand = _mk_candidate(page, url, "2.2.6", "timeout:settimeout", "", f"Script sets a time-based action at ~{int(delay_ms/1000)}s (possible timeout) without visible notice.", verdict="fail")
                out.append(cand)
    except Exception:
        pass
    return out

# -------------------- focus-visible / focus-order from keyboard trace --------------------

def _read_keyboard_trace(out_dir: pathlib.Path) -> List[Dict[str, Any]]:
    rows = []
    p = out_dir / "keyboard_trace.jsonl"
    if not p.exists():
        return rows
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows

def detect_focus_visible_weak_from_trace(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 2.4.7 — weak/absent focus indicator based on computed styles captured in trace.
    """
    trace = _read_keyboard_trace(out_dir)
    out = []
    seen = set()
    for t in trace:
        sel = t.get("selector") or ""
        if not sel or sel in seen:
            continue
        outline = (t.get("outline") or "").lower()
        boxshadow = (t.get("boxShadow") or "").lower()
        border = (t.get("border") or "").lower()
        weak = (("none" in outline or outline.strip() == "")) and (boxshadow.strip() == "") and (("0px" in border) or ("none" in border))
        if weak:
            cand = _mk_candidate(page, url, "2.4.7", "focus-visible:weak", sel, "Focused element may lack a visible focus indicator.", verdict="fail")
            shot_name = sanitize_filename(f"fvis__{sel[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
            seen.add(sel)
            if len(out) >= 20:
                break
    return out

def detect_focus_order_suspect_from_trace(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 2.4.3 — large negative jumps in Y suggest non-meaningful focus order.
    """
    trace = _read_keyboard_trace(out_dir)
    out = []
    for i in range(1, len(trace)):
        prev = trace[i-1]; cur = trace[i]
        rp = (prev.get("rect") or {})
        rc = (cur.get("rect") or {})
        if not rp or not rc: continue
        dy = rc.get("y", 0) - rp.get("y", 0)
        if dy < -400:
            sel = cur.get("selector") or ""
            cand = _mk_candidate(page, url, "2.4.3", "runner:focus-order-suspect", sel, "Focus order may not follow a meaningful sequence (large jump detected).", verdict="fail")
            shot_name = sanitize_filename(f"forder__{sel[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
            if len(out) >= 10:
                break
    return out
