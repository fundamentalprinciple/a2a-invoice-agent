import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def message_hash(message: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(message).encode("utf-8")).hexdigest()
