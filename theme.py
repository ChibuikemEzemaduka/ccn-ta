"""
theme.py  —  professional light-mode theming for the CCN AI Teaching Assistant

The base palette is set in .streamlit/config.toml (native Streamlit light theme).
This module adds a light CSS polish layer, the app header/wordmark, the standing
AI-accuracy disclaimer, and small styling helpers (info popovers, badges).
"""

import streamlit as st


# palette (kept in sync with .streamlit/config.toml)
PRIMARY   = "#C8102E"   # RPI red -- primary accent
INK       = "#1A1D21"   # near-black text
SLATE     = "#5B6470"   # secondary text
LINE      = "#E3E6EA"   # hairline borders
PANEL     = "#F5F6F8"   # secondary background
ACCENT    = "#2563EB"   # informational blue
AMBER     = "#B45309"   # warnings / homework mode


def inject_theme():
    """Inject a light CSS polish layer. Safe to call on every page."""
    st.markdown(
        f"""
        <style>
        html, body, [class*="css"] {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         "Helvetica Neue", Arial, sans-serif;
            color: {INK};
        }}
        .stApp {{ background: #FFFFFF; }}
        .stMarkdown p, .stChatMessage p {{
            font-size: 1.02rem;
            line-height: 1.6;
        }}
        h1, h2, h3, h4 {{ color: {INK}; font-weight: 700; letter-spacing: -0.01em; }}
        .stButton > button {{
            border-radius: 8px;
            font-weight: 600;
            border: 1px solid {LINE};
            transition: all 0.12s ease;
        }}
        .stButton > button:hover {{
            border-color: {PRIMARY};
            color: {PRIMARY};
        }}
        .stButton > button[kind="primary"] {{
            background: {PRIMARY};
            border-color: {PRIMARY};
            color: #FFFFFF;
        }}
        .stButton > button[kind="primary"]:hover {{
            filter: brightness(0.94);
            color: #FFFFFF;
        }}
        .stChatMessage {{
            border-radius: 12px;
            border: 1px solid {LINE};
            background: #FFFFFF;
            box-shadow: 0 1px 2px rgba(16,24,40,0.04);
        }}
        .stTextInput input, .stTextArea textarea, .stChatInput textarea {{
            border-radius: 8px !important;
        }}
        .stTabs [data-baseweb="tab-list"] {{ gap: 4px; }}
        .stTabs [data-baseweb="tab"] {{
            font-weight: 600;
            border-radius: 8px 8px 0 0;
        }}
        .streamlit-expanderHeader {{ font-weight: 600; }}
        hr {{ border-color: {LINE}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def theme_switch():
    """No-op placeholder kept for backward compatibility (light mode is fixed)."""
    pass


def color(name):
    return {"accent": ACCENT, "amber": AMBER, "primary": PRIMARY}.get(name, ACCENT)


def render_header(role_label, role_color=None):
    """App wordmark + role badge in a clean light-mode header."""
    accent = role_color or PRIMARY
    st.markdown(
        f"""
        <div style="display:flex;align-items:center;gap:14px;padding:6px 0 2px 0;">
          <div style="width:38px;height:38px;border-radius:9px;background:{PRIMARY};
                      display:flex;align-items:center;justify-content:center;font-size:20px;">📡</div>
          <div style="line-height:1.15;">
            <div style="font-size:1.35rem;font-weight:800;color:{INK};letter-spacing:-0.01em;">
              CCN AI Teaching Assistant</div>
            <div style="font-size:0.78rem;color:{SLATE};font-weight:500;">
              ECSE 4670 · Computer Communication Networks</div>
          </div>
          <div style="margin-left:auto;">
            <span style="font-size:0.72rem;font-weight:700;background:{accent};color:#fff;
                         padding:4px 12px;border-radius:6px;letter-spacing:0.06em;">{role_label}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_ai_disclaimer()
    st.divider()


def render_ai_disclaimer():
    """Discreet standing notice that the assistant is AI and may be wrong.
    Rendered as a small, muted note fixed to the bottom-right corner of the
    page -- visible but unobtrusive, not a bold banner."""
    st.markdown(
        f"""
        <div style="position:fixed;right:14px;bottom:8px;z-index:1000;
                    max-width:300px;text-align:right;pointer-events:none;
                    font-size:0.68rem;line-height:1.3;color:{SLATE};opacity:0.75;">
          AI-generated — may be incorrect; please cross-check important answers.
        </div>
        """,
        unsafe_allow_html=True,
    )


def info_button(body_markdown, label="info"):
    with st.popover("ℹ️  Help"):
        st.markdown(body_markdown)


def citation_chip_html(text, amber=False):
    bg = AMBER if amber else ACCENT
    return (
        f'<span style="display:inline-block;font-size:0.72rem;font-weight:700;'
        f'letter-spacing:0.03em;border-radius:999px;padding:3px 11px;margin:2px 4px 2px 0;'
        f'background:{bg};color:#fff">{text}</span>'
    )


def scroll_to_bottom():
    """Keep the conversation scrolled to the most recent message after a rerun.

    Streamlit re-renders from the top on every rerun, which resets the scroll
    position. This injects a tiny script that, once the page has painted,
    scrolls the LAST chat message into view -- so the student always lands on
    the newest response instead of being thrown back to the top of the page.

    A time-based key on the script forces the browser to re-run it on every
    rerun (otherwise Streamlit would dedupe an identical component and it would
    only fire once)."""
    import time as _t
    nonce = int(_t.time() * 1000)
    st.markdown(
        f"""
        <div id="scroll-anchor-{nonce}"></div>
        <script>
            (function() {{
                const scroll = () => {{
                    // Prefer the last chat message; fall back to the anchor.
                    const msgs = window.parent.document.querySelectorAll(
                        '[data-testid="stChatMessage"]');
                    if (msgs.length) {{
                        msgs[msgs.length - 1].scrollIntoView(
                            {{behavior: "smooth", block: "start"}});
                    }} else {{
                        const a = window.parent.document.getElementById(
                            "scroll-anchor-{nonce}");
                        if (a) a.scrollIntoView({{behavior: "smooth"}});
                    }}
                }};
                // Give Streamlit a moment to finish painting, then scroll.
                setTimeout(scroll, 120);
                setTimeout(scroll, 350);
            }})();
        </script>
        """,
        unsafe_allow_html=True,
    )


def render_attribution():
    st.caption(
        "CCN AI Teaching Assistant · ECSE 4670. This assistant is AI-powered and "
        "can be wrong -- please verify important information."
    )
