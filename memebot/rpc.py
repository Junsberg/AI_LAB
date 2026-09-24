"""Single Helius JSON-RPC helper. Raises on JSON-RPC `error` objects, which Helius
returns with HTTP 200 (rate limits, long-term-storage misses) — otherwise they look
exactly like "no history".
"""
from __future__ import annotations

import httpx

from memebot.config import settings


class RpcError(RuntimeError):
    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method}: [{code}] {message}")
        self.code = code


async def rpc(client: httpx.AsyncClient, method: str, params: list) -> dict | list | None:
    r = await client.post(
        settings.helius_rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    r.raise_for_status()
    body = r.json()
    if "error" in body and body["error"]:
        err = body["error"]
        raise RpcError(method, err.get("code"), str(err.get("message", err)))
    return body.get("result")


async def signatures(
    client: httpx.AsyncClient, address: str, max_pages: int
) -> tuple[list[dict], bool]:
    """Newest-first signatures for `address`, up to `max_pages` × 1000.
    Returns (signatures, exhausted). exhausted=False means the cap was hit and the
    oldest entry is NOT the true first transaction."""
    out: list[dict] = []
    before: str | None = None
    for _ in range(max_pages):
        opts: dict = {"limit": 1000}
        if before:
            opts["before"] = before
        page = await rpc(client, "getSignaturesForAddress", [address, opts]) or []
        if not page:
            return out, True
        out.extend(page)
        before = page[-1]["signature"]
        if len(page) < 1000:
            return out, True
    return out, False


async def transaction(client: httpx.AsyncClient, sig: str) -> dict | None:
    return await rpc(
        client,
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
