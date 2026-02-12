"""SQLite database schema and CRUD operations for Mamadoc document tracking."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    sender          TEXT,
    ref_number      TEXT,
    category        TEXT,
    status          TEXT DEFAULT 'open',
    first_seen      TEXT,
    latest_date     TEXT,
    latest_deadline TEXT,
    urgency         TEXT DEFAULT 'normal',
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT UNIQUE NOT NULL,
    processed_at    TEXT NOT NULL,
    doc_type        TEXT,
    doc_date        TEXT,
    sender          TEXT,
    subject         TEXT,
    amount          REAL,
    deadline        TEXT,
    urgency         TEXT DEFAULT 'normal',
    letter_type     TEXT,
    summary_en      TEXT,
    recommendation  TEXT,
    json_path       TEXT,
    page_count      INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'new',
    issue_id        INTEGER REFERENCES issues(id)
);

CREATE TABLE IF NOT EXISTS action_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          INTEGER REFERENCES documents(id),
    action_text     TEXT NOT NULL,
    deadline        TEXT,
    done            INTEGER DEFAULT 0,
    done_at         TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS personal_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_text       TEXT NOT NULL,
    deadline        TEXT,
    done            INTEGER DEFAULT 0,
    done_at         TEXT,
    created_at      TEXT NOT NULL,
    notes           TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def is_processed(filename: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM documents WHERE filename = ?", (filename,)
        ).fetchone()
        return row is not None


def insert_document(data: dict) -> int:
    """Insert a new document. Use upsert_document_with_actions for atomic ops."""
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO documents
               (filename, processed_at, doc_type, doc_date, sender, subject,
                amount, deadline, urgency, letter_type, summary_en, recommendation,
                json_path, page_count, status, issue_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["filename"],
                _now(),
                data.get("doc_type"),
                data.get("doc_date"),
                data.get("sender"),
                data.get("subject"),
                data.get("amount"),
                data.get("deadline"),
                data.get("urgency", "normal"),
                data.get("letter_type"),
                data.get("summary_en"),
                data.get("recommendation"),
                data.get("json_path"),
                data.get("page_count", 1),
                "new",
                data.get("issue_id"),
            ),
        )
        return cur.lastrowid


def insert_action_items(doc_id: int, items: list[dict]):
    with get_connection() as conn:
        for item in items:
            conn.execute(
                """INSERT INTO action_items (doc_id, action_text, deadline)
                   VALUES (?, ?, ?)""",
                (doc_id, item.get("action", ""), item.get("deadline")),
            )


def upsert_document_with_actions(data: dict, actions: list[dict]) -> int:
    """Insert or update a document and its action items atomically.

    If ``filename`` already exists: UPDATE the row, DELETE old actions, INSERT new.
    If new: INSERT document + actions.
    All within one transaction.
    """
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM documents WHERE filename = ?", (data["filename"],)
        ).fetchone()

        if existing:
            doc_id = existing["id"]
            conn.execute(
                """UPDATE documents SET
                    processed_at=?, doc_type=?, doc_date=?, sender=?, subject=?,
                    amount=?, deadline=?, urgency=?, letter_type=?, summary_en=?,
                    recommendation=?, json_path=?, page_count=?
                   WHERE id=?""",
                (
                    _now(),
                    data.get("doc_type"),
                    data.get("doc_date"),
                    data.get("sender"),
                    data.get("subject"),
                    data.get("amount"),
                    data.get("deadline"),
                    data.get("urgency", "normal"),
                    data.get("letter_type"),
                    data.get("summary_en"),
                    data.get("recommendation"),
                    data.get("json_path"),
                    data.get("page_count", 1),
                    doc_id,
                ),
            )
            # Replace action items
            conn.execute("DELETE FROM action_items WHERE doc_id = ?", (doc_id,))
        else:
            cur = conn.execute(
                """INSERT INTO documents
                   (filename, processed_at, doc_type, doc_date, sender, subject,
                    amount, deadline, urgency, letter_type, summary_en, recommendation,
                    json_path, page_count, status, issue_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["filename"],
                    _now(),
                    data.get("doc_type"),
                    data.get("doc_date"),
                    data.get("sender"),
                    data.get("subject"),
                    data.get("amount"),
                    data.get("deadline"),
                    data.get("urgency", "normal"),
                    data.get("letter_type"),
                    data.get("summary_en"),
                    data.get("recommendation"),
                    data.get("json_path"),
                    data.get("page_count", 1),
                    "new",
                    data.get("issue_id"),
                ),
            )
            doc_id = cur.lastrowid

        for item in actions:
            conn.execute(
                "INSERT INTO action_items (doc_id, action_text, deadline) VALUES (?, ?, ?)",
                (doc_id, item.get("action", ""), item.get("deadline")),
            )
        return doc_id


