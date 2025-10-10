# app/streamlit_app.py
# --- make project root importable (must be first) ---
import sys, os, asyncio, base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ----------------------------------------------------

# --- Windows: ensure asyncio can spawn subprocesses for Playwright ---
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
# --------------------------------------------------------------------

import json
from urllib.parse import urlparse
import streamlit as st

from core.axe_runner import run_axe_on_url
from core.rag_reviewer import review as rag_review
from core.report_builders.excel_report import build_excel
from core.report_builders.word_report import build_word

st.set_page_config(page_title="Tsqs — Accessibility tester", layout="wide")
# ======================= secrets → env =======================
def _apply_secrets_to_env():
    """
    Load Streamlit secrets (from .streamlit/secrets.toml in Streamlit Cloud)
    and expose them as environment variables so that core modules and UI
    can use them without manual entry.
    """
    if not hasattr(st, "secrets"):
        return

    flat = st.secrets
    nested = flat.get("openai", {}) if isinstance(flat, dict) else {}

    def set_if_missing(name, value, default=None):
        if os.environ.get(name):
            return
        if value is not None and str(value).strip():
            os.environ[name] = str(value)
        elif default is not None:
            os.environ[name] = str(default)

    # Apply keys and defaults
    set_if_missing("OPENAI_API_KEY", flat.get("OPENAI_API_KEY") or nested.get("api_key"))
    set_if_missing("OPENAI_MODEL",   flat.get("OPENAI_MODEL")   or nested.get("model"), "gpt-4o-mini")
    set_if_missing("A11Y_USE_LLM", flat.get("A11Y_USE_LLM"), "1")
    set_if_missing("A11Y_SKIP_BEST_PRACTICE", flat.get("A11Y_SKIP_BEST_PRACTICE"), "0")

_apply_secrets_to_env()

# ======================= theme & branding =======================
BRAND_ORANGE = "#ff6a00"
ACCENT_ORANGE = "#ff8c3a"

