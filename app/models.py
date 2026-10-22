from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskState = Literal[
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
]


class Part(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    kind: str | None = None
    media_type: str | None = Field(default=None, alias="mediaType")
    text: str | None = None
    data: dict[str, Any] | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    messageId: str = Field(min_length=1)
    role: Literal["ROLE_USER", "ROLE_AGENT"]
    parts: list[Part] = Field(min_length=1)
    contextId: str | None = None
    taskId: str | None = None


class TaskStatus(BaseModel):
    state: TaskState
    message: Message | None = None


class Artifact(BaseModel):
    artifactId: str
    name: str | None = None
    parts: list[Part]


class Task(BaseModel):
    id: str
    contextId: str
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
