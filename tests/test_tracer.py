import httpx
import pytest

from memebot.lineage.tracer import KNOWN_CEX, Tracer

CEX = next(iter(KNOWN_CEX))
SYS = "11111111111111111111111111111111"


def rpc_handler(responses):
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        import json

        req = json.loads(body)
        key = req["method"]
        if key == "getSignaturesForAddress":
            addr = req["params"][0]
            return httpx.Response(200, json={"result": responses["sigs"].get(addr, [])})
        if key == "getTransaction":
            sig = req["params"][0]
            return httpx.Response(200, json={"result": responses["txs"].get(sig)})
        return httpx.Response(200, json={"result": None})

    return handler


def transfer_tx(src, dst, lamports, slot=1, bt=1_700_000_000):
    return {
        "slot": slot,
        "blockTime": bt,
        "transaction": {
            "message": {
                "instructions": [
                    {"programId": SYS, "parsed": {"type": "transfer",
                     "info": {"source": src, "destination": dst, "lamports": lamports}}}
                ]
            }
        },
    }


@pytest.mark.asyncio
async def test_walk_stops_at_cex():
    responses = {
        "sigs": {
            "DEPLOYER": [{"signature": "s2"}, {"signature": "s1"}],  # newest first
            "MID": [{"signature": "m1"}],
        },
        "txs": {
            "s1": transfer_tx("MID", "DEPLOYER", 2_000_000_000),
            "s2": transfer_tx("DEPLOYER", "SOMEONE", 1),
            "m1": transfer_tx(CEX, "MID", 50_000_000_000),
        },
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler(responses))) as c:
        edges, hops = await Tracer(c).walk("DEPLOYER")
    assert [(e.src, e.dst) for e in edges] == [("MID", "DEPLOYER"), (CEX, "MID")]
    assert hops[-1].source_type == "cex"
    assert edges[0].amount_sol == 2.0


@pytest.mark.asyncio
async def test_create_account_and_inner_instruction_funding():
    inner_tx = {
        "slot": 1, "blockTime": 1,
        "transaction": {"message": {"instructions": [{"programId": "OTHER"}]}},
        "meta": {"innerInstructions": [{"instructions": [
            {"programId": SYS, "parsed": {"type": "createAccount",
             "info": {"source": "FUNDER", "newAccount": "W", "lamports": 3_000_000_000}}}]}]},
    }
    responses = {"sigs": {"W": [{"signature": "x"}], "FUNDER": []}, "txs": {"x": inner_tx}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler(responses))) as c:
        hop = await Tracer(c).first_inbound("W")
    assert hop.funded_by == "FUNDER" and hop.amount_sol == 3.0


@pytest.mark.asyncio
async def test_hub_wallet_is_not_guessed():
    # 3 full pages → cap hit → source_type 'hub', no funded_by
    full = [{"signature": f"s{i}"} for i in range(1000)]
    responses = {"sigs": {"HUB": full}, "txs": {}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler(responses))) as c:
        hop = await Tracer(c, max_pages=3).first_inbound("HUB")
    assert hop.source_type == "hub" and hop.funded_by is None


@pytest.mark.asyncio
async def test_jsonrpc_error_raises():
    from memebot.rpc import RpcError

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": -32429, "message": "rate limited"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(RpcError):
            await Tracer(c).first_inbound("X")
