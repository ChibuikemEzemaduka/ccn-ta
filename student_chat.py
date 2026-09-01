"""
student_chat.py  —  the student-facing chat page

Launched via the router in app.py. Students must log in with their RPI email
and course password before any chat is visible.
"""

import streamlit as st
import chromadb

import ask
import auth
import theme
import llm_client

theme.inject_theme()
theme.render_header("STUDENT")

# ---- 1. Authentication gate -----------------------------------------------
auth.restore_student_session()   # keep login across page refresh (URL token)
auth.require_login()   # renders login form and calls st.stop() if not logged in
student = auth.current_student()  # {'student_id', 'name', 'email'}

# Record the start of this login session in the access log (identifies the
# account, by design). Done once per session; the key is held in session_state
# so a page rerun doesn't create duplicate entries.
if "access_session_key" not in st.session_state:
    st.session_state.access_session_key = ask.log_access_start(student["email"])

# ---- 2. LLM connectivity check --------------------------------------------
@st.cache_resource
def get_collection():
    """Cache only the knowledge collection handle (expensive to open)."""
    chroma = chromadb.PersistentClient(path=ask.DB_FOLDER)
    return chroma.get_collection(ask.COLLECTION_NAME)

try:
    collection = get_collection()
except Exception as _err:
    print(f"=== BACKEND: could not open knowledge index: {_err}. "
          f"Run 'python ingest.py' then 'python build_index.py'. ===", flush=True)
    st.warning("⚠️ The assistant is temporarily unavailable. Please try again in a "
               "few minutes.")
    st.stop()

# Active assignments are loaded fresh every run (cheap JSON read) so newly
# uploaded/toggled homework takes effect immediately. The match index (an
# embedded collection) is cached keyed by a fingerprint of the active set, so
# it's only rebuilt when the homework list actually changes — fast on every
# message, but seamless when the instructor adds or toggles homework.
active_assignments = ask.load_active_assignments()

@st.cache_resource
def _build_match_index(fingerprint: str, _assignments):
    return ask.build_assignment_index(_assignments)

_fp = "|".join(sorted(f'{a["id"]}:{a.get("content_hash","")}:{a.get("active")}'
                      for a in active_assignments))
assignment_col = _build_match_index(_fp, active_assignments) if active_assignments else None

ok, info = llm_client.check_connection()
if not ok:
    # Detailed reason to the terminal for the instructor/IT; generic to students.
    print(f"=== BACKEND: cannot reach model server at {llm_client.OLLAMA_BASE_URL}: "
          f"{info} ===", flush=True)
    ask.log_error("(startup connectivity check)", info, stage="connectivity")
    st.warning("⚠️ The assistant is temporarily unavailable. Please try again in a "
               "few minutes.")
    st.stop()
# The "is the model pulled?" check only makes sense for LOCAL Ollama, where
# models live in your machine's /api/tags list. On Ollama Cloud the model runs
# server-side and never appears in that list, so we skip the check there --
# an invalid cloud model name will surface as a clear error on the first call.
if not getattr(llm_client, "USE_CLOUD", False) and llm_client.TUTOR_MODEL not in info:
    print(f"=== BACKEND: model '{llm_client.TUTOR_MODEL}' not pulled. "
          f"Run: ollama pull {llm_client.TUTOR_MODEL} ===", flush=True)
    st.warning("⚠️ The assistant is temporarily unavailable. Please try again in a "
               "few minutes.")
    st.stop()

client = llm_client
llm_client.warm_up()   # preload the model so the first message isn't slow