def delete_document(doc_id: int):
    """Delete a document and its action items. Does NOT delete the parent issue."""
    with get_connection() as conn:
        conn.execute("DELETE FROM action_items WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def get_all_documents(
    doc_type: Optional[str] = None,
    urgency: Optional[str] = None,
    status: Optional[str] = None,
) -> pd.DataFrame:
    query = """
        SELECT d.*, i.title as issue_title
        FROM documents d
        LEFT JOIN issues i ON d.issue_id = i.id
        WHERE 1=1
    """
    params = []
    if doc_type:
        query += " AND d.doc_type = ?"
        params.append(doc_type)
    if urgency:
        query += " AND d.urgency = ?"
        params.append(urgency)
    if status:
        query += " AND d.status = ?"
        params.append(status)
    query += " ORDER BY d.doc_date DESC, d.processed_at DESC"

    with get_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)


def get_document(doc_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            """SELECT d.*, i.title as issue_title
               FROM documents d
               LEFT JOIN issues i ON d.issue_id = i.id
               WHERE d.id = ?""",
            (doc_id,),
        ).fetchone()
        if not row:
            return None
        doc = dict(row)
        actions = conn.execute(
            "SELECT * FROM action_items WHERE doc_id = ? ORDER BY id",
            (doc_id,),
        ).fetchall()
        doc["actions"] = [dict(a) for a in actions]
        return doc


def get_document_by_filename(filename: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE filename = ?", (filename,)
        ).fetchone()
        if row:
            return get_document(row["id"])
        return None


def update_action_done(action_id: int, done: bool, notes: str = ""):
    with get_connection() as conn:
        conn.execute(
            """UPDATE action_items
               SET done = ?, done_at = ?, notes = COALESCE(NULLIF(?, ''), notes)
               WHERE id = ?""",
            (1 if done else 0, _now() if done else None, notes, action_id),
        )


def update_document_status(doc_id: int, status: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE documents SET status = ? WHERE id = ?", (status, doc_id)
        )


# ---------------------------------------------------------------------------
# Personal Tasks
# ---------------------------------------------------------------------------

def add_personal_task(task_text: str, deadline: str | None = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO personal_tasks (task_text, deadline, created_at) VALUES (?, ?, ?)",
            (task_text, deadline, _now()),
        )
        return cur.lastrowid


def update_personal_task_done(task_id: int, done: bool):
    with get_connection() as conn:
        conn.execute(
            "UPDATE personal_tasks SET done = ?, done_at = ? WHERE id = ?",
            (1 if done else 0, _now() if done else None, task_id),
        )


def delete_personal_task(task_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM personal_tasks WHERE id = ?", (task_id,))


def get_personal_tasks(pending_only: bool = False) -> list[dict]:
    with get_connection() as conn:
        query = "SELECT * FROM personal_tasks"
        if pending_only:
            query += " WHERE done = 0"
        query += " ORDER BY done ASC, deadline ASC NULLS LAST, id ASC"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]


def get_all_actions(pending_only: bool = False) -> pd.DataFrame:
    """Get all action items with parent document info."""
    query = """
        SELECT a.id, a.action_text, a.deadline as action_deadline,
               a.done, a.done_at, a.notes,
               d.id as doc_id, d.filename, d.sender, d.subject,
               d.doc_date, d.urgency as doc_urgency,
               i.title as issue_title
        FROM action_items a
        JOIN documents d ON a.doc_id = d.id
        LEFT JOIN issues i ON d.issue_id = i.id
    """
    if pending_only:
        query += " WHERE a.done = 0"
    query += """
        ORDER BY
            a.done ASC,
            CASE d.urgency
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'normal' THEN 2 WHEN 'low' THEN 3
            END,
            a.deadline ASC NULLS LAST,
            a.id ASC
    """
    with get_connection() as conn:
        return pd.read_sql_query(query, conn)


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

