from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class MCPNoDataError(RuntimeError):
    """The scoped entity/domain is valid, but the requested evidence does not exist."""


_NO_DATA_MARKERS = (
    "not found",
    "no data",
    "no refund",
    "no rows",
    "does not exist",
    "empty result",
    "no matching",
)


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, Any] | None = None

    async def tool_map(self) -> dict[str, Any]:
        """Discover tools once per MCP session and reuse the descriptors for all cases."""
        if self._tools is None:
            response = await self._session.list_tools()
            self._tools = {tool.name: tool for tool in response.tools}
        return self._tools

    async def list_tools(self) -> list[str]:
        return sorted((await self.tool_map()).keys())

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        last_detail = "unknown error"

        # One retry only. All business-tool calls are audited, so blind retry loops
        # directly reduce the efficiency score. A second attempt is reserved for a
        # genuinely transient/generic MCP failure.
        for attempt in range(2):
            result = await self._session.call_tool(tool_name, arguments=payload)
            is_error = getattr(result, "is_error", getattr(result, "isError", False))
            if not is_error:
                break

            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            structured_error = getattr(result, "structured_content", None)
            if structured_error is None:
                structured_error = getattr(result, "structuredContent", None)

            detail = message or "unknown error"
            if structured_error is not None:
                detail += "; structured error: " + json.dumps(
                    structured_error, ensure_ascii=False, default=str
                )
            last_detail = detail
            lowered = detail.lower()

            if any(marker in lowered for marker in _NO_DATA_MARKERS):
                raise MCPNoDataError(
                    f"MCP tool {tool_name} has no scoped data for case={case_id}: {detail}"
                )

            if attempt == 0 and "error executing tool" in lowered:
                await asyncio.sleep(0.5)
                continue

            raise RuntimeError(
                f"MCP tool {tool_name} failed for case={case_id} "
                f"args={arguments!r}: {detail}"
            )
        else:  # pragma: no cover - loop always breaks or raises
            raise RuntimeError(
                f"MCP tool {tool_name} failed for case={case_id} "
                f"args={arguments!r}: {last_detail}"
            )

        evidence = getattr(result, "structured_content", None)
        if evidence is None:
            evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(20.0, connect=8.0, write=10.0, pool=8.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        try:
            await asyncio.wait_for(session.initialize(), timeout=20)
        except ExceptionGroup as exc:
            details = "; ".join(f"{type(error).__name__}: {error}" for error in exc.exceptions)
            raise RuntimeError(f"MCP connection/initialize failed: {details}") from exc
        except TimeoutError as exc:
            raise RuntimeError("MCP connection/initialize timed out after 20 seconds") from exc
        yield EvidenceGateway(session, contracts)