# ---- 3. Sidebar -----------------------------------------------------------
with st.sidebar:
    st.caption(f"Signed in as **{student['name']}**")
    st.caption(student['email'])
    if st.button("Sign out", width='stretch'):
        # Close out the access-log session (records logout time + duration).
        ask.log_access_end(st.session_state.pop("access_session_key", None))
        auth.logout()
        st.rerun()

    # ---- Change password ---------------------------------------------------
    with st.expander("🔑 Change password"):
        cur_pw = st.text_input("Current password", type="password", key="cp_current")
        new_pw = st.text_input("New password (min 8 characters)", type="password", key="cp_new")
        new_pw2 = st.text_input("Confirm new password", type="password", key="cp_confirm")
        if st.button("Update password", key="cp_submit", width='stretch'):
            if new_pw != new_pw2:
                st.error("The new passwords do not match.")
            else:
                ok, msg = auth.change_own_password(student['email'], cur_pw, new_pw)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

    st.divider()
    if st.button("🔄 New conversation", width='stretch'):
        for k in ["messages","guard_session","session_phase",
                  "pending_confusion_context","pending_question","pending_attachments",
                  "pending_inference","stop_requested","resume_phase"]:
            st.session_state.pop(k, None)
        # also clear per-message flow keys
        for k in [k for k in st.session_state.keys() if k.startswith("flow_")]:
            st.session_state.pop(k, None)
        st.rerun()
    theme.info_button(
        "**Students page.** Ask anything about ECSE 4670. Answers are grounded in "
        "the course slides, lecture video transcripts, and textbook, and cite where "
        "they came from. You can attach a photo, PDF, or Wireshark .pcap capture.\n\n"
        "After every reply, choose one of the options shown. In homework guidance "
        "mode, choosing **Yes, I understand. Continue** will prompt you to type your "
        "own answer or next step, which the assistant will then check and build on.\n\n"
        "You can press **Stop** at any time while the assistant is thinking, then "
        "retype your message.\n\n"
        "**New conversation** clears the assistant's memory and starts fresh."
    )

# ---- 4. Session state ------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "guard_session" not in st.session_state:
    st.session_state.guard_session = {}
if "session_phase" not in st.session_state:
    st.session_state.session_phase = "ask"
if "pending_inference" not in st.session_state:
    # Holds {question, display_files} while the user's message is shown and the
    # model is about to run on the NEXT render. This is what lets the student see
    # their own question immediately instead of it vanishing during inference.
    st.session_state.pending_inference = None
if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False

# ---- 5. Flow callbacks ----------------------------------------------------
# Each assistant message gets a stable flow key based on its position in the
# messages list at the time it was appended, stored as "msg_id" on the message.
# This survives reruns and list mutations without drifting.

def _msg_id(i, message):
    """Return the stable id for this message's flow state. We prefer the
    'msg_id' field baked into the message dict (set when it is first appended)
    so the key never drifts if earlier messages are inserted. Falls back to
    the loop index for messages that predate this change."""
    return message.get("msg_id", i)

def _flow(msg_id):
    """Get-or-create the flow state dict for a given message id."""
    key = f"flow_{msg_id}"
    if key not in st.session_state:
        st.session_state[key] = {"stage": "rating"}
    return st.session_state[key]

def on_yes(msg_id, question, answer_text, homework_on, interaction_id):
    ask.log_understanding(question, answer_text, "yes", homework_on, interaction_id)
    _flow(msg_id)["stage"] = "done_yes_hw" if homework_on else "done_yes"
    st.session_state.session_phase = "ask"

def on_need_clarify(msg_id, level, question, answer_text, homework_on, interaction_id):
    ask.log_understanding(question, answer_text, level, homework_on, interaction_id)
    _flow(msg_id)["stage"] = "awaiting_confusion"
    st.session_state.pending_confusion_context = {
        "msg_id": msg_id, "question": question, "answer": answer_text,
        "homework_on": homework_on, "interaction_id": interaction_id, "level": level,
    }
    st.session_state.session_phase = "awaiting_confusion_reexplain"

def on_escalate_click(msg_id, question, answer_text, homework_on, interaction_id):
    _flow(msg_id)["stage"] = "awaiting_confusion"
    st.session_state.pending_confusion_context = {
        "msg_id": msg_id, "question": question, "answer": answer_text,
        "homework_on": homework_on, "interaction_id": interaction_id, "level": "no",
    }
    st.session_state.session_phase = "awaiting_confusion_escalate"

def on_end_session(msg_id):
    _flow(msg_id)["stage"] = "done_ended"
    st.session_state.session_phase = "ended"

# Safety net: if session_phase is "awaiting_rating" but the last assistant
# message's flow state was lost (e.g. browser refresh, partial session reset),
# restore it to "rating" so the buttons always reappear. Without this a student
# can get stuck with a blank area and no way to continue the conversation.
if st.session_state.session_phase == "awaiting_rating":
    _msgs = st.session_state.get("messages", [])
    _last = next(
        ((j, m) for j, m in reversed(list(enumerate(_msgs)))
         if m.get("role") == "assistant"),
        None,
    )
    if _last is not None:
        _j, _m = _last
        _mid = _msg_id(_j, _m)
        if f"flow_{_mid}" not in st.session_state:
            st.session_state[f"flow_{_mid}"] = {"stage": "rating"}

