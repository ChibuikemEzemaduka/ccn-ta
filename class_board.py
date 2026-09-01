"""
class_board.py  —  public anonymous class knowledge board

Visible to anyone (no login required). Shows instructor-published Q&A pairs
that the instructor chose to share after resolving escalation tickets.
All student identity is stripped -- only the title, question, answer, topic,
and date are shown, so students benefit from peers' questions without privacy
risk.

Each entry has a short, professional TITLE generated once by the LLM at publish
time; the full question and answer live behind a dropdown. Older entries that
predate the title feature are given a title once, on first load, then stored --
so the LLM never re-titles the same entry twice.
"""

import streamlit as st
import datetime as _dt
import auth
import ask
import llm_client
import theme

theme.inject_theme()
theme.render_header("CLASS BOARD")

st.markdown("Questions and answers published by the teaching staff for the whole class.")

board = auth.load_public_board()

# ── One-time backfill: give any pre-existing entry without a stored title a
# title now, then persist it. This runs the LLM only for entries missing a
# title, and only once -- after the first load every entry has a title stored,
# so subsequent loads do no LLM work at all.
if board:
    missing = [b for b in board if not b.get("title")]
    if missing:
        with st.spinner("Preparing the board…"):
            for entry in missing:
                entry["title"] = ask.generate_board_title(
                    llm_client, entry.get("question", ""), entry.get("answer", ""))
        auth.save_public_board(board)

if not board:
    st.info("Nothing published yet. Check back after the teaching staff has answered some questions.")
else:
    # Filter by topic if there are multiple
    topics = sorted(set(item.get("topic", "") for item in board if item.get("topic")))
    if topics:
        selected = st.selectbox("Filter by topic", ["All"] + topics)
        if selected != "All":
            board = [b for b in board if b.get("topic") == selected]

    for item in board:
        ts = ""
        try:
            ts = _dt.datetime.fromisoformat(item["time"]).strftime("%b %d, %Y")
        except Exception:
            pass
        title = item.get("title") or (item.get("question", "")[:80])
        topic_badge = f"  ·  {item['topic']}" if item.get("topic") else ""

        # The professional TITLE is the header; the full Q&A is behind the dropdown.
        with st.container(border=True):
            head = st.columns([0.8, 0.2])
            head[0].markdown(f"**{title}**")
            head[1].caption(f"{ts}{topic_badge}")
            with st.expander("View question and answer"):
                st.markdown(f"**Question**")
                st.markdown(item.get("question", ""))
                if item.get("subquestion"):
                    st.markdown(f"**Follow-up**")
                    st.markdown(item["subquestion"])
                st.markdown("**Teaching staff answer**")
                st.success(item.get("answer", ""))

theme.render_attribution()
