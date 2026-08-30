"""
class_board.py  —  public anonymous class knowledge board

Visible to anyone (no login required). Shows instructor-published Q&A pairs
that the instructor chose to share after resolving escalation tickets.
All student identity is stripped -- only the question, answer, topic, and date
are shown, so students benefit from peers' questions without privacy risk.
"""

import streamlit as st
import datetime as _dt
import auth
import theme

theme.inject_theme()
theme.render_header("CLASS BOARD")

st.markdown("Questions and answers published by the teaching staff for the whole class. "
            "All questions appear anonymously regardless of who originally asked them.")

board = auth.load_public_board()

if not board:
    st.info("Nothing published yet. Check back after the teaching staff has answered some questions.")
else:
    # Filter by topic if there are multiple
    topics = sorted(set(item.get("topic","") for item in board if item.get("topic")))
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
        topic_badge = f"  ·  *{item['topic']}*" if item.get("topic") else ""
        with st.expander(f"{ts}{topic_badge}  —  {item['question'][:80]}"):
            st.markdown(f"**Main question:** {item['question']}")
            # The subquestion is the more specific thing the student was stuck on
            # when they escalated -- shown separately so readers see both.
            if item.get("subquestion"):
                st.markdown(f"**Subquestion:** {item['subquestion']}")
            st.success(f"**Teaching staff:** {item['answer']}")

theme.render_attribution()