# ---- 5b. My escalated questions (main page, above the chat) ---------------
# A professional dropdown showing the student's escalated tickets: pending and
# answered, each with the full conversation and a reopen option. Placed here in
# the main area (above the conversation) so it's always visible, not tucked in a
# sidebar the student might have collapsed.
import datetime as _dt2

def _fmt_ticket_time(ts):
    try:
        return _dt2.datetime.fromisoformat(ts).strftime("%b %d, %I:%M %p")
    except Exception:
        return ts or ""

# Apply any reopen submitted on the previous run (staged by the button below).
_reopen_tid = st.session_state.pop("_do_reopen_ticket", None)
if _reopen_tid:
    _reopen_msg = st.session_state.pop("_do_reopen_msg", "").strip()
    if _reopen_msg:
        ask.reopen_ticket(_reopen_tid, _reopen_msg)
        st.session_state["_reopen_flash"] = True

_my_tickets = ask.get_tickets_for_student(student['student_id'])
if _my_tickets:
    _active = [t for t in _my_tickets if t.get("status") in ("open", "reopened")]
    _answered = [t for t in _my_tickets if t.get("status") == "resolved"]
    _unread = sum(1 for r in auth.get_student_replies(student['student_id']) if not r['read'])

    _hdr = f"🎫 My escalated questions — {len(_active)} awaiting reply · {len(_answered)} answered"
    if _unread:
        _hdr += f"   •   {_unread} new repl" + ("ies" if _unread != 1 else "y")
    _open = bool(_active) or bool(_unread) or st.session_state.pop("_reopen_flash", False)

    with st.expander(_hdr, expanded=_open):
        auth.mark_replies_read(student['student_id'])

        def _thread_block(t):
            thread = ask._thread_for_display(t)
            if not thread:
                return
            for msg in thread:
                role = "user" if msg["role"] == "student" else "assistant"
                who = "You" if msg["role"] == "student" else "Teaching staff"
                with st.chat_message(role):
                    st.markdown(f"**{who}:** {msg['content']}")

        if _answered:
            st.markdown("##### ✅ Answered")
            for t in _answered:
                with st.container(border=True):
                    hc = st.columns([0.82, 0.18])
                    hc[0].markdown(f"**{t['question']}**")
                    hc[1].markdown(
                        "<div style='text-align:right'><span style='background:#1E7A34;"
                        "color:#fff;padding:2px 9px;border-radius:11px;font-size:0.72em;'>"
                        "ANSWERED</span></div>", unsafe_allow_html=True)
                    st.caption(f"Escalated {_fmt_ticket_time(t.get('time',''))}  ·  "
                               f"answered {_fmt_ticket_time(t.get('resolved_time',''))}")
                    with st.expander("View full conversation"):
                        _thread_block(t)
                    with st.expander("Still unclear? Ask a follow-up"):
                        rk = f"reopen_msg_{t['id']}"
                        st.text_area("Your follow-up question for the teaching staff",
                                     key=rk, height=80,
                                     placeholder="Explain what still isn't clear…")
                        if st.button("↩️ Send follow-up & reopen", key=f"reopen_btn_{t['id']}"):
                            msg = st.session_state.get(rk, "").strip()
                            if msg:
                                st.session_state["_do_reopen_ticket"] = t["id"]
                                st.session_state["_do_reopen_msg"] = msg
                                st.rerun()
                            else:
                                st.warning("Type your follow-up question first.")

        if _active:
            st.markdown("##### ⏳ Awaiting a teaching staff reply")
            for t in _active:
                reop = t.get("status") == "reopened"
                with st.container(border=True):
                    hc = st.columns([0.82, 0.18])
                    hc[0].markdown(f"**{t['question']}**")
                    hc[1].markdown(
                        "<div style='text-align:right'><span style='background:#B26A00;"
                        "color:#fff;padding:2px 9px;border-radius:11px;font-size:0.72em;'>"
                        "PENDING</span></div>", unsafe_allow_html=True)
                    st.caption(f"Escalated {_fmt_ticket_time(t.get('time',''))}"
                               + ("  ·  reopened with a follow-up" if reop else "")
                               + "  ·  not yet answered")
                    with st.expander("View full conversation"):
                        _thread_block(t)
    st.divider()

