"""Scrape public Telegram channels via t.me/s/<channel> — no account, no API key.

Extracts Solana mint addresses (base58, 32-44 chars) and DexScreener/pump.fun links.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

_B58 = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_LINK_MINT = re.compile(
    r"(?:dexscreener\.com/solana/|pump\.fun/(?:coin/)?|gmgn\.ai/sol/token/|birdeye\.so/token/)"
    r"([1-9A-HJ-NP-Za-km-z]{32,44})"
)
_PUMP_SUFFIX = "pump"


@dataclass(frozen=True)
class ScrapedCall:
    channel: str
    mint: str
    called_at: datetime
    message_id: int
    text: str


def extract_mints(text: str) -> list[str]:
    found: list[str] = []
    for m in _LINK_MINT.findall(text):
        found.append(m)
    for m in _B58.findall(text):
        # heuristic: pump.fun mints end with "pump"; other 43-44 char strings are likely mints too
        if m.endswith(_PUMP_SUFFIX) or len(m) >= 43:
            found.append(m)
    seen: set[str] = set()
    out: list[str] = []
    for m in found:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


async def scrape_channel(channel: str, client: httpx.AsyncClient | None = None) -> list[ScrapedCall]:
    """Fetch the latest ~20 messages of a public channel."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    try:
        r = await client.get(f"https://t.me/s/{channel}")
        r.raise_for_status()
    finally:
        if own:
            await client.aclose()

    soup = BeautifulSoup(r.text, "html.parser")
    calls: list[ScrapedCall] = []
    for msg in soup.select("div.tgme_widget_message"):
        post = msg.get("data-post", "")  # "<channel>/<id>"
        try:
            message_id = int(post.rsplit("/", 1)[-1])
        except ValueError:
            continue
        time_el = msg.select_one("time[datetime]")
        if not time_el:
            continue
        ts = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
        text_el = msg.select_one("div.tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        hrefs = " ".join(a.get("href", "") for a in msg.select("a[href]"))
        for mint in extract_mints(text + " " + hrefs):
            calls.append(ScrapedCall(channel, mint, ts, message_id, text[:500]))
    return calls
