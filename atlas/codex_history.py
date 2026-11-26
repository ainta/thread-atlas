"""Import user-visible Codex history without resuming threads."""

import asyncio

import aiohttp

from .indexer import codex_message, epoch
from .store import ident


async def pages(rpc, method, params):
    cursor = None
    seen = set()
    while True:
        args = {**params, "cursor": cursor} if cursor else params
        page = await rpc.call(method, args)
        yield page["data"]
        cursor = page.get("nextCursor")
        if not cursor:
            return
        if cursor in seen:
            raise RuntimeError("Codex repeated a history cursor")
        seen.add(cursor)


async def thread_messages(rpc, thread):
    source = thread["id"]
    messages = []
    last_at = epoch(thread.get("createdAt"))

    async def entries():
        try:
            async for page in pages(rpc, "thread/items/list", {
                    "threadId": source, "limit": 50, "sortDirection": "asc"}):
                for entry in page:
                    recorded = entry.get("completedAtMs") or entry.get("startedAtMs")
                    yield entry["item"], entry.get("turnId"), (recorded / 1000 if recorded else 0)
        except RuntimeError as exc:
            if not any(word in str(exc).lower() for word in ("unsupported", "not supported", "unknown method")):
                raise
            async for page in pages(rpc, "thread/turns/list", {
                    "threadId": source, "limit": 10, "sortDirection": "asc", "itemsView": "full"}):
                for turn in page:
                    for item in turn["items"]:
                        recorded = item.get("completedAtMs") or item.get("startedAtMs")
                        at = recorded / 1000 if recorded else epoch(turn.get("startedAt"))
                        yield item, turn["id"], at

    async for item, turn, at in entries():
        message = codex_message(item)
        if message:
            role, body = message
            last_at = max(last_at, at)
            key = item.get("id") or ident(turn, len(messages), role, body)
            messages.append((key, role, body, last_at))
    return messages


def save_history(store, thread, messages):
    with store.tx():
        if store.db.execute("SELECT id FROM sessions WHERE provider='Codex' AND source=?", (thread["id"],)).fetchone():
            return "already_indexed", 0
        title = " ".join((thread.get("name") or thread.get("preview") or "").split())
        sid = store.session("Codex", "local", thread["id"], title=title, cwd=thread.get("cwd") or "")
        count = sum(store.message(sid, key, role, body, at, notify=False) for key, role, body, at in messages)
        return "added", count


async def import_codex_history(store, rpc):
    known = {row[0] for row in store.db.execute("SELECT source FROM sessions WHERE provider='Codex'")}
    result = {"added": 0, "already_indexed": 0, "failed": 0, "messages": 0}

    async def add(thread):
        if thread["id"] in known:
            return "already_indexed", 0
        try:
            messages = await thread_messages(rpc, thread)
            if not messages:
                return "failed", 0
            return await asyncio.to_thread(save_history, store, thread, messages)
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError):
            return "failed", 0

    async for page in pages(rpc, "thread/list", {"limit": 100}):
        threads = [thread for thread in page if not thread.get("ephemeral")]
        for start in range(0, len(threads), 6):
            batch = await asyncio.gather(*(add(thread) for thread in threads[start:start+6]))
            for status, count in batch:
                result[status] += 1
                result["messages"] += count
    return result