# ---- 6. Conversation render -----------------------------------------------
for i, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "user" and message.get("attachments"):
            for name, mime in message["attachments"]:
                lower = (name or "").lower()
                if lower.endswith((".pcap", ".pcapng", ".cap")):
                    st.caption(f"📎 {name} (Wireshark capture)")
                elif mime.startswith("image/"):
                    st.caption(f"📎 {name}")
                else:
                    st.caption(f"📎 {name} (PDF)")
        if message["role"] == "assistant":
            if message.get("homework_on"):
                problem_id = message.get("problem_id")
                hw_label = ask.friendly_problem_label(problem_id) if problem_id else "current homework"
                st.markdown(theme.citation_chip_html(
                    f"🎓 HOMEWORK GUIDANCE MODE · {hw_label}", amber=True),
                    unsafe_allow_html=True)
            if message.get("flagged"):
                st.markdown(theme.citation_chip_html("⚑ FLAGGED FOR INSTRUCTOR", amber=True),
                           unsafe_allow_html=True)
            q_text = message.get("question", "")
            a_text = message["content"]
            hw = message.get("homework_on", False)
            interaction_id = message.get("interaction_id")
            mid = _msg_id(i, message)   # stable id for this message's flow state
            flow = _flow(mid)
            stage = flow["stage"]

            if stage == "rating":
                st.caption("Do you understand the hint? Choose an option above to continue 👇")
                row1 = st.columns(3)
                row1[0].button("✅ I understand", key=f"yes_{mid}", width='stretch',
                               on_click=on_yes, args=(mid, q_text, a_text, hw, interaction_id))
                row1[1].button("🤔 Partly — clarify", key=f"partly_{mid}", width='stretch',
                               on_click=on_need_clarify,
                               args=(mid, "partly", q_text, a_text, hw, interaction_id))
                row1[2].button("❌ I don't understand", key=f"no_{mid}", width='stretch',
                               on_click=on_need_clarify,
                               args=(mid, "no", q_text, a_text, hw, interaction_id))
                row2 = st.columns(2)
                row2[0].button("🧑‍🏫 Escalate question to teaching staff", key=f"escalate_{mid}",
                               width='stretch', on_click=on_escalate_click,
                               args=(mid, q_text, a_text, hw, interaction_id))
                row2[1].button("🏁 No more help needed", key=f"end_{mid}",
                               width='stretch', on_click=on_end_session, args=(mid,))
            elif stage == "awaiting_confusion":
                st.caption("✍️ Type what's confusing you in the box below — "
                           "be as specific as you can, and I'll try a different approach.")
            elif stage == "done_yes_hw":
                st.info("👉 **Your turn.** Type your answer or your next step for this "
                        "problem in the box below, and I'll check your thinking and guide "
                        "you to the next part. (If you'd rather ask something else, you can "
                        "just type that instead.)")
            elif stage == "done_yes":
                st.caption("Great — glad it clicked! ✍️ Ask your next question in the box below 👇")
            elif stage == "done_reexplained":
                st.caption("Let's try again — read the new explanation above, then reply "
                           "in the box below with your answer or what still isn't clear 👇")
            elif stage == "done_escalated":
                st.caption("✅ Your question has been sent to the teaching staff. "
                           "You'll see their reply in the sidebar inbox when they respond. "
                           "This session has ended.")
            else:
                st.caption("Session ended. Click 🔄 New conversation in the sidebar to start a new one.")

# Keep the view anchored to the newest message after every rerun, so the page
# doesn't jump back to the top when a response comes in.
if st.session_state.messages:
    theme.scroll_to_bottom()

# ---- 7. Gated input box ---------------------------------------------------
phase = st.session_state.session_phase
file_types = ["png","jpg","jpeg","webp","gif","pdf","pcap","pcapng","cap"]

