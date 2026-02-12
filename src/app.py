"""Streamlit dashboard for custody document tracking."""

import json
from datetime import datetime, date
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st

# Must be first Streamlit call
st.set_page_config(page_title="Custody Tracker", page_icon="📋", layout="wide")

from src import db
from src.config import (
    ANTHROPIC_API_KEY,
    API_TIMEOUT,
    CUSTODY_DIR,
    MODEL,
    PROCESSED_DIR,
    setup_logging,
)
from src.process_pdf import process_pdf

log = setup_logging()
db.init_db()

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("Custody Tracker")

    if st.button("Process New PDFs", use_container_width=True, type="primary"):
        pdf_files = sorted(CUSTODY_DIR.glob("*.pdf"))
        unprocessed = [f for f in pdf_files if not db.is_processed(f.name)]
        if not unprocessed:
            st.info("No new PDFs found")
        else:
            bar = st.progress(0, text=f"0/{len(unprocessed)}...")
            results, errors = [], []
            for i, pdf in enumerate(unprocessed):
                bar.progress(
                    (i) / len(unprocessed),
                    text=f"{i + 1}/{len(unprocessed)}: {pdf.name}",
                )
                try:
                    r = process_pdf(pdf)
                    if r:
                        results.append(r)
                except Exception as e:
                    log.error(f"UI batch error: {pdf.name}: {e}")
                    errors.append(f"{pdf.name}: {e}")
            bar.progress(1.0, text="Done")
            st.success(f"Processed {len(results)}, failed {len(errors)}")
            for err in errors:
                st.error(err)
            st.rerun()

    st.divider()
    st.subheader("Filters")

    docs_df = db.get_all_documents()
    doc_types = sorted(docs_df["doc_type"].dropna().unique()) if len(docs_df) > 0 else []
    urgencies = ["critical", "high", "normal", "low"]
    statuses = ["new", "reviewed", "actioned", "archived"]

    f_type = st.multiselect("Document type", doc_types)
    f_urgency = st.multiselect("Urgency", urgencies)
    f_status = st.multiselect("Status", statuses)

    st.divider()
    if st.button("Export to Excel", use_container_width=True):
        export_path = CUSTODY_DIR / "custody_export.xlsx"
        db.export_to_excel(export_path)
        st.success(f"Exported to {export_path.name}")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def urgency_color(urgency: str) -> str:
    return {
        "critical": "🔴",
        "high": "🟠",
        "normal": "🟡",
        "low": "🟢",
    }.get(urgency, "⚪")


def status_icon(status: str) -> str:
    return {
        "new": "🆕",
        "reviewed": "👁️",
        "actioned": "✅",
        "archived": "📦",
    }.get(status, "")


def filter_df(df: pd.DataFrame) -> pd.DataFrame:
    if f_type:
        df = df[df["doc_type"].isin(f_type)]
    if f_urgency:
        df = df[df["urgency"].isin(f_urgency)]
    if f_status:
        df = df[df["status"].isin(f_status)]
    return df


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_dash, tab_issues, tab_detail, tab_actions, tab_ask = st.tabs(
    ["Dashboard", "Issues", "Document Detail", "Pending Actions", "Ask"]
)

