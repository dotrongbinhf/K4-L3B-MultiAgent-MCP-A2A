from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from .contracts import Contracts


class Gateway(Protocol):
    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]: ...

    async def list_tools(self) -> list[str]: ...


def _key(case_id: str, tool_name: str, arguments: dict[str, Any]) -> str:
    return json.dumps([case_id, tool_name, arguments], sort_keys=True, separators=(",", ":"))


def _semantic_not_found(message: str) -> bool:
    """Only explicit missing business records are safe to replay as failures."""
    text = message.lower().replace("_", " ")
    if any(
        token in text
        for token in ("timeout", "timed out", "temporar", "rate limit", "429", "502", "503", "504")
    ):
        return False
    resource = r"(?:order|customer|item|product|shipment|payment|refund|seller|policy)"
    return bool(
        re.search(rf"\b{resource}\b[^\n:]{{0,160}}\b(?:not found|does not exist)\b", text)
        or re.search(rf"\b(?:unknown|nonexistent)\s+{resource}\b", text)
        or re.search(
            rf"\bno\s+{resource}\b[^\n:]{{0,80}}\b(?:found|exists|registered|available)\b",
            text,
        )
    )


class RecordingGateway:
    """Record genuine evidence envelopes and replay exact case-scoped requests.

    Use a path under the repository's ignored ``traces`` directory. Existing JSONL
    records must contain ``case_id``, ``tool_name``, ``arguments`` and either
    ``response`` or ``error``. Transient or unclassified errors are never replayed.
    """

    def __init__(self, gateway: Gateway, path: Path | None, contracts: Contracts) -> None:
        self.gateway = gateway
        self.path = Path(path) if path is not None else None
        self.contracts = contracts
        self._responses: dict[str, dict[str, Any]] = {}
        self._not_found_errors: dict[str, str] = {}
        self._last_errors: dict[str, str] = {}
        self._ref_cases: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        if self.path is not None and self.path.exists():
            for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    self._load_record(record)
                except (ValueError, TypeError, KeyError) as exc:
                    message = f"{self.path}:{number}: invalid evidence cache record"
                    raise ValueError(message) from exc

    def _load_record(self, record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            raise ValueError("expected a JSON object")
        case_id, tool_name, arguments = (
            record["case_id"],
            record["tool_name"],
            record["arguments"],
        )
        if not isinstance(case_id, str) or not isinstance(tool_name, str):
            raise ValueError("case_id and tool_name must be strings")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        key = _key(case_id, tool_name, arguments)
        if "response" in record:
            response = record["response"]
            self._validate_response(case_id, tool_name, response)
            self._responses[key] = deepcopy(response)
            self._not_found_errors.pop(key, None)
            self._last_errors.pop(key, None)
        elif isinstance(record.get("error"), str):
            self._last_errors[key] = record["error"]
            if _semantic_not_found(record["error"]) and key not in self._responses:
                self._not_found_errors[key] = record["error"]
        else:
            raise ValueError("record must contain response or error")

    def _validate_response(self, case_id: str, tool_name: str, response: Any) -> None:
        self.contracts.validate_evidence(response, f"MCP tool {tool_name}")
        ref = response["evidence_ref"]
        previous_case = self._ref_cases.get(ref)
        if previous_case is not None and previous_case != case_id:
            raise ValueError("evidence ref appears in records for different cases")
        self._ref_cases[ref] = case_id

    def _append(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # No await occurs during the append, so case tasks cannot interleave lines.
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    async def list_tools(self) -> list[str]:
        return await self.gateway.list_tools()

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        key = _key(case_id, tool_name, arguments)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._responses:
                return deepcopy(self._responses[key])
            if key in self._not_found_errors:
                raise RuntimeError(self._not_found_errors[key])
            record = {"case_id": case_id, "tool_name": tool_name, "arguments": arguments}
            try:
                response = await self.gateway.call(tool_name, case_id=case_id, **arguments)
            except Exception as exc:
                message = str(exc)
                if self._last_errors.get(key) != message:
                    self._append({**record, "error": message})
                    self._last_errors[key] = message
                if isinstance(exc, RuntimeError) and _semantic_not_found(message):
                    self._not_found_errors[key] = message
                raise
            self._validate_response(case_id, tool_name, response)
            self._append({**record, "response": response})
            self._responses[key] = deepcopy(response)
            self._last_errors.pop(key, None)
            return deepcopy(response)