# ---- 7a. THINKING PHASE: the user's message is already shown above; run the
# model now, streaming the reply so tokens appear live. ---------------------
if phase == "thinking":
    pend = st.session_state.pending_inference or {}

    # Pre-inference stop: if the student already pressed stop, honour it before
    # we kick off the model.
    if st.session_state.stop_requested:
        st.session_state.stop_requested = False
        st.session_state.pending_inference = None
        if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
            st.session_state.messages.pop()
        st.session_state.session_phase = st.session_state.pop("resume_phase", "ask")
        st.rerun()

    question = pend.get("question", "")
    display_files = pend.get("display_files", [])
    attachment_parts, pcap_summaries, skipped = ask.make_attachment_parts(display_files)
    if skipped:
        st.warning("Skipped: " + ", ".join(skipped))
    history = [{"role":m["role"],"content":m["content"]}
               for m in st.session_state.messages[:-1]]

    # ── Thinking indicator + Stop button (shown while the model works) ───────
    # The Stop button lets the student cancel — e.g. after a mistaken prompt —
    # and re-type. It sets stop_requested and reruns; on that rerun the
    # pre-inference stop block above drops the pending message and returns to
    # the input box before any reply is committed.
    status_slot = st.empty()
    stop_cols = st.columns([1, 3])
    if stop_cols[0].button("⏹ Stop", key="stop_thinking", width='stretch'):
        st.session_state.stop_requested = True
        st.rerun()
    status_slot.caption("🤔 Thinking — working out the best way to guide you…")

    # Stream the reply live inside an assistant bubble. For non-homework
    # questions this shows tokens as they generate; homework questions are
    # fully vetted first inside respond_stream() and arrive in one piece.
    #
    # Any backend error (model overload, network issue, etc.) is caught here: the
    # full detail is printed to the terminal and written to the anonymous error
    # log, while the student sees only a friendly generic message -- never a raw
    # traceback or server error text.
    final = {}
    inference_error = None
    def _token_stream():
        for item in ask.respond_stream(client, collection, question, assignment_col,
                                       active_assignments, history,
                                       st.session_state.guard_session,
                                       attachment_parts=attachment_parts,
                                       pcap_summaries=pcap_summaries):
            if isinstance(item, dict) and item.get("__final__"):
                final.update(item)
            else:
                yield item

    with st.chat_message("assistant"):
        try:
            st.write_stream(_token_stream)
        except Exception as err:
            inference_error = err
            # Full detail to the backend terminal (for the instructor/IT only).
            import traceback as _tb
            print("=== BACKEND ERROR during student inference ===", flush=True)
            _tb.print_exc()
            # Anonymous record: prompt + error, NO student identity.
            ask.log_error(question, err, stage="student_inference")
            # Generic, reassuring message for the student.
            st.warning("⚠️ The assistant is experiencing high demand right now and "
                       "couldn't complete that request. Please wait a moment and try "
                       "again.")

    # Clear the thinking indicator now that the reply is in.
    status_slot.empty()

    # On error, discard this attempt cleanly and return to the input box so the
    # student can retry, without recording a broken assistant message.
    if inference_error is not None:
        st.session_state.pending_inference = None
        if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
            st.session_state.messages.pop()
        st.session_state.session_phase = st.session_state.pop("resume_phase", "ask")
        st.stop()

    # If the student pressed Stop while the model was generating, honour it now:
    # discard the reply and the pending message, and return to the input box.
    if st.session_state.stop_requested:
        st.session_state.stop_requested = False
        st.session_state.pending_inference = None
        if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
            st.session_state.messages.pop()
        st.session_state.session_phase = st.session_state.pop("resume_phase", "ask")
        st.rerun()

    # respond_stream always yields a final dict; fall back defensively.
    if not final:
        final = {"reply": "", "homework_on": False, "flagged": False,
                 "session": st.session_state.guard_session, "interaction_id": None,
                 "problem_id": None}

    st.session_state.guard_session = final.get("session", st.session_state.guard_session)
    new_msg_id = len(st.session_state.messages)
    st.session_state.messages.append({
        "role": "assistant",
        "content": final.get("reply", ""),
        "flagged": final.get("flagged", False),
        "homework_on": final.get("homework_on", False),
        "question": question,
        "interaction_id": final.get("interaction_id"),
        "problem_id": final.get("problem_id"),
        "msg_id": new_msg_id,
    })
    st.session_state[f"flow_{new_msg_id}"] = {"stage": "rating"}
    st.session_state.pending_inference = None
    st.session_state.pop("resume_phase", None)
    st.session_state.session_phase = "awaiting_rating"
    st.rerun()

# ---- 7b. Otherwise, render the appropriate input control ------------------

if phase == "awaiting_rating":
    st.info("👆 Do you understand the hint? Choose an option above to continue.")
    typed = None
elif phase == "ended":
    st.info("This session has ended. Click **🔄 New conversation** in the sidebar to start a new one.")
    typed = None