# ========================== DASHBOARD TAB ==================================
with tab_dash:
    docs_df = db.get_all_documents()
    filtered = filter_df(docs_df)

    # Metrics row
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Documents", len(docs_df))

    # Pending actions count
    with db.get_connection() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM action_items WHERE done = 0"
        ).fetchone()[0]
    c2.metric("Pending Actions", pending)

    # Urgent / overdue
    urgent_count = len(docs_df[docs_df["urgency"].isin(["critical", "high"])])
    overdue = 0
    if len(docs_df) > 0 and "deadline" in docs_df.columns:
        today = date.today().isoformat()
        overdue = len(
            docs_df[
                (docs_df["deadline"].notna())
                & (docs_df["deadline"] < today)
                & (docs_df["status"] != "archived")
            ]
        )
    c3.metric("Urgent / Overdue", f"{urgent_count} / {overdue}")

    # Total amounts this month
    total_amount = 0.0
    if len(docs_df) > 0 and "amount" in docs_df.columns:
        total_amount = docs_df["amount"].sum() or 0.0
    c4.metric("Total Amounts", f"€{total_amount:,.2f}")

    st.divider()

    if len(filtered) == 0:
        st.info("No documents yet. Scan some PDFs into the custody folder and click 'Process New PDFs'.")
    else:
        # Display table
        display_cols = [
            "id", "filename", "doc_type", "doc_date", "sender",
            "subject", "amount", "deadline", "urgency", "status", "issue_title",
        ]
        available = [c for c in display_cols if c in filtered.columns]
        show_df = filtered[available].copy()

        # Add urgency icons
        if "urgency" in show_df.columns:
            show_df["urgency"] = show_df["urgency"].apply(
                lambda u: f"{urgency_color(u)} {u}" if pd.notna(u) else ""
            )

        edited = st.data_editor(
            show_df,
            use_container_width=True,
            hide_index=True,
            disabled=[c for c in show_df.columns if c != "status"],
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "amount": st.column_config.NumberColumn("Amount (€)", format="%.2f"),
                "status": st.column_config.SelectboxColumn(
                    "Status",
                    options=statuses,
                    required=True,
                ),
            },
            key="dashboard_editor",
        )
        # Persist inline status changes
        if not edited.equals(show_df):
            for idx in edited.index:
                new_s = edited.at[idx, "status"] if "status" in edited.columns else None
                old_s = show_df.at[idx, "status"] if "status" in show_df.columns else None
                if new_s and new_s != old_s:
                    doc_id = int(filtered.iloc[idx]["id"])
                    db.update_document_status(doc_id, new_s)
            st.rerun()

# ========================== ISSUES TAB =====================================
with tab_issues:
    issues_df = db.get_all_issues()

    if len(issues_df) == 0:
        st.info("No issues yet. Process some documents first.")
    else:
        for _, issue in issues_df.iterrows():
            urg = urgency_color(issue["urgency"])
            status_str = issue["status"]
            doc_count = issue["doc_count"]
            date_range = ""
            if issue["first_seen"] and issue["latest_date"]:
                date_range = f"{issue['first_seen']} → {issue['latest_date']}"
            elif issue["first_seen"]:
                date_range = issue["first_seen"]

            header = f"{urg} **{issue['title']}** | {doc_count} doc(s) | {date_range} | {status_str}"

            with st.expander(header, expanded=(status_str == "open")):
                # Issue controls
                col_status, col_note = st.columns([1, 2])
                with col_status:
                    new_status = st.selectbox(
                        "Status",
                        ["open", "resolved", "escalated"],
                        index=["open", "resolved", "escalated"].index(status_str)
                        if status_str in ["open", "resolved", "escalated"]
                        else 0,
                        key=f"issue_status_{issue['id']}",
                    )
                    if new_status != status_str:
                        db.update_issue_status(issue["id"], new_status)
                        st.rerun()

                # Timeline
                timeline = db.get_issue_timeline(issue["id"])
                if timeline:
                    st.markdown("**Timeline:**")
                    for doc in timeline:
                        letter_badge = doc.get("letter_type", "").replace("_", " ").title()
                        doc_date = doc.get("doc_date", "?")
                        subject = doc.get("subject", "")
                        amount_str = f" | €{doc['amount']:,.2f}" if doc.get("amount") else ""
                        deadline_str = f" | Deadline: {doc['deadline']}" if doc.get("deadline") else ""

                        st.markdown(
                            f"- **{doc_date}** [{letter_badge}] {subject}{amount_str}{deadline_str}"
                        )

