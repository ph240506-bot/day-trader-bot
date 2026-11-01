# Day Trader Bot (free stack)

A paper-trading day-trading bot for US stocks and crypto. Every minute it pulls fresh
5-minute candles from Alpaca, looks for trend-turn setups on the chart, asks a free LLM
(via your own [freellmapi](https://github.com/tashfeenahmed/freellmapi) router) to
approve or veto each setup after reading the latest headlines, then places the trade
with a stop-loss and profit target already attached.

Everything here costs $0: Alpaca paper account, Alpaca market data (IEX feed), free
LLM tiers through freellmapi, and GitHub Actions for hosting.

## What it watches

Your fixed list (`STOCK_SYMBOLS`, default 8 big names) **plus today's top 10 % movers**,
refreshed every 15 minutes from Alpaca's free screener, plus BTC and ETH. Movers are
filtered: no stocks under $5, no warrants/units, no untradable or barely-traded names,
and the bot only shorts a mover if Alpaca says it's easy to borrow.

## How it decides (plain English)

1. **Trend turn.** A fast moving average (last ~9 candles) crosses a slow one (~21 candles).
   Crossing up = possible long, crossing down = possible short (stocks only; crypto is long-only).
2. **Confirmations**, one point each:
   - price is on the right side of **VWAP** (the day's volume-weighted average price, the
     "fair price" big traders watch),
   - **MACD** momentum agrees,
   - **RSI** isn't already stretched (not overbought for a long / oversold for a short),
   - **volume** is at least 1.5x normal.
   It needs 3+ points out of 5 (`MIN_SIGNAL_SCORE`).
3. **LLM check.** The setup, the chart numbers and the last 24h of headlines go to the LLM,
   which can only say approve or veto. If the LLM is down the trade is skipped (`LLM_REQUIRED=true`).
4. **Sizing.** The stop sits 1.5 x ATR away (ATR = how far price typically moves per candle).
   Size is set so hitting the stop loses 0.5% of the account, capped at 10% of the account per position.
   Target = 2x the stop distance.
5. **Guardrails.** Max 4 positions. If the account is down 2% on the day, it closes everything and stops.
   All stock positions are closed 5 minutes before the close, so nothing is held overnight.
6. Every decision (including skipped ones and why) goes into `journal/trades.csv`.

## Setup on GitHub Actions (~15 minutes)

1. **Alpaca:** sign up at alpaca.markets, open the **Paper** account, generate API keys.
2. **Free LLM keys** (get at least two so the router can fail over):
   Groq (console.groq.com), Google Gemini (aistudio.google.com), Cerebras, Mistral, OpenRouter.
3. Create a **public** GitHub repo and push this folder. (Public repos get unlimited Actions
   minutes; a private repo's 2,000 free minutes/month runs out in about a week of trading.
   Your keys stay hidden either way because they live in Secrets, not the code.)
4. Repo → Settings → Secrets and variables → Actions → add:
   `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and any of `GROQ_API_KEY`, `GEMINI_API_KEY`,
   `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`.
5. Actions tab → **day-trader** → *Run workflow* with **dry run** ticked to check it end to end.
   After that it runs itself every weekday.

Each run starts a throwaway freellmapi container, loads your provider keys into it,
grabs its unified key automatically, then runs the bot against it. Results show on the
run's summary page and as a downloadable journal.

To change symbols or risk, edit the `env:` block of the *Run the bot* step
(all options are listed in `.env.example`).

## Always-on alternative (Oracle Cloud Always Free / Raspberry Pi)

GitHub Actions has trade-offs: scheduled runs often start 5-15 minutes late, a job is
capped at 6 hours (so the day is covered by back-to-back runs), crypto only trades while
a run is going, and GitHub's terms are aimed at building and testing software, so using
Actions as a 24/7 bot host is a grey area that could get the workflow disabled.

For a box that just stays on, use Oracle Cloud's Always Free ARM VM (needs a card for
sign-up, not charged) or any spare machine with Docker:

```bash
cp .env.example .env            # fill in Alpaca keys, FREELLMAPI_ENCRYPTION_KEY=$(openssl rand -hex 32)
docker compose up -d freellmapi
ssh -L 3001:localhost:3001 <vm>  # then open http://localhost:3001, add provider keys, copy the unified key
# put the unified key in .env as LLM_API_KEY
docker compose up -d bot
```

## Financial data MCP (for you + Claude)

The free MCP is **Alpaca's official MCP server** (`alpaca-mcp-server`): live and historical
stock/crypto bars, quotes, news and your paper account, using the same free keys.
`mcp/claude_desktop_config.json` sets it up read-only (no order tools) plus a no-key
Yahoo Finance MCP for fundamentals. Use it in Claude Desktop to ask things like
"why did the bot short TSLA at 11:05?" with live data in hand.

The bot itself calls Alpaca's API directly rather than going through the MCP. MCP tools
return text meant for a chat model; parsing that every minute is slower and breaks if the
format changes. Same data source, same free keys, fewer moving parts in the trading loop.

## Run locally

```bash
pip install -r requirements.txt pytest
python -m pytest -q                       # offline tests, no keys needed
DRY_RUN=true python -m bot.main           # needs .env values exported
```

## Things to know before trusting it

- **This is a starting strategy, not a proven edge.** Run it on paper for weeks and read the
  journal before drawing conclusions. Most simple intraday strategies lose money after costs.
- **Pattern day trader rule:** US accounts under US$25k are limited to 3 day trades in 5 business
  days. Alpaca's paper account enforces this too, so set the paper balance realistically.
- **IEX data** is the free real-time feed but covers only a slice of total US volume, so its
  volume numbers are lower than the full market's. Fine for signals, not a perfect picture.
- Live trading is blocked unless you set `ALPACA_PAPER=false` **and**
  `LIVE_TRADING_ACK="I accept I can lose real money"`.
