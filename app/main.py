import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import authenticate
from app.database import get_connection, init_db
from app.models import Message
from app.task_store import create_task_once, get_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="A2A Invoice Agent",
    lifespan=lifespan,
)

AGENT_CARD = {
    "name": "A2A Invoice Agent",
    "description": "An invoice agent that evaluates invoices and proposes safe actions.",
    "version": "1.0.0",
    "supportedInterfaces": [
        {
            "url": "http://localhost:8000",
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
        }
    ],
    "capabilities": {},
    "defaultInputModes": ["application/a2a+json"],
    "defaultOutputModes": ["application/a2a+json"],
    "skills": [
        {
            "id": "invoice-review",
            "name": "Invoice Review",
            "description": "Evaluate invoice packages and propose actions.",
            "tags": ["invoices", "approval", "payments"],
        }
    ],
}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/.well-known/agent-card.json")
async def agent_card():
    return JSONResponse(content=AGENT_CARD, media_type="application/json")


@app.post("/message:send")
async def send_message(
    request: Request,
    principal: str = Depends(authenticate),
):
    try:
        body = await request.json()
        message = Message.model_validate(body["message"])
    except (ValueError, KeyError):
        raise HTTPException(status_code=400, detail="Invalid A2A message")

    if message.role != "user":
        raise HTTPException(status_code=400, detail="Expected a user message")

    task_id = str(uuid.uuid4())
    context_id = message.contextId or str(uuid.uuid4())

    task = {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
        "artifacts": [],
        "history": [message.model_dump(by_alias=True, exclude_none=True)],
    }

    try:
        outcome, replay = create_task_once(
            principal,
            message.model_dump(by_alias=True, exclude_none=True),
            task,
        )
    except ValueError as exc:
        if str(exc) == "IDEMPOTENCY_CONFLICT":
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        raise

    if outcome == "replay":
        if replay is None:
            raise HTTPException(status_code=500, detail="Stored task missing")
        return JSONResponse(replay)

    return JSONResponse(task, status_code=200)


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
async def list_tasks(
    principal: str = Depends(authenticate),
):
    with get_connection() as db:
        rows = db.execute(
            """
            SELECT task_json
            FROM tasks
            WHERE principal = ?
            ORDER BY created_at DESC
            """,
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
        state = task["status"]["state"]

        if state in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_REJECTED",
        }:
            db.rollback()
            raise HTTPException(status_code=409, detail="Task is already terminal")

        task["status"]["state"] = "TASK_STATE_CANCELED"
        db.execute(
            """
            UPDATE tasks
            SET state = ?, task_json = ?, updated_at = ?
            WHERE id = ? AND principal = ?
            """,
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
