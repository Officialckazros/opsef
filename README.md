# SefBot

A Discord bot that starts dumb and gets smarter the more your server uses it. One model call per message returns a structured reply plus whatever it should remember, react to, or moderate — everything it sends comes back as an embed, no emoji.

Built on top of [JayyDoesDev/airo](https://github.com/JayyDoesDev/airo), with a self-improvement layer bolted on.

## How it grows

- **Memory** — it remembers stuff about you specifically and brings it up later. You can also just tell it things with `!teach`.
- **Lessons** — upvotes/downvotes and corrections get distilled into rules that stick around and shape every future reply.
- **Commands** — ask for a new command with `!request` and the AI writes one on the spot.

Stack enough of all three and its "level" climbs from Newborn to Sage.

## What it actually does

- Every reply is one JSON object: `{response, memories, actions, chart, title}`. The bot just renders whatever comes back.
- Memory is per Discord user id, and the model decides on its own what's worth keeping.
- It reads recent channel messages so replies aren't context-blind.
- It can take real actions — kick, ban, assign/remove roles, DM someone, list roles, set status — but only if the person who asked actually has that Discord permission. Instructions buried in someone else's message can't trigger this.
- Can throw together a chart (bar/line/pie/radar) via QuickChart, no API key needed.
- `!vibecheck` gives an unfiltered read on how a channel's doing.
- No emoji, ever, in anything it sends.
- The system prompt is hardened against being told to ignore itself.

## Why it's safe-ish

Community-made commands via `!request` are prompt specs, not code — the AI generates `{name, description, behavior-prompt}` and that's stored as data. There's no code execution path at all. Moderation actions check the real permissions of whoever's asking, so you can't social-engineer the bot into banning people.

---

## Getting it running

1. Make a bot at [discord.com/developers/applications](https://discord.com/developers/applications) → New Application → Bot → Reset Token. Turn on **Message Content Intent** under Privileged Gateway Intents. You don't need the Server Members intent.
2. Invite it: OAuth2 → URL Generator → scope `bot`, permissions Send Messages / Read Message History / Add Reactions, plus Kick/Ban/Manage Roles if you want moderation to work.
3. Get a Groq key at [console.groq.com/keys](https://console.groq.com/keys). Copy `.env.example` to `.env` and drop in `DISCORD_TOKEN` and `GROQ_API_KEY`, then:

```bash
cp .env.example .env
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Commands

`@mention` it or DM it to chat. Beyond that: `!teach <fact>` (mention someone to teach the bot about them), `!memories [@user]`, `!request <idea>`, `!commands`, `!vibecheck`, `!stats`, `!forget <id>`, `!delcmd <name>`, `!reflect`, `!help`.

## Knowledge base

Separate from the memory system — this is a real retrieval store (SQLite FTS5, BM25 ranked, no cap on size, unlike memories which decay). Relevant chunks get pulled into the prompt as facts the bot should treat as ground truth.

`python seed_religion.py` loads the built-in starter corpus. Point it at a folder — `python seed_religion.py ./texts` — and it'll ingest every `.md`/`.txt` file in there too.

In Discord, mods can do `!kb add <topic> | <text>` or attach a file to `!kb add`. Anyone can run `!kb search <query>` or just `!kb` for stats. `SEFBOT_KB_TOPK` controls how many chunks get injected per message (default 6).

## Top.gg / listing notes

Check [TOPGG.md](./TOPGG.md) for the full checklist. Worth knowing up front:

- `!music` / `/music` downloads the track and sends it as an MP3.
- DMs relayed through the bot name the requester and support `!dmblock` / `!dmunblock`.
- `!privacy` covers in-bot data controls.

## Where things live

- `bot.py` — Discord glue: chat, embeds, reaction feedback, commands, the reflection loop
- `brain.py` — system prompt construction, memory retrieval, leveling, reflection
- `actions.py` — permission-gated moderation/status actions and chart URLs
- `embeds.py` — embed builders and the emoji stripper
- `customcmds.py` — AI-generated, prompt-defined community commands
- `db.py` — SQLite persistence with in-place migration
- `kb.py` — the uncapped knowledge base (chunking, FTS5, BM25 retrieval)
- `seed_religion.py` — loads the KB with a starter corpus or a folder of text
- `ai.py` — async Groq wrapper for chat and structured JSON
- `config.py` — env config and the persona

## Deploying on Railway

Ships with `railway.json` set up as a worker running `python bot.py`. Since the brain lives in SQLite, you need a persistent volume — point `SEFBOT_DB` at `/data/sefbot.db` on a mounted volume or everything it's learned gets wiped on redeploy. `db.py` handles migrating an older database in place on startup.
