import json
from dataclasses import dataclass


@dataclass
class Outcome:
    """Service-layer result: a stable code, HTTP status, detail and optional body."""

    code: str
    status: int
    detail: str
    body: dict

    @classmethod
    def ok(cls, body: dict) -> "Outcome":
        return cls("ok", 200, "", body)

    @classmethod
    def created(cls, body: dict) -> "Outcome":
        return cls("ok", 201, "", body)


def serialize(body: dict | None, status: int, detail: str = "") -> str:
    return json.dumps({"status": status, "detail": detail, "body": body}, ensure_ascii=False)


def deserialize(raw: str) -> tuple[int, str, dict | None]:
    payload = json.loads(raw)
    return payload["status"], payload["detail"], payload["body"]