# ========================== DOCUMENT DETAIL TAB ============================
with tab_detail:
    docs_df = db.get_all_documents()

    if len(docs_df) == 0:
        st.info("No documents yet.")
    else:
        doc_options = {
            row["id"]: f"#{row['id']} — {row['filename']} ({row['doc_type']})"
            for _, row in docs_df.iterrows()
        }
        selected_id = st.selectbox(
            "Select document", options=list(doc_options.keys()),
            format_func=lambda x: doc_options[x],
        )

        if selected_id:
            doc = db.get_document(selected_id)

            # Two columns: image + info
            col_img, col_info = st.columns([1, 1])

            with col_img:
                stem = Path(doc["filename"]).stem
                img_path = PROCESSED_DIR / f"{stem}_p1.jpg"
                if img_path.exists():
                    st.image(str(img_path), caption="Page 1", use_container_width=True)
                else:
                    st.warning("Page image not found")

            with col_info:
                st.markdown(f"**Type:** {doc.get('doc_type', '—')}")
                st.markdown(f"**Date:** {doc.get('doc_date', '—')}")
                st.markdown(f"**Sender:** {doc.get('sender', '—')}")
                st.markdown(f"**Subject:** {doc.get('subject', '—')}")
                st.markdown(f"**Amount:** {'€{:,.2f}'.format(doc['amount']) if doc.get('amount') else '—'}")
                st.markdown(f"**Deadline:** {doc.get('deadline') or '—'}")
                st.markdown(f"**Urgency:** {urgency_color(doc.get('urgency', ''))} {doc.get('urgency', '—')}")
                st.markdown(f"**Issue:** {doc.get('issue_title') or '—'}")

                # Status selector
                current_status = doc.get("status", "new")
                new_status = st.selectbox(
                    "Status",
                    statuses,
                    index=statuses.index(current_status) if current_status in statuses else 0,
                    key=f"doc_status_{selected_id}",
                )
                if new_status != current_status:
                    db.update_document_status(selected_id, new_status)
                    st.rerun()

                # Issue reassignment (5D)
                all_issues = db.get_all_issues()
                issue_options = {0: "— None —"}
                for _, iss in all_issues.iterrows():
                    issue_options[int(iss["id"])] = f"#{int(iss['id'])}: {iss['title']}"
                current_issue = doc.get("issue_id") or 0
                new_issue = st.selectbox(
                    "Reassign issue",
                    options=list(issue_options.keys()),
                    format_func=lambda x: issue_options[x],
                    index=list(issue_options.keys()).index(current_issue)
                    if current_issue in issue_options
                    else 0,
                    key=f"reassign_{selected_id}",
                )
                if new_issue != current_issue:
                    db.reassign_document_issue(
                        selected_id, new_issue if new_issue else None
                    )
                    st.rerun()

                # Re-extract / Delete buttons (5C)
                col_re, col_del = st.columns(2)
                with col_re:
                    if st.button("🔄 Re-extract", key=f"reprocess_{selected_id}"):
                        pdf_path = CUSTODY_DIR / doc["filename"]
                        if pdf_path.exists():
                            with st.spinner("Re-extracting..."):
                                process_pdf(pdf_path, force=True)
                            st.success("Done — re-extracted")
                            st.rerun()
                        else:
                            st.error(f"PDF not found: {doc['filename']}")
                with col_del:
                    if st.button(
                        "🗑️ Delete from DB",
                        key=f"delete_{selected_id}",
                        type="secondary",
                    ):
                        db.delete_document(selected_id)
                        st.success("Deleted")
                        st.rerun()

            st.divider()

            # Summary and recommendation
            st.markdown(f"**Summary:** {doc.get('summary_en', '—')}")
            st.markdown(f"**Recommendation:** {doc.get('recommendation', '—')}")

            st.divider()

            # Action items
            st.subheader("Action Items")
            actions = doc.get("actions", [])
            if not actions:
                st.write("No action items.")
            else:
                for action in actions:
                    col_check, col_text = st.columns([0.05, 0.95])
                    with col_check:
                        done = st.checkbox(
                            "done",
                            value=bool(action["done"]),
                            key=f"action_{action['id']}",
                            label_visibility="collapsed",
                            on_change=db.update_action_done,
                            args=(action["id"], not bool(action["done"])),
                        )
                    with col_text:
                        deadline_str = f" (by {action['deadline']})" if action.get("deadline") else ""
                        text = action["action_text"] + deadline_str
                        if done:
                            st.markdown(f"~~{text}~~")
                        else:
                            st.markdown(text)

            # Expandable sections
            with st.expander("Full German Text"):
                json_path = doc.get("json_path")
                if json_path and Path(json_path).exists():
                    full_data = json.loads(Path(json_path).read_text(encoding="utf-8"))
                    st.text(full_data.get("full_text_de", "Not available"))
                else:
                    st.write("JSON file not found")

            with st.expander("Raw JSON"):
                if json_path and Path(json_path).exists():
                    st.json(json.loads(Path(json_path).read_text(encoding="utf-8")))

