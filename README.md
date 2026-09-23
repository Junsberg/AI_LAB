# AI_LAB / memebot

Solana memecoin signal engine — deployer lineage, KOL reverse-map, survivor scan — with
fixed-rule exits and a Claude Code self-improvement loop. See `docs/DESIGN.md`.

## Setup (Windows 10, Python 3.11+)
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env   # fill HELIUS_API_KEY, DATABASE_URL
memebot check
memebot migrate
memebot listen           # smoke test: prints new pump.fun tokens
memebot scrape <public_channel_name>
python -m pytest -q
```
