"""Facts a background duty looks up itself before it asks a model what is still open.

The nightly digest used to list as "needs your decision" things the project's own tools could answer (an order that
had already gone out, a position already held). A rule in the prompt did not stop it, so the Harness now asks first:
a project names read-only tools in its scope file (``- digest_state_queries: list_orders, get_positions``) and the
Harness calls each one and hands the replies to the digest as the live state. Nothing is configured by default, so a
project without the line, or without the tool's key, is unaffected.

Only tools of the read-only proxy that need no argument can be named; anything else is refused when the lookup
runs, never reaching the network. A failed lookup is reported in the text as unknown and carries no id, so the
digest cannot cite it as evidence.
"""

from __future__ import annotations

import os
from typing import Callable, Mapping

from memtrace_harness import taiwantrade_mcp as tt

BLOCK_CHAR_LIMIT = 4_000


def collect_live_state(
    queries: list[str] | None,
    *,
    env: Mapping[str, str] | None = None,
    call: Callable[..., str] = tt.call_tool,
) -> tuple[str, set[str]]:
    """(text for the prompt, ids the digest may cite). ("", set()) when nothing is configured."""
    if not queries:
        return "", set()
    env = os.environ if env is None else env
    base_url = env.get("HARNESS_TAIWANTRADE_API_URL", tt.DEFAULT_BASE_URL)
    blocks: list[str] = []
    ids: set[str] = set()
    for name in dict.fromkeys(queries):
        tool = tt._TOOLS_BY_NAME.get(name)
        if tool is None or any(p.required for p in tool.params):
            blocks.append(f"[live:{name}] (查詢失敗：{name} 不是可免參數查詢的唯讀工具，狀態未知)")
            continue
        try:
            body = call(name, {}, base_url=base_url, api_key=tt.load_api_key(dict(env)))
        except tt.ProxyError as exc:
            blocks.append(f"[live:{name}] (查詢失敗：{exc}；狀態未知)")
            continue
        if len(body) > BLOCK_CHAR_LIMIT:
            body = body[:BLOCK_CHAR_LIMIT] + f"\n...[截斷，共 {len(body)} 字]"
        blocks.append(f"[live:{name}]\n{body}")
        ids.add(f"live:{name}")
    return "\n\n".join(blocks), ids