# ========================== PENDING ACTIONS TAB ============================
with tab_actions:
    today_str = date.today().isoformat()

    # Metrics from both sources
    all_actions_df = db.get_all_actions(pending_only=False)
    all_personal = db.get_personal_tasks(pending_only=False)
    doc_pending = len(all_actions_df[all_actions_df["done"] == 0])
    doc_done = len(all_actions_df[all_actions_df["done"] == 1])
    pt_pending = sum(1 for t in all_personal if not t["done"])
    pt_done = sum(1 for t in all_personal if t["done"])
    total_pending = doc_pending + pt_pending
    total_done = doc_done + pt_done

    with_deadline = all_actions_df[
        (all_actions_df["done"] == 0) & (all_actions_df["action_deadline"].notna())
    ]
    overdue_doc = (
        len(with_deadline[with_deadline["action_deadline"] < today_str])
        if len(with_deadline) > 0
        else 0
    )
    overdue_pt = sum(
        1
        for t in all_personal
        if not t["done"] and t.get("deadline") and t["deadline"] < today_str
    )

    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("Pending", total_pending)
    mc2.metric("Completed", total_done)
    mc3.metric("Overdue", overdue_doc + overdue_pt)

    st.divider()

    # ---- MY TASKS (personal) ----
    st.subheader("My Tasks")

    # Add new task form
    with st.form("add_task", clear_on_submit=True):
        fc1, fc2, fc3 = st.columns([5, 2, 1])
        with fc1:
            new_task = st.text_input("Task", placeholder="e.g. Order birthday cake")
        with fc2:
            new_deadline = st.date_input("Deadline", value=None, format="YYYY-MM-DD")
        with fc3:
            st.markdown("<br>", unsafe_allow_html=True)
            submitted = st.form_submit_button("Add", use_container_width=True)
        if submitted and new_task.strip():
            dl_str_val = new_deadline.isoformat() if new_deadline else None
            db.add_personal_task(new_task.strip(), dl_str_val)
            st.rerun()

    # List personal tasks
    show_done_pt = st.checkbox("Show completed", value=False, key="pt_show_done")
    personal_tasks = db.get_personal_tasks(pending_only=not show_done_pt)

    for task in personal_tasks:
        tid = task["id"]
        is_done = bool(task["done"])
        col_c, col_t, col_d = st.columns([0.04, 0.86, 0.10])
        with col_c:
            st.checkbox(
                "done",
                value=is_done,
                key=f"pt_{tid}",
                label_visibility="collapsed",
                on_change=db.update_personal_task_done,
                args=(tid, not is_done),
            )
        with col_t:
            dl = task.get("deadline")
            dl_tag = ""
            if dl:
                if not is_done and dl < today_str:
                    dl_tag = f" | ⚠️ **OVERDUE: {dl}**"
                else:
                    dl_tag = f" | by {dl}"
            if is_done:
                st.markdown(f"~~{task['task_text']}~~{dl_tag}")
                st.caption(f"Completed {task.get('done_at', '')}")
            else:
                st.markdown(f"**{task['task_text']}**{dl_tag}")
        with col_d:
            if st.button("✕", key=f"pt_del_{tid}", help="Delete task"):
                db.delete_personal_task(tid)
                st.rerun()

    st.divider()

    # ---- DOCUMENT ACTIONS ----
    st.subheader("Document Actions")

    show_completed = st.checkbox(
        "Show completed", value=False, key="da_show_done"
    )
    actions_df = db.get_all_actions(pending_only=not show_completed)

    if len(actions_df) == 0:
        st.info(
            "No pending document actions."
            if not show_completed
            else "No document actions found."
        )
    else:
        for _, act in actions_df.iterrows():
            action_id = int(act["id"])
            is_done = bool(act["done"])

            sender = act.get("sender") or "Unknown"
            subject = act.get("subject") or ""
            urg = urgency_color(act.get("doc_urgency", "normal"))
            dl = act.get("action_deadline")
            dl_str = ""
            if dl:
                if not is_done and str(dl) < today_str:
                    dl_str = f" | ⚠️ **OVERDUE: {dl}**"
                else:
                    dl_str = f" | by {dl}"

            issue_str = (
                f" | {act['issue_title']}" if act.get("issue_title") else ""
            )

            col_check, col_body = st.columns([0.04, 0.96])
            with col_check:
                st.checkbox(
                    "done",
                    value=is_done,
                    key=f"pa_{action_id}",
                    label_visibility="collapsed",
                    on_change=db.update_action_done,
                    args=(action_id, not is_done),
                )
            with col_body:
                action_text = act["action_text"]
                context_line = (
                    f"*{urg} {sender} — {subject}{dl_str}{issue_str}*"
                )
                if is_done:
                    st.markdown(f"~~{action_text}~~")
                    st.caption(f"Completed {act.get('done_at', '')}")
                else:
                    st.markdown(f"**{action_text}**")
                    st.caption(context_line)

