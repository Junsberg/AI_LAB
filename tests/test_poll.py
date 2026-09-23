import httpx
import pytest

from memebot.collectors.poll import fetch_new_pools

SAMPLE = {
    "data": [
        {
            "attributes": {"address": "POOL1", "name": "DOGE / SOL", "pool_created_at": "2026-09-23T10:00:00Z"},
            "relationships": {
                "base_token": {"data": {"id": "solana_7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"}},
                "dex": {"data": {"id": "pumpswap"}},
            },
        },
        {   # wrapped SOL as base → skipped
            "attributes": {"address": "POOL2", "name": "SOL / USDC", "pool_created_at": "2026-09-23T10:00:00Z"},
            "relationships": {
                "base_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112"}},
                "dex": {"data": {"id": "raydium"}},
            },
        },
    ]
}


@pytest.mark.asyncio
async def test_fetch_new_pools_parses_and_skips_sol():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        pools = await fetch_new_pools(c, pages=1)
    assert len(pools) == 1
    p = pools[0]
    assert p.mint == "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
    assert p.dex == "pumpswap" and p.symbol == "DOGE" and p.pool == "POOL1"
