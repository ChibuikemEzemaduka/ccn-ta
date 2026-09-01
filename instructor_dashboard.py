"""
instructor_dashboard.py  —  instructor-only dashboard

Six tabs:
  Students          manage the enrolled-student roster
  Tickets           review + reply to escalated questions; publicize to class board
  Analytics         where students struggle
  Active Homeworks  toggle homework guard on/off
  Upload Materials  year-aware material upload + index rebuild
  Generate          practice variants (text OR Wireshark-trace based)
"""

import os
import io
import re
import json
import datetime as _dt
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import ask
import auth
import add_homework
import generate_variants as gv
import theme
import llm_client

load_dotenv()

theme.inject_theme()

def get_client():
    ok, info = llm_client.check_connection()
    if not ok:
        st.sidebar.warning(f"Ollama not reachable — AI features unavailable. ({info})")
        return None
    return llm_client

client = get_client()

# ── admin login gate ───────────────────────────────────────────────────────
# The dashboard is protected by an admin username + password. The password is
# stored in .env only as a bcrypt HASH, never in plain text. Set these with the
# generate_admin_hash.py helper. See ADMIN_USERNAME / ADMIN_PASSWORD_HASH in .env.
import bcrypt

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

def _check_admin(username, password):
    """Return True only if the username matches and the password verifies
    against the stored bcrypt hash. Fails safe (False) if either is unset or
    the stored hash is malformed."""
    if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
        return False
    if username != ADMIN_USERNAME:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"),
                              ADMIN_PASSWORD_HASH.encode("utf-8"))
    except (ValueError, TypeError):
        return False

if "instructor_authed" not in st.session_state:
    st.session_state.instructor_authed = False

# Keep the instructor logged in across a page refresh: if session_state was
# cleared by the refresh but the URL carries a valid, non-expired token, restore
# the authenticated state.
if not st.session_state.instructor_authed and auth.restore_instructor_session():
    st.session_state.instructor_authed = True

if not st.session_state.instructor_authed:
    theme.render_header("TEACHING STAFF", role_color=theme.color("amber"))
    if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
        st.warning("Admin credentials are not configured. Set ADMIN_USERNAME and "
                   "ADMIN_PASSWORD_HASH in .env (use generate_admin_hash.py).")
    username = st.text_input("Admin username")
    pw = st.text_input("Admin password", type="password")
    if st.button("Enter", type="primary"):
        if _check_admin(username, pw):
            st.session_state.instructor_authed = True
            auth.start_instructor_session()   # persist across refresh (URL token)
            st.rerun()
        else:
            st.error("Incorrect username or password.")
    st.stop()

theme.render_header("TEACHING STAFF", role_color=theme.color("amber"))

with st.sidebar:
    if st.button("Sign out", width='stretch'):
        auth.end_instructor_session()
        st.session_state.instructor_authed = False
        st.rerun()

(tab_students, tab_tickets, tab_analytics,
 tab_homeworks, tab_upload, tab_variants, tab_maintenance) = st.tabs([
    "👥 Students", "🎫 Tickets", "📊 Analytics",
    "✅ Active Homeworks", "📁 Upload Materials", "🧪 Generate", "🧹 Maintenance",
])

# ── helpers ────────────────────────────────────────────────────────────────
def _fmt_time(iso):
    try:
        return _dt.datetime.fromisoformat(iso).strftime("%b %d, %Y at %I:%M %p")
    except Exception:
        return iso or "—"

def _extract_text(uploaded):
    if uploaded.name.lower().endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(uploaded.getvalue()))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    return uploaded.getvalue().decode("utf-8", errors="replace")

