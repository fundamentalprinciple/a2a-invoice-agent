import json
from datetime import UTC, datetime
from typing import Any

from app.database import get_connection
from app.idempotency import message_hash


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def save_task(task: dict[str, Any]) -> None:
    now = now_iso()
    with get_connection() as db:
        db.execute(
            """
            INSERT INTO tasks (id, principal, state, task_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                task_json = excluded.task_json,
                updated_at = excluded.updated_at
            """,
            (
                task["id"],
                task["principal"],
                task["status"]["state"],
                json.dumps(task["task"], separators=(",", ":"), sort_keys=True),
                now,
                now,
            ),
        )


def get_task(task_id: str, principal: str) -> dict[str, Any] | None:
    with get_connection() as db:
        row = db.execute(
            "SELECT task_json FROM tasks WHERE id = ? AND principal = ?",
            (task_id, principal),
        ).fetchone()
    return json.loads(row["task_json"]) if row else None


def get_message_replay(
    principal: str,
    message_id: str,
    message: dict[str, Any],
) -> tuple[str, str] | None:
    digest = message_hash(message)
    with get_connection() as db:
        row = db.execute(
            """
            SELECT message_hash, task_id
            FROM messages
            WHERE principal = ? AND message_id = ?
            """,
            (principal, message_id),
        ).fetchone()

    if row is None:
        return None
    if row["message_hash"] != digest:
        raise ValueError("IDEMPOTENCY_CONFLICT")
    return row["task_id"], digest


def create_task_once(
    principal: str,
    message: dict[str, Any],
    task: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Return ('created', None), ('replay', task), or raise ValueError on conflict."""
    message_id = message["messageId"]
    digest = message_hash(message)
    now = now_iso()

    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")

        existing = db.execute(
            """
            SELECT message_hash, task_id
            FROM messages
            WHERE principal = ? AND message_id = ?
            """,
            (principal, message_id),
        ).fetchone()

        if existing:
            if existing["message_hash"] != digest:
                db.rollback()
                raise ValueError("IDEMPOTENCY_CONFLICT")

            row = db.execute(
                "SELECT task_json FROM tasks WHERE id = ? AND principal = ?",
                (existing["task_id"], principal),
            ).fetchone()
            db.commit()
            return "replay", json.loads(row["task_json"]) if row else None

        db.execute(
            """
            INSERT INTO tasks (id, principal, state, task_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task["id"],
                principal,
                task["status"]["state"],
                json.dumps(task, sort_keys=True, separators=(",", ":")),
                now,
                now,
            ),
        )
        db.execute(
            """
            INSERT INTO messages (principal, message_id, message_hash, task_id)
            VALUES (?, ?, ?, ?)
            """,
            (principal, message_id, digest, task["id"]),
        )
        db.commit()

    return "created", None