elif phase == "thinking":
    typed = None   # handled above
elif phase == "awaiting_confusion_reexplain":
    typed = st.chat_input("Type your answer, or tell me what's still confusing — "
                          "I'll guide you from there…",
                          accept_file="multiple", file_type=file_types)
elif phase == "awaiting_confusion_escalate":
    typed = st.chat_input("Describe your question for the teaching staff…",
                          accept_file="multiple", file_type=file_types)
else:
    typed = st.chat_input("Type your question or your answer here… "
                          "(you can attach a photo, PDF, or Wireshark .pcap)",
                          accept_file="multiple", file_type=file_types)

pending = st.session_state.pop("pending_question", None)
pending_files_raw = st.session_state.pop("pending_attachments", [])

# ---- 8. Confusion / escalation handling -----------------------------------
if phase == "awaiting_confusion_reexplain" and typed:
    confusion = (typed.text or "").strip() or "I still don't understand; can you explain differently?"
    ctx = st.session_state.pop("pending_confusion_context", {})
    ask.log_followup_outcome(
        ctx.get("question",""), ctx.get("answer",""), ctx.get("level",""),
        confusion, "reexplained", ctx.get("homework_on",False), ctx.get("interaction_id"),
        student_id=student['student_id'], student_name=student['name'],
        student_email=student['email'], anonymous=False,
    )
    mid = ctx.get("msg_id", ctx.get("message_index"))
    if mid is not None:
        _flow(mid)["stage"] = "done_reexplained"
    st.session_state.pending_question = (
        f"Here is my response: {confusion}\n\n"
        "Please continue guiding me — if I gave an answer, check my thinking; "
        "if I'm still confused, explain it in simpler terms and a different way than before."
    )
    if typed.files:
        st.session_state.pending_attachments = [(f.getvalue(),f.type,f.name) for f in typed.files]
    st.session_state.session_phase = "ask"
    st.rerun()

elif phase == "awaiting_confusion_escalate" and typed:
    confusion = (typed.text or "").strip() or "(student did not provide additional detail)"
    ctx = st.session_state.pop("pending_confusion_context", {})
    anonymous = st.session_state.pop("escalate_anonymous", False)

    conversation_snapshot = [{"role":m["role"],"content":m["content"]}
                             for m in st.session_state.messages]
    conversation_snapshot.append({"role":"user","content":f"[Escalation note] {confusion}"})
    ask.log_followup_outcome(
        ctx.get("question",""), ctx.get("answer",""), ctx.get("level",""),
        confusion, "escalated", ctx.get("homework_on",False), ctx.get("interaction_id"),
        conversation=conversation_snapshot,
        student_id=student['student_id'], student_name=student['name'],
        student_email=student['email'], anonymous=anonymous,
    )
    if "message_index" in ctx or "msg_id" in ctx:
        mid = ctx.get("msg_id", ctx.get("message_index"))
        _flow(mid)["stage"] = "done_escalated"
    st.session_state.session_phase = "ended"
    st.rerun()

# ---- Anonymous toggle (shown only when awaiting escalation confusion) -----
elif phase == "awaiting_confusion_escalate":
    if "escalate_anonymous" not in st.session_state:
        st.session_state.escalate_anonymous = False
    anon = st.toggle("Submit anonymously (teaching staff won't see your name or email)",
                     key="anon_toggle",
                     value=st.session_state.escalate_anonymous)
    st.session_state.escalate_anonymous = anon

# ---- 9. Normal question path: append the message, then switch to "thinking"
# so the student SEES their question immediately on the next render. --------
else:
    question, display_files = None, []
    if pending:
        question = pending
        display_files = pending_files_raw
    elif typed:
        question = typed.text
        display_files = [(f.getvalue(),f.type,f.name) for f in typed.files]

    if question or display_files:
        question = question or ""
        # Optimistically append the user message so it renders right away.
        st.session_state.messages.append({
            "role": "user",
            "content": question or "(attachment, no caption)",
            "attachments": [(name,mime) for _,mime,name in display_files],
        })
        # Stash what the model needs; inference runs on the next render in the
        # "thinking" phase (where the Stop button is shown).
        st.session_state.pending_inference = {
            "question": question,
            "display_files": display_files,
        }
        st.session_state.resume_phase = "ask"
        st.session_state.stop_requested = False
        st.session_state.session_phase = "thinking"
        st.rerun()

theme.render_attribution()
