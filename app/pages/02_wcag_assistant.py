# app/pages/02_wcag_assistant.py
import os
from pathlib import Path
import streamlit as st
from core.assistant_engine import list_runs, answer_query

st.set_page_config(page_title="WCAG Assistant", page_icon="🧠", layout="wide")

# ======================= secrets → env =======================
def _apply_secrets_to_env():
    """
    Load Streamlit secrets (from .streamlit/secrets.toml) and expose them
    as environment variables so the assistant uses the same config as the main app.
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

    set_if_missing("OPENAI_API_KEY", flat.get("OPENAI_API_KEY") or nested.get("api_key"))
    set_if_missing("OPENAI_MODEL",   flat.get("OPENAI_MODEL")   or nested.get("model"), "gpt-4o-mini")
    set_if_missing("A11Y_USE_LLM", flat.get("A11Y_USE_LLM"), "1")
    set_if_missing("A11Y_SKIP_BEST_PRACTICE", flat.get("A11Y_SKIP_BEST_PRACTICE"), "0")

_apply_secrets_to_env()
# ============================================================

st.title("🧠 WCAG Assistant")
st.caption("Chat about WCAG and your audit findings. Answers are grounded to your selected run and wcag_lib.")

# ---------- Sidebar ----------
with st.sidebar:
    st.subheader("Run & Status")
    runs = list_runs()
    if not runs:
        st.info("No runs under 'out/'. Run an audit first.")
        chosen_path = None
    else:
        labels = [r.name for r in runs]
        prev = st.session_state.get("chosen_label")
        chosen_label = st.selectbox("Pick a run", labels, index=0)
        chosen_path = runs[labels.index(chosen_label)]
        if prev and prev != chosen_label:
            st.session_state.pop("wcag_chat", None)
            st.session_state.pop("last_elem_key", None)
            st.session_state.pop("last_sc", None)
        st.session_state["chosen_label"] = chosen_label

    st.markdown("---")
    st.subheader("API Key")

    # Prefer secrets/env; UI field is optional override for local testing
    key_present = bool(os.environ.get("OPENAI_API_KEY"))
    if key_present:
        st.info("Using API key from secrets / environment.")
    ui_key = st.text_input(
        "OpenAI API Key (optional override)",
        value="",
        type="password",
        help="Leave empty to use the key from secrets/environment."
    )
    if ui_key.strip():
        os.environ["OPENAI_API_KEY"] = ui_key.strip()
        st.session_state["openai_api_key"] = ui_key.strip()
        st.success("Using API key from UI override for this session.")
    elif not key_present:
        st.warning("No API key found. Enter one above to enable AI responses.")

    st.markdown("---")
    st.markdown("**Try:**")
    st.code("""what is 1.1.1? to which elements it is applied?
what is 1.4.3? what failed?
inspect .nav-active
show screenshot
show screenshot for heading-order_h1
show me h1 tag
continue
hi""")

# ---------- Chat state ----------
if "wcag_chat" not in st.session_state:
    st.session_state.wcag_chat = []
if "last_elem_key" not in st.session_state:
    st.session_state.last_elem_key = None
if "last_sc" not in st.session_state:
    st.session_state.last_sc = None

# ---------- Render history ----------
for msg in st.session_state.wcag_chat:
    with st.chat_message("user" if msg["role"] == "user" else "assistant",
                         avatar="🧑" if msg["role"] == "user" else "🤖"):
        st.write(msg["content"])
        for img in msg.get("images", []) or []:
            st.image(img, caption=Path(img).name, use_column_width=True)
            st.markdown(f"[Open image file]({Path(img)})")

# ---------- Input ----------
user_q = st.chat_input("Ask about a WCAG rule or a specific element…")
if user_q:
    st.session_state.wcag_chat.append({"role": "user", "content": user_q})

    with st.chat_message("assistant", avatar="🤖"):
        with st.spinner("Thinking…"):
            # Always pass the effective key from env (UI override sets env too)
            api_key_effective = os.environ.get("OPENAI_API_KEY") or st.session_state.get("openai_api_key")
            res = answer_query(
                user_q,
                chosen_path,
                history=st.session_state.wcag_chat,
                last_elem_key=st.session_state.last_elem_key,
                last_sc=st.session_state.last_sc,
                fallback_to_latest=True,
                max_images=6,
                api_key=api_key_effective
            )
            text = res.get("text", "")
            images = res.get("images", [])
            st.write(text)
            for img in images:
                st.image(img, caption=Path(img).name, use_column_width=True)
                st.markdown(f"[Open image file]({Path(img)})")

            st.session_state.last_elem_key = res.get("element_key", st.session_state.last_elem_key)
            st.session_state.last_sc = res.get("sc", st.session_state.last_sc)

    st.session_state.wcag_chat.append({"role": "assistant", "content": text, "images": images})