# ========================== ASK TAB ========================================
with tab_ask:
    st.subheader("Ask about your documents & tasks")
    st.caption("Ask questions about documents, personal tasks, deadlines, or issues.")

    # Chat history in session state
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # Display chat history
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat input
    question = st.chat_input("e.g., What invoices are unpaid? When is the birthday party?")

    if question:
        st.session_state.chat_history.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        # Build context from DB — capped at 50 docs / 8000 chars (5B)
        all_docs = db.get_all_documents()
        all_issues = db.get_all_issues()

        MAX_CTX_DOCS = 50
        MAX_CTX_CHARS = 8000

        context_parts = ["DOCUMENT DATABASE:"]
        doc_count = 0
        for _, doc in all_docs.iterrows():
            if doc_count >= MAX_CTX_DOCS:
                remaining = len(all_docs) - doc_count
                context_parts.append(
                    f"...and {remaining} more documents (ask about specific docs by ID)"
                )
                break
            line = (
                f"- Doc #{doc['id']}: {doc['filename']} | type: {doc['doc_type']} | "
                f"date: {doc['doc_date']} | sender: {doc['sender']} | "
                f"subject: {doc['subject']} | amount: {doc['amount']} | "
                f"deadline: {doc['deadline']} | urgency: {doc['urgency']} | "
                f"status: {doc['status']} | issue: {doc.get('issue_title', 'none')}"
            )
            context_parts.append(line)
            doc_count += 1

        if len(all_issues) > 0:
            context_parts.append("\nISSUES:")
            for _, iss in all_issues.iterrows():
                context_parts.append(
                    f"- Issue #{iss['id']}: {iss['title']} | sender: {iss['sender']} | "
                    f"urgency: {iss['urgency']} | status: {iss['status']} | "
                    f"docs: {iss['doc_count']} | dates: {iss['first_seen']} to {iss['latest_date']} | "
                    f"deadline: {iss['latest_deadline']}"
                )

        personal_tasks = db.get_personal_tasks(pending_only=False)
        if personal_tasks:
            context_parts.append("\nPERSONAL TASKS:")
            for t in personal_tasks:
                status = "DONE" if t["done"] else "PENDING"
                dl = f" | deadline: {t['deadline']}" if t.get("deadline") else ""
                context_parts.append(
                    f"- Task #{t['id']}: {t['task_text']} | {status}{dl}"
                )

        context_parts.append(f"\nToday's date: {date.today().isoformat()}")

        context = "\n".join(context_parts)
        if len(context) > MAX_CTX_CHARS:
            context = context[:MAX_CTX_CHARS] + "\n... (context truncated)"

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    client = anthropic.Anthropic(
                        api_key=ANTHROPIC_API_KEY, timeout=API_TIMEOUT
                    )
                    response = client.messages.create(
                        model=MODEL,
                        max_tokens=2048,
                        system=(
                            "You help manage elder care documents and personal tasks for a family caregiver. "
                            "Answer questions about documents, personal tasks, deadlines, issues, and action items "
                            "based on the database context provided. Be concise and actionable. "
                            "Use English. Reference specific document IDs, task IDs, and issue IDs when relevant."
                        ),
                        messages=[
                            {"role": "user", "content": f"{context}\n\nQuestion: {question}"},
                        ],
                    )
                    answer = response.content[0].text
                except Exception as e:
                    answer = f"Error: {e}"

            st.markdown(answer)
            st.session_state.chat_history.append({"role": "assistant", "content": answer})