# ══════════════════════════════════════════════════════════════════════════════
# TAB: STUDENTS
# ══════════════════════════════════════════════════════════════════════════════
with tab_students:
    _h = st.columns([0.9, 0.1])
    _h[0].subheader("Enrolled students")
    with _h[1]:
        theme.info_button(
            "Manage the roster of students allowed to use the module. "
            "Add students one at a time or via CSV bulk import. "
            "Each student logs in with their RPI email and the course password you assign. "
            "Removing a student deactivates them without deleting their history."
        )

    students = auth.load_roster()
    active_students = [s for s in students if s.get("active", True)]

    col_add, col_csv = st.columns(2)

    with col_add:
        st.markdown("**Add a student**")
        new_name = st.text_input("Full name", key="new_student_name")
        new_email = st.text_input("RPI email", key="new_student_email")
        suggested_pw = auth.generate_temp_password()
        new_pw = st.text_input("Course password", key="new_student_pw",
                                value=suggested_pw,
                                help="Students log in with this password. You can change it anytime.")
        if st.button("➕ Add student", type="primary"):
            if not new_name.strip() or not new_email.strip() or not new_pw.strip():
                st.error("Fill in all three fields.")
            else:
                try:
                    auth.add_student(new_name.strip(), new_email.strip(), new_pw.strip())
                    st.success(f"Added {new_name}. Share their password with them.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with col_csv:
        st.markdown("**Bulk import via CSV**")
        st.caption("CSV format: one row per student — `Full Name,email@rpi.edu,password`")
        csv_file = st.file_uploader("Upload CSV", type=["csv"], key="roster_csv")
        if csv_file and st.button("Import CSV"):
            added, skipped = 0, []
            for line in csv_file.getvalue().decode("utf-8", errors="replace").splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 3:
                    skipped.append(line)
                    continue
                try:
                    auth.add_student(parts[0], parts[1], parts[2])
                    added += 1
                except ValueError:
                    skipped.append(f"{parts[1]} (already exists)")
            st.success(f"Imported {added} student(s).")
            if skipped:
                st.warning("Skipped: " + ", ".join(skipped))
            st.rerun()

    st.divider()
    st.markdown(f"**{len(active_students)} enrolled student(s)**")

    if active_students:
        for s in sorted(active_students, key=lambda x: x["name"]):
            col_info, col_pw, col_del = st.columns([3, 2, 1])
            col_info.markdown(f"**{s['name']}**  \n{s['email']}")
            replies_count = len(auth.get_student_replies(s['student_id']))
            if replies_count:
                col_info.caption(f"{replies_count} reply(ies) sent")

            with col_pw.expander("Reset password"):
                new_pw_key = f"rpw_{s['student_id']}"
                st.text_input("New password", key=new_pw_key)
                if st.button("Set password", key=f"setpw_{s['student_id']}"):
                    np = st.session_state.get(new_pw_key, "").strip()
                    if np:
                        auth.reset_password(s["email"], np)
                        st.success("Password updated.")
                    else:
                        st.error("Enter a new password first.")

            if col_del.button("Remove", key=f"del_{s['student_id']}"):
                auth.remove_student(s["email"])
                st.success(f"Removed {s['name']}.")
                st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# TAB: TICKETS
# ══════════════════════════════════════════════════════════════════════════════
with tab_tickets:
    _h = st.columns([0.9, 0.1])
    _h[0].subheader("Escalated questions")
    with _h[1]:
        theme.info_button(
            "Two sources: low course-material coverage (auto-flagged), and student "
            "escalations. For each ticket you can see the student's name and email "
            "(unless they submitted anonymously), their full conversation, and their "
            "confusion note. You can reply to the student directly, publish to the "
            "class knowledge base, and optionally post it anonymously to the public "
            "class board so all students benefit."
        )

    # Auto-refresh watcher: every few seconds this small fragment re-counts the
    # open tickets; if a new one arrived (or a reply changed the count) it reruns
    # the whole tab so the new escalation shows up without a manual refresh. It
    # renders nothing itself, so it never disturbs the reply forms below.
    @st.fragment(run_every="8s")
    def _tickets_autorefresh():
        current = len([t for t in ask.load_tickets() if t["status"] in ("open", "reopened")])
        prev = st.session_state.get("_open_ticket_count")
        if prev is not None and current != prev:
            st.session_state["_open_ticket_count"] = current
            st.rerun()   # full rerun so the ticket list rebuilds with the change
        st.session_state["_open_ticket_count"] = current
    _tickets_autorefresh()

    open_tickets = [t for t in ask.load_tickets() if t["status"] in ("open", "reopened")]

    if not open_tickets:
        st.info("No open tickets. 🎉")
    else:
        sort_choice = st.radio(
            "Order", ["Oldest first", "Newest first"],
            horizontal=True, label_visibility="collapsed",
        )
        open_tickets.sort(key=lambda t: t.get("last_time", t.get("time", "")),
                          reverse=(sort_choice == "Newest first"))

        for pos, ticket in enumerate(open_tickets, start=1):
            is_escalation = ticket["source"] == "rating"
            reopened = ticket["status"] == "reopened"
            if reopened:
                badge = "🔄 reopened by student"
            elif is_escalation:
                badge = "🙋 student escalation"
            else:
                badge = "📚 low coverage"
            count_note = f' · asked {ticket.get("count", 1)}×' if ticket.get("count", 1) > 1 else ""

            # Student attribution
            if ticket.get("anonymous"):
                student_label = "🔒 Anonymous"
                can_reply_to_student = False
            elif ticket.get("student_name"):
                student_label = f"{ticket['student_name']} ({ticket.get('student_email','')})"
                can_reply_to_student = bool(ticket.get("student_id"))
            else:
                student_label = "Unknown (pre-auth ticket)"
                can_reply_to_student = False

            ts = _fmt_time(ticket.get("time", ""))
            with st.expander(
                f'#{pos}  ·  {ts}  ·  {badge}{count_note}  ·  {ticket["question"][:65]}',
                expanded=reopened,
            ):
                st.caption(f'Created {ts}'
                           + (f' · reopened {_fmt_time(ticket.get("last_time",""))}' if reopened else ''))
                st.markdown(f'**From:** {student_label}')

                st.markdown(f'**Triggering question:** {ticket["question"]}')

                if ticket.get("conversation"):
                    st.markdown("**Full session conversation:**")
                    with st.container(border=True):
                        for turn in ticket["conversation"]:
                            with st.chat_message(turn["role"]):
                                st.markdown(turn["content"])
                elif ticket.get("shown_answer"):
                    st.markdown("**What the assistant had told them:**")
                    st.text(ticket["shown_answer"][:600])

                # The ticket thread: the student's escalation note, every reply,
                # and any reopenings -- the whole back-and-forth in order.
                _thread = ask._thread_for_display(ticket)
                if _thread:
                    st.markdown("**Ticket conversation with the student:**")
                    with st.container(border=True):
                        for msg in _thread:
                            role = "user" if msg["role"] == "student" else "assistant"
                            who = "Student" if msg["role"] == "student" else "Teaching staff"
                            with st.chat_message(role):
                                st.markdown(f"**{who}:** {msg['content']}")

                st.divider()
                ans_key = f'ans_{ticket["id"]}'
                pub_kb_key = f'pubkb_{ticket["id"]}'
                pub_board_key = f'pubboard_{ticket["id"]}'
                topic_key = f'topic_{ticket["id"]}'

                st.text_area("Your reply", key=ans_key, height=120)

                c1, c2, c3 = st.columns(3)
                c1.checkbox("Add to knowledge base (cited as Teaching Staff answer)",
                            value=True, key=pub_kb_key)
                if can_reply_to_student:
                    c2.checkbox("Post reply to student's account", value=True,
                                key=f'notify_{ticket["id"]}')
                c3.checkbox("Post to anonymous class board", value=False, key=pub_board_key)
                if st.session_state.get(pub_board_key):
                    st.text_input("Board topic label (optional, e.g. 'TCP Congestion')",
                                  key=topic_key)

                if st.button("✅ Resolve & send", key=f'resolve_{ticket["id"]}', type="primary"):
                    reply_text = st.session_state.get(ans_key, "").strip()
                    if not reply_text:
                        st.error("Write a reply first.")
                    else:
                        # Resolve + optionally add to KB
                        result = ask.resolve_ticket(
                            ticket["id"], reply_text,
                            publish=st.session_state.get(pub_kb_key, True),
                        )
                        # Post to student's account if they weren't anonymous
                        if can_reply_to_student and st.session_state.get(f'notify_{ticket["id"]}', True):
                            auth.post_reply_to_student(
                                ticket["student_id"], ticket["id"],
                                ticket["question"], reply_text,
                            )
                        # Post to anonymous class board if requested
                        if st.session_state.get(pub_board_key, False):
                            topic = st.session_state.get(topic_key, "")
                            notes = ticket.get("student_notes", [])
                            subq = notes[-1] if notes else ""
                            # Generate the professional board title ONCE, now, at
                            # publish time -- it's stored on the entry so the board
                            # never regenerates it on subsequent views.
                            board_title = ask.generate_board_title(
                                llm_client, ticket["question"], reply_text)
                            auth.publish_to_board(ticket["id"], ticket["question"],
                                                  reply_text, topic=topic,
                                                  subquestion=subq, title=board_title)
                        st.success(f"Resolved. {result}")
                        st.rerun()

    resolved = [t for t in ask.load_tickets() if t["status"] == "resolved"]
    if resolved:
        with st.expander(f"Resolved tickets ({len(resolved)})"):
            for ticket in sorted(resolved, key=lambda t: t.get("resolved_time",""), reverse=True):
                who = "🔒 Anonymous" if ticket.get("anonymous") else (
                    f'{ticket.get("student_name","Unknown")}'
                    + (f' ({ticket.get("student_email","")})' if ticket.get("student_email") else ""))
                rt = _fmt_time(ticket.get("resolved_time",""))
                ct = _fmt_time(ticket.get("time",""))
                with st.container(border=True):
                    st.markdown(f'**From:** {who}')
                    st.caption(f"Created {ct} · resolved {rt}")
                    st.markdown(f'**Triggering question:** {ticket["question"]}')

                    # Student's confusion notes, if any
                    if ticket.get("student_notes"):
                        st.markdown("**Student's confusion note(s):**")
                        for note in ticket["student_notes"]:
                            st.markdown(f"- {note}")

                    # Full conversation that led to the escalation
                    if ticket.get("conversation"):
                        with st.expander("Full session conversation"):
                            for turn in ticket["conversation"]:
                                with st.chat_message(turn["role"]):
                                    st.markdown(turn["content"])
                    elif ticket.get("shown_answer"):
                        st.markdown("**What the assistant had told them:**")
                        st.text(ticket["shown_answer"][:600])

                    # The full ticket thread (student notes, every reply, any
                    # reopenings) -- the complete back-and-forth in order.
                    _thread = ask._thread_for_display(ticket)
                    if _thread:
                        st.markdown("**Ticket conversation with the student:**")
                        with st.container(border=True):
                            for msg in _thread:
                                role = "user" if msg["role"] == "student" else "assistant"
                                who = "Student" if msg["role"] == "student" else "Teaching staff"
                                with st.chat_message(role):
                                    st.markdown(f"**{who}:** {msg['content']}")

# ══════════════════════════════════════════════════════════════════════════════
# TAB: ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    _h = st.columns([0.9, 0.1])
    _h[0].subheader("Where students struggle")
    with _h[1]:
        theme.info_button(
            "Questions grouped by meaning so paraphrases land together. "
            "Groups asked more than once with high struggle rate are pacing concerns."
        )
    threshold = st.slider("Clustering strictness", 0.30, 0.65, 0.45, 0.01)
    analytics = ask.build_analytics(cluster_threshold=threshold)
    c1, c2, c3 = st.columns(3)
    c1.metric("Questions asked", analytics["total_questions"])
    c2.metric("Ratings given", analytics["total_rated"])
    c3.metric("Open tickets", len(analytics["open_tickets"]))
    if not analytics["clusters"]:
        st.info("Not enough data yet.")
    else:
        hotspots = [c for c in analytics["clusters"]
                    if c["struggle_rate"] and c["struggle_rate"] >= 0.5 and c["asked_count"] >= 2]
        if hotspots:
            st.markdown("#### ⚠️ Possible pacing concerns")
            for c in sorted(hotspots, key=lambda c: c["asked_count"], reverse=True):
                st.warning(f'**{c["representative"]}** — asked {c["asked_count"]}×, '
                           f'{c["struggle_rate"]:.0%} struggled, {c["escalated_count"]} escalated')
        st.markdown("#### All topics, by volume")
        for c in sorted(analytics["clusters"], key=lambda c: c["asked_count"], reverse=True):
            sl = f'{c["struggle_rate"]:.0%} struggled' if c["struggle_rate"] is not None else "not yet rated"
            hw = " · 🎓 homework" if c["homework_share"] >= 0.5 else ""
            with st.expander(f'**{c["asked_count"]}×**  {c["representative"][:90]}  ·  {sl}{hw}'):
                st.markdown(f'Re-explained: {c["reexplained_count"]}  ·  Escalated: {c["escalated_count"]}')
                for q in c["sample_questions"]:
                    st.markdown(f"- {q}")

    # ── Weekly LLM insights (sits alongside the clustering above) ────────────
    st.divider()
    _wh = st.columns([0.75, 0.25])
    _wh[0].markdown("### 🗓️ Weekly insights")
    _wh[0].caption("An LLM reads each calendar week's anonymous questions and "
                   "summarizes what students asked most and where they struggled. "
                   "Each week is generated once and stored, so it isn't recomputed "
                   "on every visit.")
    # "Generate now" regenerates the most recent week on demand.
    _weeks_preview = ask._interactions_by_week()
    _newest_key = _weeks_preview[0][0] if _weeks_preview else None
    force = None
    if _wh[1].button("🔄 Generate latest now", disabled=_newest_key is None):
        force = _newest_key

    with st.spinner("Preparing weekly insights…"):
        weekly = ask.get_weekly_analytics(client, force_week=force)

    if not weekly:
        st.info("No weekly data yet — insights appear once students start asking questions.")
    else:
        for w in weekly:
            with st.expander(f"**{w['label']}**  ·  {w['count']} question"
                             f"{'s' if w['count'] != 1 else ''}",
                             expanded=(w is weekly[0])):
                st.markdown(w["summary"] or "_No summary available._")
                if w.get("generated_at"):
                    st.caption(f"Generated {w['generated_at']}")

# ══════════════════════════════════════════════════════════════════════════════
# TAB: ACTIVE HOMEWORKS
# ══════════════════════════════════════════════════════════════════════════════
with tab_homeworks:
    _h = st.columns([0.9, 0.1])
    _h[0].subheader("Active homeworks")
    with _h[1]:
        theme.info_button(
            "**The guardrail checks every ACTIVE homework, from any year.** "
            "When a student asks a question, the module compares it against every "
            "active problem you've enabled below — current or past year. If it matches, "
            "the module switches to Socratic guidance mode instead of answering directly.\n\n"
            "**Why enable past years?** Students sometimes look up a previous year's "
            "version of a problem hoping the module will answer it directly, then "
            "transfer that answer to this year's homework. Enabling the relevant past "
            "homeworks closes that loophole.\n\n"
            "**How matches are described to students:**\n"
            "- Matches this year's homework → cites the specific current homework/problem.\n"
            "- Matches a past year only → tells the student it resembles past problems, "
            "without citing a current one.\n"
            "- Matches both → treated as current; the student is only told about the "
            "current homework.\n\n"
            "Toggle any homework off once it should no longer trigger guidance."
        )

    try:
        from ingest import current_year
        CURRENT_YEAR = current_year()
    except ImportError:
        import datetime as _dt
        CURRENT_YEAR = _dt.date.today().year

    entries = add_homework.load_assignments()
    if not entries:
        st.info("No homeworks registered yet. Upload homework files and run the index rebuild.")
    else:
        by_year = {}
        for e in entries:
            year = e.get("year", CURRENT_YEAR)
            by_year.setdefault(year, []).append(e)

        # Summary line: how many active across all years
        total_active = sum(1 for e in entries if e.get("active"))
        st.caption(f"{total_active} active problem(s) across {len(by_year)} year(s) — "
                   "all active problems trigger guidance mode.")

        for year in sorted(by_year.keys(), reverse=True):
            year_entries = by_year[year]
            is_current = (year == CURRENT_YEAR)

            st.markdown(
                f"#### {year}"
                + (" — **current year**" if is_current else " — past year")
            )

            by_hw = {}
            for e in year_entries:
                base = e["id"].split("-")[0]   # HW4-P3 or HW4-P3-2025 -> HW4
                by_hw.setdefault(base, []).append(e)

            for hw_base, group in sorted(by_hw.items()):
                n_problems = len(group)
                n_active   = sum(1 for e in group if e.get("active"))
                all_active = (n_active == n_problems)

                # Every year (current AND past) is now toggleable.
                status = ("✅ active — triggers guidance" if all_active
                          else f"{n_active}/{n_problems} active"
                          if n_active else "⚪ inactive")
                label = f"{hw_base}  ·  {n_problems} problem(s)  ·  {status}"
                # Unique key per (year, hw) so past and current HW4 don't collide
                new_state = st.checkbox(label, value=all_active,
                                        key=f"hw_{year}_{hw_base}")
                if new_state != all_active:
                    add_homework.set_active(hw_base, new_state, year=year)
                    st.rerun()

                with st.expander(f"View {hw_base} ({year}) problems ({n_problems})"):
                    for e in group:
                        active_tag = "🟢" if e.get("active") else "⚪"
                        st.markdown(f"{active_tag} **{e['id']}**: {e['text'][:300]}")

            st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# TAB: UPLOAD MATERIALS
# ══════════════════════════════════════════════════════════════════════════════
# TAB: UPLOAD MATERIALS
# ══════════════════════════════════════════════════════════════════════════════
with tab_upload:
    st.subheader("Add course material")
    theme.info_button(
        "**How it works:**\n\n"
        "1. Pick a year and material type — these stay fixed while you build up your queue.\n"
        "2. Set the lecture or homework number, then attach the file for that number.\n"
        "   The file is **automatically added to the queue** the moment you attach it — "
        "   no button to click.\n"
        "3. The number advances by one so you can immediately attach the next file.\n"
        "4. Keep going until you have everything you need, then click **Save all**.\n"
        "5. Each slot (e.g. Lecture 5 slides) can hold only one file. Attaching a second "
        "   file to the same slot replaces the first in the queue.\n"
        "6. Use the **✕** button next to any queued item to remove it before saving."
    )

    import ingest as _ingest
    import re as _re

    # The canonical subfolder tree every year folder should contain. Auto-created
    # when the instructor picks a year, so no manual folder-making is ever needed
    # and every year has an identical, consistent structure.
    YEAR_SUBFOLDERS = [
        "slides", "transcripts", "handwritten_notes",
        "homework_problems", "homework_solutions", "textbook",
    ]

    def _ensure_year_tree(year_str: str):
        """Create materials/<year>/ and all standard subfolders if missing."""
        created = []
        base_dir = Path("materials") / year_str
        for sub in YEAR_SUBFOLDERS:
            d = base_dir / sub
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                created.append(sub)
        return created

    # Dynamic, wide year range from today's date — future years for pre-staging,
    # past years for archives. Current year (today) is the default selection.
    cy = _ingest.current_year()
    all_years = _ingest.supported_years()   # newest (future) first

    def _year_label(y):
        existing = (Path("materials") / str(y)).exists()
        tag = ""
        if y == cy:
            tag = "  — current year"
        elif y > cy:
            tag = "  — future"
        marker = "📁 " if existing else "＋ "   # folder exists vs. will be created
        return f"{marker}{y}{tag}"

    # Default index: the current year's position in the list
    default_idx = all_years.index(cy) if cy in all_years else 0
    year_choice = st.selectbox(
        "Year",
        all_years,
        index=default_idx,
        format_func=_year_label,
        key="up_year",
        help="Pick any year — past, current, or future. If its folders don't exist "
             "yet they're created automatically with the standard structure.",
    )
    year_folder = str(year_choice)

    # Auto-create the folder tree for the chosen year the moment it's selected.
    newly_created = _ensure_year_tree(year_folder)
    if newly_created:
        st.info(f"Created new folder tree for **{year_folder}**: "
                + ", ".join(f"`{s}`" for s in newly_created))

    base = Path(f"materials/{year_folder}")

    mat_type = st.selectbox(
        "Material type",
        ["Lecture slides (.pptx / .pdf)",
         "Lecture transcripts (.vtt / .srt / .txt)",
         "Handwritten notes (.pdf / .pptx)",
         "Homework problems (.pdf / .txt)",
         "Homework solutions — PRIVATE (.pdf / .txt)",
         "Textbook (full book .pdf / .txt)"],
        key="up_mat_type",
    )

    # Derive folder and allowed extensions from the material type
    is_lecture = mat_type.startswith("Lecture")
    is_hw      = mat_type.startswith("Homework")

    if   "slides"      in mat_type: allowed = ["pptx","pdf"];      folder = base / "slides"
    elif "transcripts" in mat_type: allowed = ["vtt","srt","txt"];  folder = base / "transcripts"
    elif "notes"       in mat_type: allowed = ["pdf","pptx"];       folder = base / "handwritten_notes"
    elif "problems"    in mat_type: allowed = ["pdf","txt"];        folder = base / "homework_problems"
    elif "solutions"   in mat_type: allowed = ["pdf","txt"];        folder = base / "homework_solutions"
    else:                           allowed = ["pdf","txt"];        folder = base / "textbook"

    # ── session state init ────────────────────────────────────────────────────
    if "upload_queue"     not in st.session_state: st.session_state.upload_queue    = []
    if "up_lec_num"  not in st.session_state: st.session_state.up_lec_num  = 1
    if "up_hw_num"   not in st.session_state: st.session_state.up_hw_num   = 1
    if "up_file_counter"  not in st.session_state: st.session_state.up_file_counter = 0
    if "up_tb_name"       not in st.session_state: st.session_state.up_tb_name      = ""

    def _slug(s):
        s = _re.sub(r"[^\w\s-]", "", s).strip()
        return _re.sub(r"\s+", "_", s)[:40]

    def _rescan_if_homework(filenames: list):
        """If any filename in the list is a homework problems or solutions file,
        re-run scan_folder so assignments.json reflects the latest content on disk.
        This keeps the guardrail cross-referencing against up-to-date material
        whenever homework files are added, replaced, or deleted — not just when
        the instructor manually triggers a full index rebuild."""
        hw_pattern = _re.compile(r"^HW\d+_(problems|solutions)\.(pdf|txt)$", _re.IGNORECASE)
        if any(hw_pattern.match(str(Path(f).name)) for f in filenames):
            with st.spinner("Homework file changed — updating active homework list..."):
                add_homework.scan_folder(use_llm=bool(client))
            st.success("Active homework list updated.", icon="✅")

    # ── number / name selector ────────────────────────────────────────────────
    st.markdown("---")

    is_tb = mat_type.startswith("Textbook")

    if is_tb:
        tb_name = st.text_input(
            "Textbook name",
            placeholder="e.g. Forouzan, Kurose_Ross",
            value=st.session_state.up_tb_name,
            help="Used only to name the saved file (e.g. Textbook_Forouzan.pdf). "
                 "The full book is indexed page by page automatically.",
            key="up_tb_name_input",
        )
        st.session_state.up_tb_name = tb_name
        st.caption("The full textbook will be saved as a single file and indexed page by page.")
    else:
        label = "Lecture number" if is_lecture else "Homework number"
        max_n = 40 if is_lecture else 20
        num = st.number_input(
            label, 1, max_n,
            value=st.session_state.up_lec_num if is_lecture else st.session_state.up_hw_num,
            step=1, key="up_num",
        )
        if is_lecture:
            st.session_state.up_lec_num = int(num)
        else:
            st.session_state.up_hw_num = int(num)

    # ── file uploader — key changes after each auto-add to reset the widget ──
    uploader_key = f"up_file_{st.session_state.up_file_counter}"
    uploaded = st.file_uploader(
        "Attach file — queued automatically when you choose a file",
        type=allowed,
        accept_multiple_files=False,
        key=uploader_key,
    )

    # ── auto-add logic ────────────────────────────────────────────────────────
    if uploaded is not None:
        ext = Path(uploaded.name).suffix.lower()

        # Build standardised filename for this slot
        if   "slides"      in mat_type: std_name = f"Lecture_{st.session_state.up_lec_num:02d}_slides{ext}"
        elif "transcripts" in mat_type: std_name = f"Lecture_{st.session_state.up_lec_num:02d}_transcript{ext}"
        elif "notes"       in mat_type: std_name = f"Lecture_{st.session_state.up_lec_num:02d}_notes{ext}"
        elif "problems"    in mat_type: std_name = f"HW{st.session_state.up_hw_num}_problems{ext}"
        elif "solutions"   in mat_type: std_name = f"HW{st.session_state.up_hw_num}_solutions{ext}"
        else:
            # Full textbook — Textbook_<name>.pdf or just Textbook.pdf
            name_slug = _slug(st.session_state.up_tb_name)
            std_name = f"Textbook_{name_slug}{ext}" if name_slug else f"Textbook{ext}"

        slot_key = f"{year_folder}|{str(folder)}|{std_name}"
        exists_on_disk = (folder / std_name).exists()

        # Find if this slot is already in the queue; replace if so (one file per slot)
        existing_idx = next(
            (i for i, q in enumerate(st.session_state.upload_queue)
             if q["slot_key"] == slot_key),
            None,
        )
        entry = {
            "slot_key":  slot_key,
            "std_name":  std_name,
            "folder":    folder,
            "data":      uploaded.getvalue(),
            "exists":    exists_on_disk,
            "overwrite": True,     # always overwrite; instructor can remove if wrong
            "year":      year_folder,
            "mat_type":  mat_type,
        }
        if existing_idx is not None:
            st.session_state.upload_queue[existing_idx] = entry
            replaced = True
        else:
            st.session_state.upload_queue.append(entry)
            replaced = False

        # Advance the number ready for the next file (not for textbook)
        if not is_tb:
            if is_lecture:
                st.session_state.up_lec_num = min(st.session_state.up_lec_num + 1, 40)
            else:
                st.session_state.up_hw_num = min(st.session_state.up_hw_num + 1, 20)

        # Reset the file uploader by changing its key
        st.session_state.up_file_counter += 1
        msg = f"{'Updated' if replaced else 'Added'} **{std_name}** in the queue."
        if exists_on_disk:
            msg += " (will overwrite the file already on disk)"
        st.session_state._upload_last_msg = msg
        st.rerun()

    # Show the feedback message from the last auto-add (survives the rerun)
    if "_upload_last_msg" in st.session_state:
        st.success(st.session_state.pop("_upload_last_msg"))

    # ── queue ─────────────────────────────────────────────────────────────────
    queue = st.session_state.upload_queue
    if queue:
        st.markdown("---")
        st.markdown(f"**Queue — {len(queue)} file(s) ready to save**")

        # Prominent overwrite warning if any queued file already exists on disk.
        _will_overwrite = [item["std_name"] for item in queue if item["exists"]]
        if _will_overwrite:
            st.warning(
                "⚠️ **Overwrite warning:** the following file(s) already exist on disk "
                "and **will be replaced** when you save:  \n"
                + ", ".join(f"`{n}`" for n in _will_overwrite)
                + "  \nRemove any you did not intend to overwrite before saving."
            )

        to_remove = []
        for idx, item in enumerate(queue):
            c_name, c_flag, c_rm = st.columns([4, 2, 1])
            icon = "⚠️" if item["exists"] else "🆕"
            c_name.markdown(f"{icon} `{item['std_name']}`")
            c_name.caption(f"{item['mat_type']}  ·  {item['year']}")
            if item["exists"]:
                c_flag.markdown("**Will overwrite**")
            else:
                c_flag.caption("New file")
            if c_rm.button("✕", key=f"rm_{idx}_{item['std_name']}", help="Remove from queue"):
                to_remove.append(idx)

        for i in sorted(to_remove, reverse=True):
            st.session_state.upload_queue.pop(i)
        if to_remove:
            st.rerun()

        col_save, col_clear = st.columns([3, 1])
        if col_save.button(f"💾 Save all ({len(queue)} file(s))", type="primary"):
            saved, skipped = [], []
            for item in queue:
                item["folder"].mkdir(parents=True, exist_ok=True)
                dest = item["folder"] / item["std_name"]
                if dest.exists() and not item["overwrite"]:
                    skipped.append(item["std_name"])
                else:
                    dest.write_bytes(item["data"])
                    saved.append(item["std_name"])
            st.session_state.upload_queue = []
            _rescan_if_homework(saved)
            st.session_state._upload_saved_msg = saved
            st.session_state._upload_skipped_msg = skipped
            if saved:
                # Flag that new material landed on disk but isn't in the index
                # yet, so the instructor gets a clear prompt to rebuild.
                st.session_state._needs_index_update = True
            st.rerun()

        if col_clear.button("🗑️ Clear all"):
            st.session_state.upload_queue = []
            st.rerun()

    # Post-save feedback + the prompt to update the index (survives the rerun).
    if "_upload_saved_msg" in st.session_state:
        _saved = st.session_state.pop("_upload_saved_msg")
        _skipped = st.session_state.pop("_upload_skipped_msg", [])
        if _saved:
            st.success(f"Saved {len(_saved)} file(s): " + ", ".join(f"`{n}`" for n in _saved))
        if _skipped:
            st.info("Skipped: " + ", ".join(f"`{n}`" for n in _skipped))

    if st.session_state.get("_needs_index_update"):
        st.warning(
            "🔄 **New material saved — update the index to make it searchable.**  \n"
            "The file(s) are on disk, but the module cannot see or cite them until "
            "the index is rebuilt. Go to **Index management** below and run "
            "**Update index (add / change only)** (fast — processes only new/changed "
            "files), or **Reset and rebuild** for a full clean rebuild."
        )

    # ── materials browser (index-driven) ──────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Browse saved & indexed materials")
    theme.info_button(
        "This shows what is **actually in the module's database** — every file that "
        "has been indexed and is searchable. Only files saved under the module's "
        "**standard naming convention** (e.g. `Lecture_05_slides.pptx`, "
        "`HW3_solutions.pdf`) are indexed. Any other files in those folders — such as "
        "original downloads you keep alongside the standardised copies — are "
        "intentionally ignored everywhere: never indexed, never counted.\n\n"
        "It reads directly from the index (`knowledge.json`, `solutions.json`, "
        "`assignments.json`), so a file appears here only if the module can really "
        "use it. Standard-named files sitting in a folder but not yet indexed (e.g. "
        "never rebuilt, or produced no extractable text) are listed separately.\n\n"
        "Use the selectors to focus on a specific year or category."
    )

    import json as _json

    def _safe_load(path):
        p = Path(path)
        if not p.exists():
            return []
        try:
            txt = p.read_text(encoding="utf-8").strip()
            return _json.loads(txt) if txt else []
        except Exception:
            return []

    # ── Build the "what is actually indexed" picture from the index files ─────
    # knowledge.json entries carry: type (slide/transcript/textbook/notes),
    # source_file, year, is_current_year. solutions.json carries hw_id/year/
    # source_file. assignments.json carries the homework-problem entries.
    knowledge = _safe_load("knowledge.json")
    solutions = _safe_load("solutions.json")
    assignments = _safe_load("assignments.json")

    # type -> { year -> { source_file -> chunk_count } }
    indexed = {}
    def _add(type_key, year, source_file):
        if not source_file:
            return
        indexed.setdefault(type_key, {}).setdefault(year, {})
        indexed[type_key][year][source_file] = indexed[type_key][year].get(source_file, 0) + 1

    TYPE_LABELS = {
        "slide":      "Lecture slides",
        "transcript": "Transcripts",
        "notes":      "Handwritten notes",
        "textbook":   "Textbook",
        "solution":   "Homework solutions",
        "problem":    "Homework problems",
    }

    for item in knowledge:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in ("slide", "transcript", "notes", "textbook"):
            _add(t, item.get("year", "unknown"), item.get("source_file", ""))

    for item in solutions:
        if isinstance(item, dict):
            _add("solution", item.get("year", "unknown"), item.get("source_file", ""))

    # Homework problems live in assignments.json. Older entries may lack
    # source_file (parsed before that field existed) — count those under a
    # placeholder so the instructor still sees they're indexed.
    for item in assignments:
        if not isinstance(item, dict):
            continue
        sf = item.get("source_file") or "(older entry — rebuild to show filename)"
        _add("problem", item.get("year", "unknown"), sf)

    # ── Files ON DISK, so we can flag anything present but NOT indexed ────────
    # We count ONLY standard-named files (the same convention ingestion uses),
    # so original downloads the instructor keeps alongside standardised copies
    # are ignored everywhere — never counted, never flagged as "not indexed".
    DISK_SUBFOLDERS = {
        "slide":      "slides",
        "transcript": "transcripts",
        "notes":      "handwritten_notes",
        "textbook":   "textbook",
        "solution":   "homework_solutions",
        "problem":    "homework_problems",
    }
    # Map browser type -> ingest category key for the standard-name check.
    _TYPE_TO_CATEGORY = {
        "slide":      "slides",
        "transcript": "transcripts",
        "notes":      "handwritten_notes",
        "textbook":   "textbook",
        "solution":   "homework_solutions",
        "problem":    "homework_problems",
    }
    try:
        from ingest import is_standard_name as _is_standard
    except ImportError:
        _is_standard = lambda cat, fn: True   # fail open if ingest unavailable

    def _disk_files(type_key, year):
        """Standard-named files on disk for a type+year. Non-standard files
        (original downloads) are ignored, exactly like ingestion does."""
        sub = DISK_SUBFOLDERS[type_key]
        cat = _TYPE_TO_CATEGORY[type_key]
        d = Path(f"materials/{year}/{sub}")
        if not d.exists():
            return {}
        return {f.name: f.stat().st_size for f in d.iterdir()
                if f.is_file() and _is_standard(cat, f.name)}

    def _fmt_size(n):
        return f"{n/1024:.0f} KB" if n < 1_000_000 else f"{n/1_048_576:.1f} MB"

    # Which years exist either in the index or on disk
    years_in_index = set()
    for t in indexed.values():
        years_in_index.update(y for y in t.keys() if isinstance(y, int))
    years_on_disk = set()
    if Path("materials").exists():
        for c in Path("materials").iterdir():
            if c.is_dir() and c.name.isdigit():
                years_on_disk.add(int(c.name))
    all_years_present = sorted(years_in_index | years_on_disk, reverse=True)

    # ── selectors ─────────────────────────────────────────────────────────────
    bcol1, bcol2 = st.columns(2)
    browse_year = bcol1.selectbox(
        "Year to browse",
        ["All years"] + [str(y) for y in all_years_present],
        index=0, key="browse_year",
    )
    type_choices = ["All categories"] + [TYPE_LABELS[t] for t in
                    ["slide", "transcript", "notes", "textbook", "solution", "problem"]]
    browse_cat = bcol2.selectbox("Category", type_choices, index=0, key="browse_cat")

    label_to_type = {v: k for k, v in TYPE_LABELS.items()}
    types_to_show = (list(TYPE_LABELS.keys()) if browse_cat == "All categories"
                     else [label_to_type[browse_cat]])
    years_to_show = (all_years_present if browse_year == "All years"
                     else [int(browse_year)])

    if not all_years_present:
        st.info("Nothing indexed yet. Upload materials and run a rebuild.")
    elif browse_year == "All years":
        # ── Cross-year summary: on-disk vs indexed, per type/year ────────────
        st.markdown("##### Standard-named files: on disk vs indexed, by year")
        st.caption("Each cell shows how many standard-named files are on disk and "
                   "how many of those are indexed (searchable). Non-standard files "
                   "are never counted.")
        rows = []
        for y in years_to_show:
            row = {"Year": str(y)}
            for t in types_to_show:
                idx_files  = indexed.get(t, {}).get(y, {})
                disk_files = _disk_files(t, y)
                # The full set of standard-named files = union of what's on disk
                # and what's indexed (indexed-but-deleted-from-disk still counts).
                all_std = set(disk_files) | set(idx_files)
                on_disk = len(disk_files)
                n_indexed = sum(1 for f in all_std if f in idx_files)
                if not all_std:
                    row[TYPE_LABELS[t]] = "—"
                else:
                    row[TYPE_LABELS[t]] = f"{on_disk} on disk / {n_indexed} indexed"
            rows.append(row)
        if rows:
            st.dataframe(pd.DataFrame(rows).set_index("Year"), width='stretch')

        # Flag anything on disk that is NOT indexed, across all years
        not_indexed_any = []
        for t in types_to_show:
            for y in years_to_show:
                idx_files = set(indexed.get(t, {}).get(y, {}).keys())
                for fname, fsize in _disk_files(t, y).items():
                    if fname not in idx_files:
                        not_indexed_any.append((y, TYPE_LABELS[t], fname, fsize))
        if not_indexed_any:
            st.markdown("")
            st.warning(f"⚠️ {len(not_indexed_any)} standard-named file(s) are on disk but "
                       "NOT indexed (run a rebuild, or they produced no extractable text):")
            for y, tlabel, fname, fsize in sorted(not_indexed_any):
                st.caption(f"  {y} · {tlabel} · `{fname}` ({_fmt_size(fsize)})")
    else:
        # ── Single-year detail: per-file, per-category, with delete ──────────
        y = int(browse_year)

        # Handle a confirmed delete from a previous run (staged by the button).
        _del_path = st.session_state.pop("_confirm_delete_path", None)
        if _del_path:
            try:
                p = Path(_del_path)
                if p.exists():
                    p.unlink()
                    st.session_state._deleted_msg = p.name
                    # The deleted file may still be in the index until a rebuild.
                    st.session_state._needs_index_update = True
            except Exception as _e:
                st.session_state._delete_error = str(_e)
            st.rerun()

        if "_deleted_msg" in st.session_state:
            st.success(f"Deleted `{st.session_state.pop('_deleted_msg')}` from disk. "
                       "Run **Reset and rebuild** below so the module stops referencing it.")
        if "_delete_error" in st.session_state:
            st.error(f"Could not delete the file: {st.session_state.pop('_delete_error')}")

        st.caption("Files on disk can be deleted here. Deleting removes the file from "
                   "disk; rebuild the index afterwards so the module stops citing it.")

        any_shown = False
        for t in types_to_show:
            idx_files = indexed.get(t, {}).get(y, {})
            disk_files = _disk_files(t, y)
            if not idx_files and not disk_files:
                continue
            any_shown = True
            st.markdown(f"**{TYPE_LABELS[t]}**")

            # Every standard-named file for this type/year (on disk and/or indexed)
            all_files = sorted(set(disk_files) | set(idx_files))
            for fname in all_files:
                on_disk = fname in disk_files
                is_indexed = fname in idx_files
                sub = DISK_SUBFOLDERS[t]
                full_path = f"materials/{y}/{sub}/{fname}"

                fcol, dcol = st.columns([5, 1])
                if is_indexed and on_disk:
                    chunks = idx_files[fname]
                    fcol.caption(f"✅ `{fname}` · {chunks} chunk(s) · "
                                 f"{_fmt_size(disk_files[fname])}  ·  indexed")
                elif is_indexed and not on_disk:
                    chunks = idx_files[fname]
                    fcol.caption(f"📄 `{fname}` · {chunks} chunk(s)  ·  indexed "
                                 "(not on disk — will disappear on next rebuild)")
                else:  # on disk, not indexed
                    fcol.caption(f"⚠️ `{fname}` · {_fmt_size(disk_files[fname])}  ·  "
                                 "on disk, NOT indexed — rebuild to include it")

                # Delete button only for files that are actually on disk.
                if on_disk:
                    dkey = f"del_{y}_{t}_{fname}"
                    ckey = f"delconfirm_{y}_{t}_{fname}"
                    if st.session_state.get(ckey):
                        # Confirmation state: show confirm/cancel.
                        if dcol.button("Confirm", key=f"{dkey}_yes", type="primary"):
                            st.session_state._confirm_delete_path = full_path
                            st.session_state.pop(ckey, None)
                            st.rerun()
                        if dcol.button("Cancel", key=f"{dkey}_no"):
                            st.session_state.pop(ckey, None)
                            st.rerun()
                    else:
                        if dcol.button("🗑️", key=dkey, help=f"Delete {fname} from disk"):
                            st.session_state[ckey] = True
                            st.rerun()

            all_std   = set(disk_files) | set(idx_files)
            n_on_disk = len(disk_files)
            n_indexed = sum(1 for f in all_std if f in idx_files)
            not_idx = [f for f in disk_files if f not in idx_files]
            summary = f"{n_on_disk} standard file(s) on disk · {n_indexed} indexed"
            if len(not_idx):
                summary += f" · {len(not_idx)} not yet indexed"
            st.caption(summary)
            st.markdown("")

        if not any_shown:
            st.info(f"Nothing indexed or on disk for {browse_year}.")


    # ── index management ──────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Index management")
    theme.info_button(
        "Three independent actions. All of them cover **every course material** — "
        "lecture slides, textbook, transcripts, notes, homework **problems**, and "
        "homework **solutions** — so nothing stale is ever left behind.\n\n"
        "**Delete index only** — wipes `chroma_db/`, `knowledge.json`, "
        "`solutions.json`, and `assignments.json` (the homework problems the "
        "guardrail matches against). The module stops answering and stops "
        "recognising any homework until you rebuild. Useful to inspect or "
        "reorganise your materials folder before rebuilding.\n\n"
        "**Reset and rebuild** — deletes all of the above, then rebuilds "
        "everything from scratch, re-embedding every chunk and re-parsing every "
        "homework problem and solution. Slower but guarantees a completely clean "
        "state. Use after reorganising, renaming, or removing files.\n\n"
        "**Update index only** — reads your materials folder and adds/updates "
        "only what changed since the last build (including new or changed "
        "homework problems and solutions). Fast; use after adding new files."
    )

    import build_index as _build_index
    import shutil as _shutil

    db_path = Path(getattr(_ingest, "DB_FOLDER", "chroma_db"))
    kj, sj = Path("knowledge.json"), Path("solutions.json")
    index_exists = db_path.exists()

    st.markdown(
        f"Current index status: {'✅ exists' if index_exists else '❌ no index found'} "
        f"(`{db_path}/`)"
    )

    idx_action = st.radio(
        "Choose action",
        ["Update index (add / change only)",
         "Reset and rebuild (delete everything, then rebuild)",
         "Delete index only (no rebuild)"],
        index=0,
        key="idx_action",
    )

    if "Reset" in idx_action or "Delete" in idx_action:
        st.warning(
            "This permanently deletes the whole index and all derived course data: "
            "`chroma_db/`, `knowledge.json`, `solutions.json`, and `assignments.json` "
            "(homework problems). The homework guardrail will recognise **no** "
            "homework until you rebuild. Make sure your materials folders are "
            "complete before a reset+rebuild."
        )

    if st.button("▶ Run", type="primary"):
        import build_index as _bi

        def _delete_index():
            # Remove EVERY piece of indexed/derived course data so nothing stale
            # survives -- including the homework PROBLEMS in assignments.json,
            # which the four-layer guardrail (signals -> supervisor -> tutor ->
            # reviewer) matches against. Leaving it behind would let the module
            # activate homework mode on problems that no longer exist.
            if db_path.exists(): _shutil.rmtree(db_path)
            for f in [kj, sj,
                      Path("assignments.json"),          # homework PROBLEMS (guardrail source)
                      Path("parse_cache.json"),
                      Path("homework_parse_cache.json")]:
                if f.exists(): f.unlink()
            # Drop cached indexes/handles so the next page load reads fresh data
            # (the student page keys its assignment-match index on a fingerprint
            # of assignments.json, so an empty file yields an empty guardrail).
            try:
                st.cache_resource.clear()
            except Exception:
                pass
            ask.reset_solutions_cache()

        def _run_build(progress_fn, incremental=True):
            _ingest.main(incremental=incremental)
            _bi.main(progress=progress_fn)
            add_homework.scan_folder(use_llm=True, incremental=incremental)
            st.cache_resource.clear()
            ask.reset_solutions_cache()   # drop stale collection handle after rebuild
            st.session_state.pop("_needs_index_update", None)  # prompt satisfied

        status = st.status("Running...", expanded=True)
        bar = st.progress(0.0)

        def report(done, total, label):
            if total: bar.progress(done / total)
            status.update(label=label)

        try:
            if "Delete index only" in idx_action:
                status.write("Deleting index files...")
                _delete_index()
                bar.progress(1.0)
                status.update(label="Index deleted. Upload is now empty — run a rebuild when ready.",
                              state="complete", expanded=False)

            elif "Reset and rebuild" in idx_action:
                status.write("Step 1/4 — deleting old index...")
                _delete_index()
                status.write("Step 2/4 — reading and chunking all materials...")
                status.write("Step 3/4 — embedding all chunks (this takes a while)...")
                status.write("Step 4/4 — re-parsing homework files...")
                _run_build(report, incremental=False)
                bar.progress(1.0)
                status.update(label="Reset and rebuild complete.", state="complete", expanded=False)

            else:  # Update only
                status.write("Step 1/3 — reading materials (unchanged files are reused)...")
                status.write("Step 2/3 — embedding new or changed chunks...")
                status.write("Step 3/3 — re-parsing homework files...")
                _run_build(report, incremental=True)
                bar.progress(1.0)
                status.update(label="Index updated (only new/changed files were processed).",
                              state="complete", expanded=False)

            # Surface any files that produced NO searchable text (image-only PDFs,
            # scans without OCR). This is the #1 cause of "0 indexed" — the file is
            # on disk but has no extractable text layer.
            if "Delete index only" not in idx_action:
                rep_path = Path("ingest_report.json")
                if rep_path.exists():
                    try:
                        rep = json.loads(rep_path.read_text(encoding="utf-8"))
                    except Exception:
                        rep = {}
                    failed = rep.get("empty_files", [])
                    no_text = rep.get("no_text", [])
                    if failed or no_text:
                        st.error(
                            "⚠️ Some files produced **no searchable text** and were NOT indexed. "
                            "This is almost always because the file is an **image-only PDF** "
                            "(e.g. PowerPoint exported as images, or a scanned document) with no "
                            "selectable text layer, and OCR isn't installed."
                        )
                        if failed:
                            st.markdown("**Files with zero extractable text:**")
                            for fn in sorted(set(failed)):
                                st.markdown(f"- `{fn}`")
                        st.markdown(
                            "**How to fix it — pick one:**\n"
                            "1. **Re-export the slides/textbook as text-based PDFs.** In PowerPoint, "
                            "use *File → Save As → PDF* (not *Export as images*). This keeps a real "
                            "text layer and is the most reliable fix.\n"
                            "2. **Upload the original `.pptx`** instead of a PDF — PowerPoint files "
                            "always carry their text.\n"
                            "3. **Install OCR** so scanned pages can be read: on the machine running "
                            "this app, `pip install pymupdf pytesseract pillow`, then install the "
                            "Tesseract engine (Windows: the UB-Mannheim installer). Then rebuild.\n\n"
                            "After fixing, run **Reset and rebuild** again."
                        )

        except Exception as e:
            status.update(label=f"Error: {e}", state="error")
            st.error(str(e))

# ══════════════════════════════════════════════════════════════════════════════
# TAB: GENERATE
# ══════════════════════════════════════════════════════════════════════════════
with tab_variants:
    _h = st.columns([0.9, 0.1])
    _h[0].subheader("Generate practice content")
    with _h[1]:
        theme.info_button(
            "Two modes:\n\n"
            "**Problem variants** — upload any problem set (homework, exam, quiz, "
            "practice set) plus its solutions, then use the chat to instruct the "
            "module exactly how you want the variant generated. For example:\n"
            "- *'Generate a variant with different subnet sizes'*\n"
            "- *'Make the problems harder by adding a second subnet'*\n"
            "- *'Focus on the CIDR notation aspect'*\n"
            "- *'Change the topology to a star network'*\n"
            "- *'Only generate a variant for problem 2'*\n\n"
            "The module generates ONE variant of the whole set (one new problem "
            "for each original problem, or just the problems you target). Previous "
            "instructions stay in the chat so you can refine iteratively.\n\n"
            "**Wireshark trace questions** — upload a .pcap capture and describe "
            "the topic you want to test; the module generates questions and "
            "step-by-step Socratic hints based on what's in the trace."
        )

    gen_mode = st.radio("Generate from", ["Problem variants", "Wireshark trace questions"],
                        horizontal=True)
    st.warning(gv.DISCLAIMER)

    # ── Mode A: problem variants — chat-driven ───────────────────────────────
    if gen_mode == "Problem variants":

        st.markdown("Upload a **problem set** (homework, exam, quiz, or practice "
                    "set) and its **solutions**. The module generates a variant — "
                    "same concepts and methods, different numbers or scenarios.")

        # Persistent file state — keep uploaded files across reruns so the
        # instructor doesn't have to re-upload after each chat message.
        col_upload1, col_upload2 = st.columns(2)
        with col_upload1:
            hw_file  = st.file_uploader("Problems (.pdf or .txt)",  type=["pdf","txt"], key="vh")
        with col_upload2:
            sol_file = st.file_uploader("Solutions (.pdf or .txt)", type=["pdf","txt"], key="vs")

        # Remember the extracted text in session state so it survives reruns
        if hw_file:
            st.session_state.variant_hw_text  = _extract_text(hw_file)
            st.session_state.variant_hw_id    = add_homework.hw_id_from_filename(hw_file.name)
            # Also keep the homework as page images so the model can SEE any
            # embedded diagrams/figures/tables when generating variants.
            if hw_file.name.lower().endswith(".pdf"):
                st.session_state.variant_hw_images = ask.make_attachment_parts(
                    [(hw_file.getvalue(), "application/pdf", hw_file.name)]
                )[0]
            else:
                st.session_state.variant_hw_images = []
        if sol_file:
            st.session_state.variant_sol_text = _extract_text(sol_file)

        hw_text  = st.session_state.get("variant_hw_text",  "")
        sol_text = st.session_state.get("variant_sol_text", "")
        hw_id    = st.session_state.get("variant_hw_id",    "Problems")
        hw_images = st.session_state.get("variant_hw_images", [])
        files_ready = bool(hw_text and sol_text)

        if not files_ready:
            st.info("Upload a problem set (homework or exam) and its solutions file "
                    "above to get started. Then use the chat to tell the module how "
                    "you want the variant generated.")
        else:
            st.success(f"Files loaded: **{hw_id}**. Use the chat below to generate the variant.")

        # ── Conversation history (shown above the chat input) ────────────────
        if "variant_chat" not in st.session_state:
            st.session_state.variant_chat = []   # list of {role, content}

        if st.session_state.variant_chat:
            if st.button("🗑️ Clear conversation", key="clear_variant_chat"):
                st.session_state.variant_chat = []
                st.session_state.pop("variant_results", None)
                st.session_state.pop("variant_last_prompt", None)
                for k in list(st.session_state.keys()):
                    if k.startswith(("vp_", "vs_")):
                        del st.session_state[k]
                st.rerun()

        for msg in st.session_state.variant_chat:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        # ── Results panel (shown after the most recent generation) ───────────
        if "variant_results" in st.session_state and gen_mode == "Problem variants":
            variants = st.session_state.variant_results
            sol_cache = st.session_state.get("variant_sol_text", "")
            last_prompt = st.session_state.get("variant_last_prompt", "")
            ok_count = sum(1 for v in variants if not v.get("error"))
            st.markdown(f"**1 variant of {hw_id} — {ok_count} problem"
                        f"{'s' if ok_count != 1 else ''}.**")
            pending = st.session_state.setdefault("pending_variant_updates", {})
            edited = []
            for idx, v in enumerate(variants):
                if idx in pending:
                    np_text, ns_text = pending.pop(idx)
                    st.session_state[f"vp_{idx}"] = np_text
                    st.session_state[f"vs_{idx}"] = ns_text
                failed = bool(v.get("error"))
                with st.expander(f"Problem {v['problem_id']}" + ("  ⚠️ failed" if failed else ""),
                                 expanded=failed):
                    if failed:
                        st.error(v["error"])
                        if st.button("🔄 Retry", key=f"retry_{idx}"):
                            nv = gv.generate_one_variant(
                                client, v["problem_id"], v["original_problem"],
                                sol_cache, instructor_prompt=last_prompt,
                                attachment_parts=st.session_state.get("variant_hw_images", []))
                            variants[idx] = nv
                            if not nv.get("error"):
                                pending[idx] = (nv["variant_problem"], nv["variant_solution"])
                            st.session_state.variant_results = variants
                            st.rerun()
                        edited.append(v); continue
                    st.session_state.setdefault(f"vp_{idx}", v["variant_problem"])
                    st.session_state.setdefault(f"vs_{idx}", v["variant_solution"])
                    st.text_area("Problem",  key=f"vp_{idx}", height=100)
                    # Show a generated figure image if the model produced one
                    if v.get("figure_image"):
                        st.image(v["figure_image"], caption="Generated figure for this problem",
                                 width='stretch')
                    st.text_area("Solution", key=f"vs_{idx}", height=140)
                    if v.get("what_changed"):
                        st.caption(f"Changed: {v['what_changed']}")
                    if last_prompt:
                        st.caption(f"Instruction: {last_prompt[:120]}")
                    col_regen, col_regen_custom = st.columns([1, 2])
                    if col_regen.button("🔄 Regenerate (same instruction)", key=f"regen_{idx}"):
                        nv = gv.generate_one_variant(
                            client, v["problem_id"], v["original_problem"],
                            sol_cache, instructor_prompt=last_prompt,
                            attachment_parts=st.session_state.get("variant_hw_images", []))
                        variants[idx] = nv
                        if not nv.get("error"):
                            pending[idx] = (nv["variant_problem"], nv["variant_solution"])
                        st.session_state.variant_results = variants
                        st.rerun()
                    edited.append({**v,
                                   "variant_problem": st.session_state[f"vp_{idx}"],
                                   "variant_solution": st.session_state[f"vs_{idx}"]})
            st.divider()
            qb = gv.doc_to_bytes(gv.build_questions_docx(hw_id, edited))
            kb = gv.doc_to_bytes(gv.build_answer_key_docx(hw_id, edited))
            c1, c2 = st.columns(2)
            c1.download_button("⬇️ Practice problems", qb,
                               f"{hw_id}_practice_problems.docx",
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            c2.download_button("⬇️ Answer key", kb,
                               f"{hw_id}_practice_answer_key.docx",
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        # ── Chat input — the instructor's prompt drives generation ────────────
        st.divider()
        typed_instruction = st.chat_input(
            "Describe how you want the variants generated…  "
            "e.g. 'Generate variants with different subnet sizes'  "
            "or 'Make the problems harder'  or 'Focus on CIDR notation'",
            disabled=not files_ready,
        )

        if typed_instruction:
            instruction = typed_instruction.strip()
            st.session_state.variant_chat.append({"role": "user", "content": instruction})
            for k in list(st.session_state.keys()):
                if k.startswith(("vp_", "vs_")):
                    del st.session_state[k]
            st.session_state.pop("pending_variant_updates", None)

            # Split the uploaded problem set into individual problems, then apply
            # any problem-targeting the instructor asked for ("only problem 2").
            probs = add_homework.llm_split(hw_text, client) if client else None
            if probs is None:
                probs = add_homework.regex_split(hw_text)
            if probs is None:
                probs = [(1, hw_text.strip())]
            probs = gv.select_target_problems(instruction, probs)
            st.session_state.variant_queue = [(f"P{n}", t) for n, t in probs]
            st.session_state.variant_total = len(probs)
            st.session_state.variant_results = []
            st.session_state.variant_last_prompt = instruction
            st.session_state.variant_stop = False
            st.session_state.variant_generating = True
            st.rerun()

        # ── Incremental generation loop (one problem per rerun) ───────────────
        if st.session_state.get("variant_generating"):
            queue = st.session_state.get("variant_queue", [])
            done = st.session_state.get("variant_results", [])
            instruction = st.session_state.get("variant_last_prompt", "")
            total = st.session_state.get("variant_total", len(done) + len(queue))

            # This is ONE variant made of `total` problems -- report progress by
            # problems completed within that single variant, not "N variants".
            prog = st.progress(min(len(done) / max(total, 1), 1.0))
            st.caption(f"Generating your variant — problem {min(len(done)+1, total)} "
                       f"of {total}…")
            if st.button("⏹ Stop generating", key="stop_variants", type="secondary"):
                st.session_state.variant_stop = True

            if st.session_state.get("variant_stop") or not queue:
                # Finished or stopped
                st.session_state.variant_generating = False
                ok = sum(1 for v in done if not v.get("error"))
                stopped = st.session_state.get("variant_stop") and queue
                # ONE variant set, containing `ok` successfully generated problems.
                summary = (f"Generated **1 variant** of **{hw_id}** "
                           f"containing **{ok} problem{'s' if ok != 1 else ''}**"
                           + (f" (of {total} targeted)" if ok != total else "")
                           + (f", following: *{instruction[:100]}{'…' if len(instruction)>100 else ''}*" if instruction else "")
                           + (" — **stopped early.**" if stopped else "."))
                st.session_state.variant_chat.append({"role": "assistant", "content": summary})
                st.session_state.variant_queue = []
                st.session_state.variant_stop = False
                st.rerun()
            else:
                # Generate the next single problem, then rerun to refresh UI
                pid, ptext = queue[0]
                with st.spinner(f"Generating {pid} of your variant…"):
                    v = gv.generate_one_variant(
                        client, pid, ptext, sol_text,
                        instructor_prompt=instruction,
                        attachment_parts=hw_images,
                    )
                done.append(v)
                st.session_state.variant_results = done
                st.session_state.variant_queue = queue[1:]
                st.rerun()

    # ── Mode B: Wireshark trace questions ────────────────────────────────────
    else:
        st.markdown("Upload a Wireshark capture and describe what you want to test. "
                    "The module parses the trace and generates targeted questions with "
                    "step-by-step Socratic hints for students to work through.")
        pcap_file = st.file_uploader("Wireshark capture (.pcap or .pcapng)",
                                     type=["pcap","pcapng","cap"], key="ws_pcap")
        topic = st.text_input("Topic to focus on",
                              placeholder="e.g. TCP connection setup, congestion control, retransmissions")
        n_questions = st.slider("Number of questions to generate", 2, 10, 5)

        if pcap_file:
            import pcap_reader as _pr
            summary, ok_parse = _pr.summarize_pcap_bytes(pcap_file.getvalue())
            if ok_parse:
                with st.expander("Parsed capture preview"):
                    st.text(summary[:2000])
            else:
                st.error(f"Could not parse this file as a capture: {summary}")

        if st.button("✨ Generate Wireshark questions", type="primary",
                     disabled=not (pcap_file and topic.strip() and client)):
            import pcap_reader as _pr
            summary, ok_parse = _pr.summarize_pcap_bytes(pcap_file.getvalue())
            if not ok_parse:
                st.error(summary)
            else:
                with st.spinner("Generating questions from the trace..."):
                    ws_qs = gv.generate_wireshark_questions(
                        client, summary, topic.strip(), n_questions)
                st.session_state.ws_questions = ws_qs
                st.session_state.ws_trace_name = pcap_file.name
                st.session_state.ws_topic = topic
                st.rerun()

        if "ws_questions" in st.session_state and gen_mode == "Wireshark trace questions":
            qs = st.session_state.ws_questions
            trace_name = st.session_state.get("ws_trace_name", "trace")
            topic_label = st.session_state.get("ws_topic", "")
            ok_count = sum(1 for q in qs if not q.get("error"))
            st.markdown(f"**{ok_count}/{len(qs)} questions generated from {trace_name}**  "
                        f"(topic: {topic_label})")
            for idx, q in enumerate(qs):
                with st.expander(f"Q{idx+1}: {q.get('question','')[:80]}", expanded=bool(q.get("error"))):
                    if q.get("error"):
                        st.error(q["error"]); continue
                    st.markdown(f"**Question:** {q['question']}")
                    st.markdown(f"**Answer:** {q['answer']}")
                    if q.get("hints"):
                        st.markdown("**Socratic hints (given one at a time):**")
                        for i, hint in enumerate(q["hints"], 1):
                            st.markdown(f"{i}. {hint}")
                    if q.get("packets_referenced"):
                        st.caption(f"References packets: {q['packets_referenced']}")

            st.divider()
            ws_qb = gv.doc_to_bytes(gv.build_wireshark_questions_docx(
                trace_name, topic_label, qs))
            ws_kb = gv.doc_to_bytes(gv.build_wireshark_answer_key_docx(
                trace_name, topic_label, qs))
            c1, c2 = st.columns(2)
            c1.download_button("⬇️ Student question sheet", ws_qb,
                               f"{trace_name}_questions.docx",
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            c2.download_button("⬇️ Answer key + hints", ws_kb,
                               f"{trace_name}_answer_key.docx",
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

# ══════════════════════════════════════════════════════════════════════════════
# TAB: MAINTENANCE  (per-semester data reset)
# ══════════════════════════════════════════════════════════════════════════════
with tab_maintenance:
    st.subheader("🧹 Clear module data for a new semester")
    st.markdown(
        "This clears the **operational data** that accumulates during a semester so "
        "you can start fresh with a new cohort. It removes: the **student roster**, "
        "**tickets**, **chat interaction logs**, **feedback**, **published board "
        "items**, **staff replies**, and the **access and error logs**."
    )
    st.info("Your **course materials and the search index are NOT affected** — only "
            "the per-semester student data is cleared. Your admin login also stays "
            "the same.")

    st.divider()
    st.markdown("#### 1. Back up first (recommended)")
    st.caption("Download a single ZIP of all current operational data before clearing. "
               "Keep it somewhere safe in case you need the old semester's records.")
    try:
        zip_bytes, included = ask.build_backup_zip()
        st.download_button(
            "⬇️ Download backup (.zip)",
            data=zip_bytes,
            file_name=f"ccnta_backup_{_dt.datetime.now().strftime('%Y%m%d_%H%M')}.zip",
            mime="application/zip",
        )
        st.caption(f"Backup will include: {', '.join(included) if included else 'no data files yet'}.")
    except Exception as _e:
        st.warning(f"Could not build the backup right now: {_e}")

    st.divider()
    st.markdown("#### 2. Clear the data")
    st.warning("⚠️ **This permanently deletes all the operational data listed above.** "
               "Students will need to be re-added for the new semester. This cannot be "
               "undone — make sure you have downloaded a backup if you might need it.")

    # Require the admin username + password again before clearing, as a
    # deliberate second gate on a destructive action.
    confirm_user = st.text_input("Re-enter admin username", key="maint_user")
    confirm_pw = st.text_input("Re-enter admin password", type="password", key="maint_pw")
    typed_phrase = st.text_input('Type CLEAR to confirm', key="maint_phrase")

    if st.button("🗑️ Permanently clear all semester data", type="primary", key="maint_clear"):
        if not _check_admin(confirm_user, confirm_pw):
            st.error("Admin username or password is incorrect. Nothing was cleared.")
        elif typed_phrase.strip().upper() != "CLEAR":
            st.error('Please type CLEAR in the confirmation box. Nothing was cleared.')
        else:
            removed = ask.clear_operational_data()
            # Drop any in-memory caches that referenced the cleared data.
            try:
                st.cache_resource.clear()
            except Exception:
                pass
            if removed:
                st.success(f"Cleared: {', '.join(removed)}. The module is ready for a "
                           f"new semester. Add your new students in the 👥 Students tab.")
            else:
                st.info("There was no operational data to clear — everything was already empty.")

theme.render_attribution()