def get_all_issues() -> pd.DataFrame:
    query = """
        SELECT i.*,
               COUNT(d.id) as doc_count,
               SUM(CASE WHEN d.status = 'new' THEN 1 ELSE 0 END) as new_docs
        FROM issues i
        LEFT JOIN documents d ON d.issue_id = i.id
        GROUP BY i.id
        ORDER BY
            CASE i.urgency
                WHEN 'critical' THEN 0
                WHEN 'high' THEN 1
                WHEN 'normal' THEN 2
                WHEN 'low' THEN 3
            END,
            i.latest_deadline ASC
    """
    with get_connection() as conn:
        return pd.read_sql_query(query, conn)


def get_issue_timeline(issue_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, filename, doc_date, doc_type, letter_type, subject,
                      summary_en, amount, deadline, urgency, status
               FROM documents
               WHERE issue_id = ?
               ORDER BY doc_date ASC, processed_at ASC""",
            (issue_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_issue(data: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO issues
               (title, sender, ref_number, category, status,
                first_seen, latest_date, latest_deadline, urgency)
               VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (
                data["title"],
                data.get("sender"),
                data.get("ref_number"),
                data.get("category"),
                data.get("first_seen"),
                data.get("latest_date"),
                data.get("latest_deadline"),
                data.get("urgency", "normal"),
            ),
        )
        return cur.lastrowid


def link_document_to_issue(doc_id: int, issue_id: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE documents SET issue_id = ? WHERE id = ?",
            (issue_id, doc_id),
        )
        # Update issue dates from all linked documents
        conn.execute(
            """UPDATE issues SET
                 first_seen = (SELECT MIN(doc_date) FROM documents WHERE issue_id = ? AND doc_date IS NOT NULL),
                 latest_date = (SELECT MAX(doc_date) FROM documents WHERE issue_id = ? AND doc_date IS NOT NULL),
                 latest_deadline = (SELECT MAX(deadline) FROM documents WHERE issue_id = ? AND deadline IS NOT NULL),
                 urgency = (
                     SELECT urgency FROM documents WHERE issue_id = ?
                     ORDER BY CASE urgency
                         WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                         WHEN 'normal' THEN 2 WHEN 'low' THEN 3
                     END
                     LIMIT 1
                 )
               WHERE id = ?""",
            (issue_id, issue_id, issue_id, issue_id, issue_id),
        )


def reassign_document_issue(doc_id: int, new_issue_id: int | None):
    """Move a document to a different issue (or unlink with None)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE documents SET issue_id = ? WHERE id = ?",
            (new_issue_id, doc_id),
        )
    if new_issue_id:
        link_document_to_issue(doc_id, new_issue_id)


def update_issue_status(issue_id: int, status: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE issues SET status = ? WHERE id = ?", (status, issue_id)
        )


def get_issues_summary_for_linking() -> list[dict]:
    """Get a compact list of issues for the Claude linking prompt."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT i.id, i.title, i.sender, i.ref_number, i.category,
                      i.first_seen, i.latest_date, i.status,
                      COUNT(d.id) as doc_count
               FROM issues i
               LEFT JOIN documents d ON d.issue_id = i.id
               WHERE i.status != 'resolved'
               GROUP BY i.id
               ORDER BY i.latest_date DESC""",
        ).fetchall()
        return [dict(r) for r in rows]


def export_to_excel(path: Path):
    with get_connection() as conn:
        docs_df = pd.read_sql_query(
            """SELECT d.*, i.title as issue_title
               FROM documents d
               LEFT JOIN issues i ON d.issue_id = i.id
               ORDER BY d.doc_date DESC""",
            conn,
        )
        actions_df = pd.read_sql_query(
            """SELECT a.*, d.filename, d.sender, d.subject
               FROM action_items a
               JOIN documents d ON a.doc_id = d.id
               ORDER BY a.done ASC, a.deadline ASC""",
            conn,
        )
        issues_df = pd.read_sql_query(
            "SELECT * FROM issues ORDER BY latest_deadline ASC", conn
        )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        docs_df.to_excel(writer, sheet_name="Documents", index=False)
        actions_df.to_excel(writer, sheet_name="Actions", index=False)
        issues_df.to_excel(writer, sheet_name="Issues", index=False)
