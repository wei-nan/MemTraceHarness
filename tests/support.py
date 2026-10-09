"""Shared test helpers."""

from __future__ import annotations

from typing import Any


def relay(service, update: dict[str, Any]):
    """What the Telegram service does with one update, as the Harness sees it: a message becomes
    `handle_message`, a button tap `handle_action`. The chat id stands in for the opaque audience
    (the Harness only ever hands it back). Returns what the service call returned, so a test can
    keep asserting on it. Messages from chats the test did not intend are the caller's business:
    the allowlist lives in the Telegram service, not here (tests/test_telegram_service.py)."""
    callback = update.get("callback_query")
    if callback:
        source = callback.get("message") or {}
        chat_id = (source.get("chat") or {}).get("id")
        message_id = source.get("message_id")
        return service.handle_action(
            callback.get("data") or "",
            audience=chat_id,
            message_ref=f"tg:{chat_id}:{message_id}" if message_id is not None else None,
        )
    message = update.get("message")
    if not message:
        return None
    chat_id = (message.get("chat") or {}).get("id")
    reply_to = message.get("reply_to_message") or {}
    reply_text = reply_to.get("text") or reply_to.get("caption")
    return service.handle_message(
        message.get("text", ""),
        audience=chat_id,
        reply_to_ref=(
            f"tg:{chat_id}:{reply_to['message_id']}" if reply_to.get("message_id") is not None else None
        ),
        reply_to_text=reply_text.strip() if isinstance(reply_text, str) and reply_text.strip() else None,
    )


def tap(service, callback_query: dict[str, Any]):
    """A button tap, in the shape Telegram delivers it (see relay())."""
    return relay(service, {"callback_query": callback_query})


def outbox(service) -> list[dict]:
    """Every event the Harness has put in its outbox so far (delivered or not), oldest first."""
    store = service.approval_manager.trace_store
    with store._connection() as conn:
        rows = conn.execute(
            f"SELECT {store._OUTBOX_COLUMNS} FROM outbox ORDER BY id"
        ).fetchall()
    return [store._outbox_row_to_dict(r) for r in rows]
