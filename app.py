"""
app.py  —  the entry point: streamlit run app.py

Three pages:
  student_chat.py         "Students"       — login-gated chat
  instructor_dashboard.py "Teaching Staff" — password-gated dashboard
  class_board.py          "Class Board"    — public, no login required
"""

import streamlit as st

st.set_page_config(
    page_title="CCN AI Teaching Assistant",
    page_icon="📡",
    layout="wide",
)

students = st.Page("student_chat.py", title="Students", icon="🎓", default=True)
instructors = st.Page("instructor_dashboard.py", title="Teaching Staff", icon="🧑‍🏫")
board = st.Page("class_board.py", title="Class Board", icon="📋")

page = st.navigation([students, instructors, board])
page.run()
