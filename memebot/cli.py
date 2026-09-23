from __future__ import annotations

import asyncio

import typer

from memebot.config import load_params, settings

app = typer.Typer(help="memebot control surface")


@app.command()
def check() -> None:
    """Validate params.yaml and env; print mode."""
    p = load_params()
    typer.echo(f"params v{p.version} ok | mode={settings.mode} | helius={'set' if settings.helius_api_key else 'MISSING'} | db={'set' if settings.database_url else 'MISSING'}")


@app.command()
def migrate() -> None:
    from memebot.db import migrate as _migrate

    for name in _migrate():
        typer.echo(f"applied {name}")


@app.command()
def listen() -> None:
    """Stream new pump.fun tokens to stdout (smoke test for Helius key)."""
    from memebot.collectors.pumpfun import listen_new_tokens

    async def run() -> None:
        async for t in listen_new_tokens():
            typer.echo(f"{t.ts.isoformat()} {t.mint} by {t.deployer}")

    asyncio.run(run())


@app.command()
def scrape(channel: str) -> None:
    """Scrape a public Telegram channel (t.me/s/<channel>) for mints."""
    from memebot.signals.calls import scrape_channel

    for c in asyncio.run(scrape_channel(channel)):
        typer.echo(f"{c.called_at.isoformat()} {c.mint} #{c.message_id}")


if __name__ == "__main__":
    app()
