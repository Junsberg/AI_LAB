"""pump.fun new-token listener over Helius WebSocket `logsSubscribe` (free tier).

Pattern follows chainstacklabs/pumpfun-bonkfun-bot `logs` listener. We only need
mint + deployer + timestamp for lineage; trades are picked up separately.
Swap to Geyser/LaserStream later without touching downstream code.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

import structlog
import websockets

from memebot.config import settings

log = structlog.get_logger()

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_LOG = "Program log: Instruction: Create"


@dataclass(frozen=True)
class NewToken:
    mint: str
    deployer: str
    signature: str
    slot: int
    ts: datetime


async def _fetch_tx(sig: str) -> dict | None:
    import httpx

    from memebot.rpc import transaction

    async with httpx.AsyncClient(timeout=20) as c:
        return await transaction(c, sig)


def _parse_create(tx: dict, sig: str) -> NewToken | None:
    msg = tx["transaction"]["message"]
    keys = [k["pubkey"] for k in msg["accountKeys"]]
    for ix in msg["instructions"]:
        if ix.get("programId") != PUMP_PROGRAM:
            continue
        accts = ix.get("accounts", [])
        # pump.fun Create: accounts[0]=mint, accounts[7]=user (deployer)
        if len(accts) >= 8:
            return NewToken(
                mint=accts[0],
                deployer=accts[7],
                signature=sig,
                slot=tx["slot"],
                ts=datetime.fromtimestamp(tx["blockTime"], tz=timezone.utc),
            )
    _ = keys
    return None


async def listen_new_tokens() -> AsyncIterator[NewToken]:
    sub = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "processed"}],
    }
    while True:
        try:
            async with websockets.connect(settings.helius_ws, ping_interval=20) as ws:
                await ws.send(json.dumps(sub))
                await ws.recv()  # subscription ack
                log.info("pumpfun.listener.connected")
                async for raw in ws:
                    data = json.loads(raw)
                    val = data.get("params", {}).get("result", {}).get("value", {})
                    logs = val.get("logs", [])
                    if val.get("err") or not any(CREATE_LOG in line for line in logs):
                        continue
                    sig = val["signature"]
                    tx = await _fetch_tx(sig)
                    if not tx:
                        continue
                    tok = _parse_create(tx, sig)
                    if tok:
                        yield tok
        except (websockets.ConnectionClosed, OSError) as e:
            log.warning("pumpfun.listener.reconnect", error=str(e))
            await asyncio.sleep(2)
