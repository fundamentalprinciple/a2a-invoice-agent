import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import authenticate
from app.database import get_connection, init_db
from app.decision_service import DecisionServiceError, decide_batch
from app.models import Message
from app.task_store import create_task_once, get_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="A2A Invoice Agent", lifespan=lifespan)

BATCH_MEDIA = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL_MEDIA = "application/vnd.ga5.invoice-action-proposals+json"

AGENT_CARD = {
    "name": "A2A Invoice Agent",
    "description": "Evaluates invoice packages and proposes safe actions.",
    "version": "1.0.0",
    "supportedInterfaces": [{
        "url": "http://localhost:8000",
        "protocolBinding": "HTTP+JSON",
        "protocolVersion": "1.0",
    }],
    "capabilities": {},
    "defaultInputModes": [BATCH_MEDIA],
    "defaultOutputModes": [
        PROPOSAL_MEDIA,
        "application/vnd.ga5.invoice-action-receipts+json",
    ],
    "skills": [{
        "id": "invoice_action_agent",
        "name": "Invoice Action Agent",
        "description": "Evaluates invoice evidence and proposes safe actions.",
        "tags": ["invoices", "approval", "payments"],
    }],
}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/.well-known/agent-card.json")
async def agent_card():
    return JSONResponse(AGENT_CARD)


@app.post("/message:send")
async def send_message(
    request: Request,
    principal: str = Depends(authenticate),
):
    try:
        body = await request.json()
        message = Message.model_validate(body["message"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid A2A message")

    if message.role != "ROLE_USER":
        raise HTTPException(status_code=400, detail="Expected a ROLE_USER message")

    message_data = message.model_dump(by_alias=True, exclude_none=True)
    batch_parts = [
        part for part in message.parts
        if part.media_type == BATCH_MEDIA and part.data is not None
    ]

    if len(batch_parts) > 1:
        raise HTTPException(status_code=400, detail="Expected one invoice batch")

    batch = None
    if batch_parts:
        batch = batch_parts[0].data
        if (
            not isinstance(batch, dict)
            or not isinstance(batch.get("batchId"), str)
            or not isinstance(batch.get("packages"), list)
        ):
            raise HTTPException(status_code=400, detail="Invalid invoice batch")

    # Check idempotency BEFORE calling the model.
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "contextId": message.contextId or str(uuid.uuid4()),
        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
        "artifacts": [],
        "history": [message_data],
    }

    try:
        outcome, replay = create_task_once(principal, message_data, task)
    except ValueError as exc:
        if str(exc) == "IDEMPOTENCY_CONFLICT":
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        raise

    if outcome == "replay":
        if replay is None:
            raise HTTPException(status_code=500, detail="Stored task missing")
        return JSONResponse(replay)

    if batch is not None:
        try:
            proposals = await decide_batch(batch)
        except DecisionServiceError as exc:
            # Remove the failed task and its message key, allowing a retry.
            with get_connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "DELETE FROM messages WHERE principal = ? AND message_id = ?",
                    (principal, message.messageId),
                )
                db.execute(
                    "DELETE FROM tasks WHERE id = ? AND principal = ?",
                    (task_id, principal),
                )
                db.commit()
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        task["artifacts"] = [{
            "artifactId": str(uuid.uuid4()),
            "name": "Invoice action proposals",
            "parts": [{
                "mediaType": PROPOSAL_MEDIA,
                "data": proposals,
            }],
        }]

        with get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT task_json FROM tasks WHERE id = ? AND principal = ?",
                (task_id, principal),
            ).fetchone()

            if row is None:
                db.rollback()
                raise HTTPException(status_code=409, detail="Task no longer exists")

            current = json.loads(row["task_json"])
            if current["status"]["state"] == "TASK_STATE_CANCELED":
                db.rollback()
                raise HTTPException(status_code=409, detail="Task was canceled")

            db.execute(
                """UPDATE tasks
                   SET task_json = ?, updated_at = ?
                   WHERE id = ? AND principal = ?""",
                (
                    json.dumps(task, sort_keys=True, separators=(",", ":")),
                    datetime.now(UTC).isoformat(),
                    task_id,
                    principal,
                ),
            )
            db.commit()

    return JSONResponse(task)


@app.get("/tasks/{task_id}")
async def get_task_endpoint(
    task_id: str,
    principal: str = Depends(authenticate),
):
    task = get_task(task_id, principal)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(task)


@app.get("/tasks")
async def list_tasks(principal: str = Depends(authenticate)):
    with get_connection() as db:
        rows = db.execute(
            """SELECT task_json FROM tasks
               WHERE principal = ? ORDER BY created_at DESC""",
            (principal,),
        ).fetchall()
    return JSONResponse({"tasks": [json.loads(row["task_json"]) for row in rows]})


@app.post("/tasks/{task_id}:cancel")
async def cancel_task(
    task_id: str,
    principal: str = Depends(authenticate),
):
    with get_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT task_json FROM tasks WHERE id = ? AND principal = ?",
            (task_id, principal),
        ).fetchone()

        if row is None:
            db.rollback()
            raise HTTPException(status_code=404, detail="Task not found")

        task = json.loads(row["task_json"])
        if task["status"]["state"] in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_REJECTED",
        }:
            db.rollback()
            raise HTTPException(status_code=409, detail="Task is already terminal")

        task["status"]["state"] = "TASK_STATE_CANCELED"
        db.execute(
            """UPDATE tasks SET state = ?, task_json = ?, updated_at = ?
               WHERE id = ? AND principal = ?""",
            (
                "TASK_STATE_CANCELED",
                json.dumps(task, sort_keys=True, separators=(",", ":")),
                datetime.now(UTC).isoformat(),
                task_id,
                principal,
            ),
        )
        db.commit()

    return JSONResponse(task)

