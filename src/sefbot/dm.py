"""Interactive CLI for DMing Discord users through the SefBot bot account.

Full interactive shell (recommended):
    PYTHONPATH=src python -m sefbot.dm

Jump straight into a chat with one user:
    PYTHONPATH=src python -m sefbot.dm <user_id>

Fire a single message and exit, no shell:
    PYTHONPATH=src python -m sefbot.dm <user_id> "message text"
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord

from sefbot import config

INTENTS = discord.Intents.default()
INTENTS.dm_messages = True

_ROOT = Path(__file__).resolve().parent.parent.parent
CONTACTS_FILE = Path(
    os.getenv("SEFBOT_DM_CONTACTS_FILE", str(_ROOT / "dm_contacts.json"))
)

ACTIVE_CHATS_FILE = Path(
    os.getenv("SEFBOT_CLI_ACTIVE_FILE", str(_ROOT / "cli_active_chats.json"))
)
ACTIVE_HEARTBEAT_SECONDS = 20

HELP_TEXT = """\
Commands:
  send <user_id> <message...>   Send a single message, stay in the shell.
  chat <user_id>                 Open a live chat with that user (/back to leave).
  contacts                       List people you've DMed before, most recent first.
  help                           Show this help.
  quit                           Disconnect and exit.
"""


def load_contacts() -> dict:
    if CONTACTS_FILE.exists():
        try:
            return json.loads(CONTACTS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_contacts(contacts: dict) -> None:
    CONTACTS_FILE.write_text(json.dumps(contacts, indent=2))


def _load_active() -> dict:
    if ACTIVE_CHATS_FILE.exists():
        try:
            return json.loads(ACTIVE_CHATS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _mark_active(user_id: int) -> None:
    data = _load_active()
    data[str(user_id)] = time.time()
    ACTIVE_CHATS_FILE.write_text(json.dumps(data))


def _mark_inactive(user_id: int) -> None:
    data = _load_active()
    data.pop(str(user_id), None)
    ACTIVE_CHATS_FILE.write_text(json.dumps(data))


def message_text(msg: discord.Message) -> str:
    """SefBot's own replies go out as embeds, so message.content is often
    empty — fall back to the embed's title/description/fields for those."""
    if msg.content:
        return msg.content
    parts = []
    for e in msg.embeds:
        if e.title:
            parts.append(e.title)
        if e.description:
            parts.append(e.description)
        for f in e.fields:
            if f.name or f.value:
                parts.append(f"{f.name}: {f.value}")
    if parts:
        return " | ".join(p for p in parts if p)
    if msg.attachments:
        return f"[attachment: {', '.join(a.filename for a in msg.attachments)}]"
    return "(empty)"


