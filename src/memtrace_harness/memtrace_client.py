from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


class MemTraceClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemTraceClient:
    mcp_url: str
    token: str | None = None

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid4()),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        result = self._post_json(payload)
        if "error" in result:
            raise MemTraceClientError(str(result["error"]))
        return result.get("result") or {}

    def create_node(
        self,
        *,
        workspace_id: str,
        title: str,
        body: str,
        content_type: str = "inquiry",
        tags: list[str] | None = None,
    ) -> str:
        result = self.call_tool(
            "create_node",
            {
                "workspace_id": workspace_id,
                "title": title,
                "body": body,
                "content_type": content_type,
                "content_format": "markdown",
                "source_type": "ai",
                "visibility": "private",
                "trust_score": 0.64,
                "tags": tags or ["harness", "draft", "human-gate"],
            },
        )
        text = _extract_text(result)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MemTraceClientError(f"create_node returned non-JSON content: {text}") from exc
        node_id = data.get("id")
        if not node_id:
            raise MemTraceClientError(f"create_node response did not include id: {data}")
        return str(node_id)

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.mcp_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MemTraceClientError(f"MemTrace HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise MemTraceClientError(f"MemTrace connection failed: {exc}") from exc

        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise MemTraceClientError(f"MemTrace returned non-JSON response: {response_body}") from exc


def _extract_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        if parts:
            return "\n".join(parts)
    return json.dumps(result)