CUSTOM_CSS = f"""
<style>
    .stApp {{ background: #fffaf6; }}
    .brandbar {{ display:flex; align-items:center; gap:12px; margin: 0.2rem 0 0.8rem; }}
    .brandbar h1 {{ margin:0; font-size: 1.4rem; color:{BRAND_ORANGE}; letter-spacing:0.3px; }}
    .brandlogo {{ height:42px; width:auto; display:block; }}
    .stButton button {{
        background:{BRAND_ORANGE} !important; color:white !important; border:none !important; border-radius:8px !important;
    }}
    .stButton button:hover {{ background:{ACCENT_ORANGE} !important; }}
    .stDownloadButton button {{
        background:{BRAND_ORANGE} !important; color:#fff !important; border:none !important; border-radius:8px !important;
    }}
    .st-bb, .st-bc {{ border-color: {BRAND_ORANGE}33 !important; }}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# brand bar (inline image to keep it from dropping)
logo_path = ROOT / "app" / "assets" / "logo.gif"   # put your logo here
logo_html = ""
if logo_path.exists():
    b64 = base64.b64encode(logo_path.read_bytes()).decode()
    logo_html = f'<img class="brandlogo" src="data:image/gif;base64,{b64}" alt="logo">'

st.markdown(f'''
<div class="brandbar">
  {logo_html}
  <h1>Tsqs — Accessibility tester</h1>
</div>
''', unsafe_allow_html=True)

# ======================= helpers =======================
BASE_OUT = (ROOT / "out")
BASE_OUT.mkdir(exist_ok=True)

def slugify(u: str) -> str:
    p = urlparse(u)
    host = (p.netloc or "site").replace(":", "_")
    path = (p.path or "/").strip("/").replace("/", "_") or "home"
    return f"{host}__{path}"[:80]

# compute a dir etag (mtime_ns + size across files) for cache-busting zips
def compute_dir_etag(run_dir: Path) -> str:
    import hashlib, os as _os
    h = hashlib.sha256()
    for root, _, files in _os.walk(run_dir):
        for f in sorted(files):
            p = Path(root) / f
            try:
                st_ = p.stat()
                h.update(str(p.relative_to(run_dir)).encode())
                h.update(str(st_.st_mtime_ns).encode())
                h.update(str(st_.st_size).encode())
            except Exception:
                pass
    return h.hexdigest()

@st.cache_data(show_spinner=False)
def zip_run_dir(run_dir_str: str, etag: str) -> bytes:
    """
    Create a zip of the run directory. Cache key includes a content etag,
    so the zip refreshes whenever any file changes (mtime/size).
    """
    import io, zipfile, os as _os
    run_dir = Path(run_dir_str)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in _os.walk(run_dir):
            for f in files:
                p = Path(root) / f
                arcname = Path(run_dir.name) / p.relative_to(run_dir)
                try:
                    z.write(p, arcname.as_posix())
                except FileNotFoundError:
                    # If a file changed mid-walk, skip gracefully.
                    continue
    buf.seek(0)
    return buf.getvalue()

def show_preview_tables(out_dir: Path):
    try:
        cands = json.loads((out_dir / "candidates.json").read_text(encoding="utf-8"))
        ai = json.loads((out_dir / "ai_verdicts.json").read_text(encoding="utf-8"))
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Candidates")
            st.dataframe(cands, use_container_width=True)
        with col2:
            st.subheader("AI Verdicts")
            st.dataframe(ai, use_container_width=True)
    except Exception as e:
        st.info(f"Preview not available yet: {e}")

# ======================= UI =======================
st.title("")

with st.sidebar:
    st.header("Run Settings")
    fast_mode = st.toggle(
        "Fast Mode (skip iframes & DOM snapshots)",
        value=False,
        help="Fast mode limits scan to MAIN document, disables DOM snapshot saving. Keyboard probe still runs."
    )
    quick_scan = st.toggle(
        "Quick scan (lighter & faster)",
        value=False,
        help="Disables element screenshots, reduces keyboard steps, and skips contrast heuristics."
    )
    very_quick = st.toggle(
        "Very quick (axe-only)",
        value=False,
        help="MAIN doc only; axe violations only. Skips keyboard, screenshots, 2.4.6, contrast, and DOM."
    )
    if very_quick:
        quick_scan = False
        fast_mode = True

    # Allow screenshots in quick scan (fail-only)
    quick_fail_shots = False
    if quick_scan and not very_quick:
        quick_fail_shots = st.checkbox("Attach screenshots in Quick scan (fail-only)", value=False)

    timeout_ms = st.number_input("Nav timeout (ms)", min_value=5000, max_value=120000, value=30000, step=5000)

    if quick_scan:
        st.caption("Quick scan overrides:")
        qs_kb_steps = st.number_input("Max Tab steps (quick)", 20, 400, 80, 10)
        qs_kb_repeats = st.number_input("Repeat loops until stop", 1, 10, 3, 1)
        qs_kb_back = st.number_input("Back-sweep steps", 0, 20, 6, 1)
    else:
        qs_kb_steps = qs_kb_repeats = qs_kb_back = None

    # st.header("AI Settings")
    # saved_key = st.session_state.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    # api_key = st.text_input("OpenAI API key", type="password", value=saved_key, help="Kept in memory for this session only.")
    # if api_key:
    #     st.session_state["OPENAI_API_KEY"] = api_key
    #     os.environ["OPENAI_API_KEY"] = api_key

    # default_model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    # model = st.text_input("OpenAI model", value=st.session_state.get("OPENAI_MODEL", default_model))
    # if model:
    #     st.session_state["OPENAI_MODEL"] = model
    #     os.environ["OPENAI_MODEL"] = model

    key_present = bool(os.environ.get("OPENAI_API_KEY"))
    use_live_ai = st.checkbox(
        "Use live AI (RAG + 2.4.6)",
        value=key_present and True,
        help="If enabled and API key provided, the reviewer will call your model. Otherwise, offline stubs."
    )
    os.environ["A11Y_USE_LLM"] = "1" if (use_live_ai and key_present) else "0"

    skip_bp = st.checkbox(
        "Skip non-WCAG (best-practice) items in AI",
        value=False,
        help="If ON, AI runs only for candidates with WCAG SC tags."
    )
    os.environ["A11Y_SKIP_BEST_PRACTICE"] = "1" if skip_bp else "0"

    if key_present:
        st.success("API key set. Live AI is " + ("ON" if use_live_ai else "OFF"))
    else:
        st.warning("No API key set. AI uses the safe stub.")

    st.header("Import WCAG Techniques")
    xls = st.file_uploader("Upload WCAG_Checklist.xlsx", type=["xlsx", "xls"])
    merge_existing = st.checkbox("Merge with existing JSONs (append/unique)", value=True,
                                 help="If off, existing files will be overwritten with the Excel content.")
    if xls and st.button("Import techniques"):
        from core.wcag_importer import import_wcag_from_excel, WCAG_LIB_DIR
        result = import_wcag_from_excel(xls.getvalue(), out_dir=WCAG_LIB_DIR, merge_existing=merge_existing)
        st.success(f"Imported: {result['created']} created, {result['updated']} updated, {result['skipped']} skipped")
        for p in result["files"][:10]:
            st.write("•", p)
        if len(result["files"]) > 10:
            st.write(f"... and {len(result['files']) - 10} more")

# ---- Main controls ----
if "last_url" not in st.session_state:
    st.session_state.last_url = "https://example.com"
if "last_run" not in st.session_state:
    st.session_state.last_run = None  # dict with url/slug/paths

url = st.text_input("Enter a URL to audit", value=st.session_state.last_url, help="Single page for now.")
colA, colB, colC = st.columns([1, 1, 1])
run_clicked = colA.button("Run Full Audit", type="primary", key="run_audit")
reai_clicked = colB.button("Rebuild AI Verdicts Only", key="reai")
clear_clicked = colC.button("Clear Session", key="clear")

if clear_clicked:
    st.session_state.last_run = None
    st.session_state.last_url = url
    st.cache_data.clear()
    st.success("Session cleared.")

# ======================= actions =======================
def build_reports(out_dir: Path, url: str):
    xlsx = out_dir / "report.xlsx"
    docx = out_dir / "report.docx"
    build_excel(out_dir, xlsx)
    build_word(out_dir, docx, url)
    return xlsx, docx

if run_clicked and url:
    st.session_state.last_url = url
    slug = slugify(url)
    out_dir = BASE_OUT / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    with st.spinner("Running axe-core & heuristics…"):
        run_axe_on_url(
            url,
            out_dir,
            timeout_ms=timeout_ms,
            fast_mode=fast_mode,
            include_frames=not fast_mode and not very_quick,
            capture_dom=not fast_mode and not very_quick,
            screenshot_elements=True,  # internally overridden by quick/very_quick
            quick_scan=quick_scan,
            ultra_quick=very_quick,
            kb_steps_override=qs_kb_steps,
            kb_repeats_override=qs_kb_repeats,
            kb_back_steps_override=qs_kb_back,
            enable_contrast_checks=not quick_scan and not very_quick,
            quick_screenshots=("fail-only" if quick_fail_shots else None),
        )

    with st.spinner("Building RAG prompts & AI verdicts…"):
        # Honors A11Y_USE_LLM; also reads ai/2_4_6/input.json and writes ai/2_4_6/results.json
        rag_review(out_dir)

    with st.spinner("Generating Excel & Word reports…"):
        xlsx, docx = build_reports(out_dir, url)

    st.session_state.last_run = {
        "url": url,
        "slug": slug,
        "out_dir": str(out_dir),
        "xlsx": str(xlsx),
        "docx": str(docx),
    }
    st.success("Audit completed.")

elif reai_clicked and st.session_state.last_run:
    lr = st.session_state.last_run
    out_dir = Path(lr["out_dir"])
    with st.spinner("Rebuilding RAG prompts & AI verdicts…"):
        rag_review(out_dir)
    with st.spinner("Regenerating Excel & Word reports…"):
        xlsx, docx = build_reports(out_dir, lr["url"])
    st.session_state.last_run["xlsx"] = str(xlsx)
    st.session_state.last_run["docx"] = str(docx)
    st.success("AI verdicts and reports updated.")

# ======================= downloads & preview =======================
if st.session_state.last_run:
    lr = st.session_state.last_run
    out_dir = Path(lr["out_dir"])
    slug = lr["slug"]
    xlsx = Path(lr["xlsx"])
    docx = Path(lr["docx"])

    st.divider()
    st.subheader("Downloads")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("Download Excel report", data=xlsx.read_bytes(), file_name=xlsx.name, key="dl_excel")
    with c2:
        st.download_button("Download Word report", data=docx.read_bytes(), file_name=docx.name, key="dl_word")
    with c3:
        etag = compute_dir_etag(out_dir)
        zip_bytes = zip_run_dir(str(out_dir), etag)  # cache key now includes content fingerprint
        st.download_button(
            "Download ALL outputs (ZIP)",
            data=zip_bytes,
            file_name=f"{slug}.zip",
            mime="application/zip",
            key="dl_zip_all"
        )

    st.divider()
    st.subheader("Run Artifacts")
    st.code(json.dumps({
        "axe_results": str(out_dir / "axe_results.json"),
        "candidates": str(out_dir / "candidates.json"),
        "ai_verdicts": str(out_dir / "ai_verdicts.json"),
        "ai_2_4_6_input": str(out_dir / "ai" / "2_4_6" / "input.json"),
        "ai_2_4_6_results": str(out_dir / "ai" / "2_4_6" / "results.json"),
        "keyboard_probe": str(out_dir / "keyboard_probe.json"),
        "screenshots_dir": str(out_dir / "screenshots")
    }, indent=2))

    show_preview_tables(out_dir)
