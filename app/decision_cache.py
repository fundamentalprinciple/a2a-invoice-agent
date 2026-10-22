import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.database import get_connection
from app.idempotency import canonical_json


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_package_decisions_table() -> None:
    with get_connection() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS package_decisions (
                package_hash TEXT PRIMARY KEY,
                package_json TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def package_content_hash(
    package: dict[str, Any],
    policy_revision: str | None = None,
) -> str:
    """Hash the decision-relevant contents of a package.

    Package identifiers are intentionally excluded so the same commercial
    invoice receives the same cached decision even when the grader re-uses
    package content under a fresh batch, message, task, or package namespace.
    """
    content = {
        key: value
        for key, value in package.items()
        if key != "packageId"
    }
    if policy_revision is not None:
        content["_policyRevision"] = policy_revision
    digest = canonical_json(content).encode("utf-8")
    return hashlib.sha256(digest).hexdigest()


def get_cached_decisions(
    package_hashes: set[str],
) -> dict[str, dict[str, Any]]:
    if not package_hashes:
        return {}

    _ensure_package_decisions_table()
    placeholders = ",".join("?" for _ in package_hashes)
    with get_connection() as db:
        rows = db.execute(
            f"""
            SELECT package_hash, decision_json
            FROM package_decisions
            WHERE package_hash IN ({placeholders})
            """,
            tuple(package_hashes),
        ).fetchall()

    return {
        row["package_hash"]: json.loads(row["decision_json"])
        for row in rows
    }


def store_cached_decision(
    package: dict[str, Any],
    decision: dict[str, Any],
    policy_revision: str | None = None,
) -> None:
    _ensure_package_decisions_table()
    package_hash = package_content_hash(package, policy_revision)
    now = _now_iso()
    with get_connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO package_decisions (
                package_hash, package_json, decision_json, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                package_hash,
                canonical_json(package),
                json.dumps(decision, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