class DMShell(discord.Client):
    def __init__(self):
        super().__init__(intents=INTENTS)
        self.contacts = load_contacts()
        self.chat_target: Optional[int] = None
        self.ready_event = asyncio.Event()

    async def on_ready(self):
        print(f"Connected as {self.user}.\n")
        self.ready_event.set()

    def _touch_contact(self, user: discord.User) -> None:
        self.contacts[str(user.id)] = {
            "name": str(user),
            "last_message_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_contacts(self.contacts)

    async def on_message(self, message: discord.Message):
        if self.user and message.author.id == self.user.id:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        self._touch_contact(message.author)
        stamp = datetime.now().strftime("%H:%M:%S")
        if self.chat_target == message.author.id:
            print(f"\n[{stamp}] {message.author}: {message_text(message)}")
            print("chat> ", end="", flush=True)
        else:
            print(f"\n[{stamp}] New DM from {message.author} (id {message.author.id}): "
                  f"{message_text(message)}")
            print(f"  -> reply with: chat {message.author.id}")
            print("> ", end="", flush=True)

    async def resolve_user(self, user_id: int) -> Optional[discord.User]:
        try:
            return await self.fetch_user(user_id)
        except discord.NotFound:
            print(f"No Discord user found with id {user_id}.")
        except discord.HTTPException as e:
            print(f"Failed to fetch user {user_id}: {e}")
        return None

    async def send_to(self, user: discord.User, content: str) -> bool:
        try:
            await user.send(content)
            self._touch_contact(user)
            return True
        except discord.Forbidden:
            print("Could not send — this user has DMs closed or has blocked the bot.")
        except discord.HTTPException as e:
            print(f"Send failed: {e}")
        return False

    async def cmd_contacts(self) -> None:
        if not self.contacts:
            print("No contacts yet — send or receive a DM to add one.")
            return
        rows = sorted(
            self.contacts.items(),
            key=lambda kv: kv[1].get("last_message_at", ""),
            reverse=True,
        )
        for uid, info in rows:
            print(f"  {uid}  {info.get('name', '?')}  (last: {info.get('last_message_at', '?')})")

    async def cmd_send(self, user_id: int, content: str) -> None:
        user = await self.resolve_user(user_id)
        if not user:
            return
        if await self.send_to(user, content):
            print(f"Sent to {user}: {content}")

    async def cmd_chat(self, user_id: int) -> None:
        user = await self.resolve_user(user_id)
        if not user:
            return
        channel = user.dm_channel or await user.create_dm()
        print(f"-- Chatting with {user} ({user.id}). /back to return to the menu. --")
        print("Fetching full conversation history...")
        try:
            count = 0
            async for msg in channel.history(limit=None, oldest_first=True):
                who = "you" if msg.author.id == self.user.id else str(msg.author)
                stamp = msg.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                print(f"  [{stamp}] {who}: {message_text(msg)}")
                count += 1
            print(f"-- {count} message(s) total. --")
        except discord.HTTPException as e:
            print(f"Could not fetch history: {e}")

        self.chat_target = user_id
        _mark_active(user_id)
        heartbeat = asyncio.create_task(self._heartbeat(user_id))
        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    line = await loop.run_in_executor(None, lambda: input("chat> "))
                except EOFError:
                    break
                line = line.strip()
                if not line:
                    continue
                if line in ("/back", "/quit", "/exit"):
                    break
                await self.send_to(user, line)
        finally:
            heartbeat.cancel()
            self.chat_target = None
            _mark_inactive(user_id)
        print("-- Left chat. --\n")

    async def _heartbeat(self, user_id: int) -> None:
        """Keep re-marking this user active so bot.py's staleness check
        (see bot.py's _CLI_ACTIVE_TTL) doesn't let the AI take back over
        mid-conversation."""
        try:
            while True:
                await asyncio.sleep(ACTIVE_HEARTBEAT_SECONDS)
                _mark_active(user_id)
        except asyncio.CancelledError:
            pass

    async def shell_loop(self, initial_chat_id: Optional[int] = None) -> None:
        await self.ready_event.wait()
        print(HELP_TEXT)

        if initial_chat_id is not None:
            await self.cmd_chat(initial_chat_id)

        loop = asyncio.get_event_loop()
        while not self.is_closed():
            try:
                line = await loop.run_in_executor(None, lambda: input("> "))
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(HELP_TEXT)
            elif cmd == "contacts":
                await self.cmd_contacts()
            elif cmd == "send":
                if len(parts) < 3:
                    print("Usage: send <user_id> <message>")
                    continue
                try:
                    uid = int(parts[1])
                except ValueError:
                    print("user_id must be numeric.")
                    continue
                await self.cmd_send(uid, parts[2])
            elif cmd == "chat":
                if len(parts) < 2:
                    print("Usage: chat <user_id>")
                    continue
                try:
                    uid = int(parts[1])
                except ValueError:
                    print("user_id must be numeric.")
                    continue
                await self.cmd_chat(uid)
            else:
                print(f"Unknown command '{cmd}'. Type 'help' for the command list.")
        await self.close()


async def run_one_shot(user_id: int, message: str) -> None:
    client = DMShell()

    @client.event
    async def on_ready():
        print(f"Connected as {client.user}.")
        user = await client.resolve_user(user_id)
        if user and await client.send_to(user, message):
            print(f"Sent to {user}: {message}")
        await client.close()

    await client.start(config.DISCORD_TOKEN)


async def run_shell(initial_chat_id: Optional[int] = None) -> None:
    client = DMShell()
    asyncio.create_task(client.shell_loop(initial_chat_id))
    await client.start(config.DISCORD_TOKEN)


def main():
    parser = argparse.ArgumentParser(description="DM Discord users via the SefBot bot account.")
    parser.add_argument("user_id", nargs="?", help="Target Discord user ID")
    parser.add_argument("message", nargs="?", help="Message to send (skips the shell if set)")
    args = parser.parse_args()

    try:
        if args.user_id and args.message:
            uid = int(args.user_id)
            asyncio.run(run_one_shot(uid, args.message))
        elif args.user_id:
            uid = int(args.user_id)
            asyncio.run(run_shell(initial_chat_id=uid))
        else:
            asyncio.run(run_shell())
    except ValueError:
        print(f"'{args.user_id}' is not a valid Discord user ID (must be numeric).")
        sys.exit(1)
    except discord.LoginFailure:
        print("Login failed — check DISCORD_TOKEN in .env.")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
