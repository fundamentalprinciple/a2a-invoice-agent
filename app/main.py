import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import authenticate
from app.config import get_settings
from app.database import get_connection, init_db
from app.decision_service import DecisionServiceError, decide_batch
from app.models import Message
from app.task_store import (
    complete_task_once,
    create_task_once,
    delete_failed_task,
    get_message_replay,
    get_task,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="A2A Invoice Agent", lifespan=lifespan)
a2a_router = APIRouter()

BATCH_MEDIA = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL_MEDIA = "application/vnd.ga5.invoice-action-proposals+json"
RESULTS_MEDIA = "application/vnd.ga5.invoice-action-results+json"
RECEIPTS_MEDIA = "application/vnd.ga5.invoice-action-receipts+json"

def build_agent_card() -> dict[str, Any]:
    return {
        "name": "A2A Invoice Agent",
        "description": "Evaluates invoice packages and proposes safe actions.",
        "version": "1.0.0",
        "supportedInterfaces": [{
            "url": get_settings().public_base_url,
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
        }],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": [BATCH_MEDIA],
        "defaultOutputModes": [
            PROPOSAL_MEDIA,
            RECEIPTS_MEDIA,
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
    return JSONResponse(build_agent_card())

async def wait_for_task(
    task_id: str,
    principal: str,
    timeout: float = 60,
) -> dict | None:
    deadline = asyncio.get_running_loop().time() + timeout

    while asyncio.get_running_loop().time() < deadline:
        task = get_task(task_id, principal)

        if task is None:
            return None

        if task["status"]["state"] != "TASK_STATE_WORKING":
            return task

        await asyncio.sleep(0.05)

    return None


def _find_proposal_artifact(task: dict[str, Any]) -> dict[str, Any] | None:
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("mediaType") == PROPOSAL_MEDIA:
                data = part.get("data")
                if isinstance(data, dict):
                    return data
    return None


def _validate_batch_shape(batch: Any) -> None:
    if not isinstance(batch, dict):
        raise HTTPException(status_code=400, detail="Invalid invoice batch")

    batch_id = batch.get("batchId")
    if not isinstance(batch_id, str) or not batch_id:
        raise HTTPException(status_code=400, detail="Invalid invoice batch")

    packages = batch.get("packages")
    if not isinstance(packages, list) or not packages:
        raise HTTPException(status_code=400, detail="Invalid invoice batch")

    package_ids: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            raise HTTPException(status_code=400, detail="Invalid invoice batch")
        package_id = package.get("packageId")
        if not isinstance(package_id, str) or not package_id:
            raise HTTPException(status_code=400, detail="Invalid invoice batch")
        package_ids.append(package_id)

    if len(set(package_ids)) != len(package_ids):
        raise HTTPException(status_code=400, detail="Duplicate package IDs")


def _validate_results_message(
    message: Message,
    data: Any,
    task: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    if message.taskId != task["id"]:
        raise HTTPException(status_code=409, detail="Task ID does not match")
    if message.contextId != task["contextId"]:
        raise HTTPException(status_code=409, detail="Context ID does not match")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid invoice action results")

    batch_id = data.get("batchId")
    results = data.get("results")
    if not isinstance(batch_id, str) or not batch_id:
        raise HTTPException(status_code=400, detail="Results must include batchId")
    if not isinstance(results, list) or not results:
        raise HTTPException(
            status_code=400,
            detail="Results must include a non-empty results list",
        )

    proposals_data = _find_proposal_artifact(task)
    if proposals_data is None:
        raise HTTPException(status_code=409, detail="Task has no proposals to act on")
    if proposals_data.get("batchId") != batch_id:
        raise HTTPException(status_code=409, detail="Batch ID does not match")

    proposals = proposals_data.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        raise HTTPException(
            status_code=409,
            detail="Stored proposals are missing or invalid",
        )

    proposal_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise HTTPException(status_code=409, detail="Stored proposal is invalid")
        package_id = proposal.get("packageId")
        action_id = proposal.get("actionId")
        action = proposal.get("action")
        if not all(isinstance(value, str) and value for value in (package_id, action_id, action)):
            raise HTTPException(status_code=409, detail="Stored proposal is invalid")
        key = (package_id, action_id, action)
        if key in proposal_map:
            raise HTTPException(status_code=409, detail="Stored proposals contain duplicates")
        proposal_map[key] = proposal

    seen: set[tuple[str, str, str]] = set()
    executions: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            raise HTTPException(status_code=400, detail="Invalid result entry")

        package_id = result.get("packageId")
        action_id = result.get("actionId")
        action = result.get("action")
        outcome = result.get("outcome")
        if not all(isinstance(value, str) and value for value in (package_id, action_id, action)):
            raise HTTPException(status_code=400, detail="Invalid result entry")
        if outcome not in {"ACCEPTED", "REJECTED"}:
            raise HTTPException(status_code=400, detail="Invalid result outcome")

        key = (package_id, action_id, action)
        if key in seen:
            raise HTTPException(status_code=400, detail="Duplicate result entry")
        seen.add(key)

        proposal = proposal_map.get(key)
        if proposal is None:
            raise HTTPException(
                status_code=409,
                detail="Result does not match a stored proposal",
            )

        receipt_nonce = result.get("receiptNonce")
        if outcome == "ACCEPTED":
            if not isinstance(receipt_nonce, str) or not receipt_nonce:
                raise HTTPException(
                    status_code=400,
                    detail="Accepted result must include a receiptNonce",
                )
            executions.append(
                {
                    "packageId": package_id,
                    "actionId": action_id,
                    "action": action,
                    "receiptNonce": receipt_nonce,
                    "facts": proposal["facts"],
                    "evidenceRefs": proposal["evidenceRefs"],
                }
            )
        elif receipt_nonce is not None and not isinstance(receipt_nonce, str):
            raise HTTPException(status_code=400, detail="receiptNonce must be a string")

    if seen != set(proposal_map):
        raise HTTPException(
            status_code=409,
            detail="Results must cover every stored proposal exactly once",
        )

    return executions, batch_id


async def _handle_results_continuation(
    principal: str,
    message: Message,
    message_data: dict[str, Any],
    results_data: Any,
) -> JSONResponse:
    try:
        replay = get_message_replay(principal, message.messageId, message_data)
    except ValueError as exc:
        if str(exc) == "IDEMPOTENCY_CONFLICT":
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        raise

    if replay is not None:
        replay_task_id, _digest = replay
        task = get_task(replay_task_id, principal)
        if task is None:
            raise HTTPException(status_code=500, detail="Stored task missing")
        return JSONResponse({"task": task})

    if message.taskId is None:
        raise HTTPException(status_code=400, detail="Continuation must include taskId")

    task = get_task(message.taskId, principal)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
        raise HTTPException(status_code=409, detail="Task is not awaiting results")

    executions, batch_id = _validate_results_message(message, results_data, task)

    final_task = {
        **task,
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [
            *task["artifacts"],
            {
                "artifactId": str(uuid.uuid4()),
                "name": "Invoice action receipts",
                "parts": [
                    {
                        "mediaType": RECEIPTS_MEDIA,
                        "data": {
                            "batchId": batch_id,
                            "executions": executions,
                        },
                    }
                ],
            },
        ],
        "history": [*task["history"], message_data],
    }

    try:
        outcome, stored = complete_task_once(
            principal,
            message_data,
            message.taskId,
            final_task,
        )
    except ValueError as exc:
        if str(exc) == "IDEMPOTENCY_CONFLICT":
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        if str(exc) == "TASK_NOT_FOUND":
            raise HTTPException(status_code=404, detail="Task not found")
        if str(exc) == "TASK_STATE_CONFLICT":
            raise HTTPException(status_code=409, detail="Task is already terminal")
        raise

    if outcome == "replay":
        return JSONResponse({"task": stored})
    return JSONResponse({"task": final_task})


@a2a_router.post("/message:send")
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
    result_parts = [
        part for part in message.parts
        if part.media_type == RESULTS_MEDIA and part.data is not None
    ]

    if any(part.media_type == RESULTS_MEDIA for part in message.parts):
        if len(result_parts) != 1:
            raise HTTPException(
                status_code=400,
                detail="Expected one invoice action results part",
            )
        return await _handle_results_continuation(
            principal,
            message,
            message_data,
            result_parts[0].data,
        )

    batch_parts = [
        part for part in message.parts
        if part.media_type == BATCH_MEDIA and part.data is not None
    ]

    if any(part.media_type == BATCH_MEDIA for part in message.parts) and len(batch_parts) != 1:
        raise HTTPException(status_code=400, detail="Expected one invoice batch")

    batch = None
    if batch_parts:
        batch = batch_parts[0].data
        _validate_batch_shape(batch)

    # Check idempotency BEFORE calling the model.
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "contextId": message.contextId or str(uuid.uuid4()),
        "status": {"state": "TASK_STATE_WORKING"},
        "artifacts": [],
        "history": [message_data],
    }

    try:
        outcome, replay = create_task_once(principal, message_data, task)
    except ValueError as exc:
        if str(exc) == "IDEMPOTENCY_CONFLICT":
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        raise

    while outcome == "replay":
        if replay is None:
            raise HTTPException(status_code=500, detail="Stored task missing")

        if replay["status"]["state"] != "TASK_STATE_WORKING":
            return JSONResponse({"task": replay})

        replay = await wait_for_task(replay["id"], principal)
        if replay is not None:
            return JSONResponse({"task": replay})

        # The previous owner may have failed and released its claim.
        try:
            outcome, replay = create_task_once(principal, message_data, task)
        except ValueError as exc:
            if str(exc) == "IDEMPOTENCY_CONFLICT":
                raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
            raise
    if batch is not None:
        try:
            proposals = await decide_batch(batch, use_cache=True)
        except DecisionServiceError as exc:
            # Remove the failed task and its message key, allowing a retry.
            delete_failed_task(principal, message.messageId, task_id)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        task["status"]["state"] = "TASK_STATE_INPUT_REQUIRED"
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
            if current["status"]["state"] != "TASK_STATE_WORKING":
                db.rollback()
                raise HTTPException(status_code=409, detail="Task was canceled")

            db.execute(
                """UPDATE tasks
                   SET state = ?, task_json = ?, updated_at = ?
                   WHERE id = ? AND principal = ?""",
                (
                    "TASK_STATE_INPUT_REQUIRED",
                    json.dumps(task, sort_keys=True, separators=(",", ":")),
                    datetime.now(UTC).isoformat(),
                    task_id,
                    principal,
                ),
            )
            db.commit()

    if batch is None:
        task["status"]["state"] = "TASK_STATE_INPUT_REQUIRED"
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
            if current["status"]["state"] != "TASK_STATE_WORKING":
                db.rollback()
                raise HTTPException(status_code=409, detail="Task was canceled")

            db.execute(
                """UPDATE tasks
                   SET state = ?, task_json = ?, updated_at = ?
                   WHERE id = ? AND principal = ?""",
                (
                    "TASK_STATE_INPUT_REQUIRED",
                    json.dumps(task, sort_keys=True, separators=(",", ":")),
                    datetime.now(UTC).isoformat(),
                    task_id,
                    principal,
                ),
            )
            db.commit()

    return JSONResponse({"task": task})


@a2a_router.get("/tasks/{task_id}")
async def get_task_endpoint(
    task_id: str,
    principal: str = Depends(authenticate),
):
    task = get_task(task_id, principal)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(task)


@a2a_router.get("/tasks")
async def list_tasks(principal: str = Depends(authenticate)):
    with get_connection() as db:
        rows = db.execute(
            """SELECT task_json FROM tasks
               WHERE principal = ? ORDER BY created_at DESC""",
            (principal,),
        ).fetchall()
    return JSONResponse({"tasks": [json.loads(row["task_json"]) for row in rows]})


@a2a_router.post("/tasks/{task_id}:cancel")
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


_public_path = urlparse(get_settings().public_base_url).path.rstrip("/")
app.include_router(a2a_router)
if _public_path:
    app.include_router(a2a_router, prefix=_public_path)
    app.add_api_route(
        f"{_public_path}/.well-known/agent-card.json",
        agent_card,
        methods=["GET"],
        include_in_schema=False,
    )

