"""Execution of AI-emitted actions, with permission gating.

The model can *ask* for moderation/admin actions, but the bot only performs
them when the REQUESTING user actually holds the matching Discord permission
(and the bot does too). Instructions in other people's messages can never
trigger an action — only what the requester is entitled to do themselves.
This is the only thing standing between "full server control" and any random
member pinging the bot into doing something destructive — do not weaken it.

Exception: react_message is a soft, non-destructive action available to anyone
(including as spontaneous vibes from the model). The bot still needs Add
Reactions in the channel.
"""
import datetime
import json
import re
import urllib.parse
from typing import List, Optional, Union

import discord

import config

_PERMS = {
    "kick_user": "kick_members",
    "ban_user": "ban_members",
    "assign_role": "manage_roles",
    "remove_role": "manage_roles",
    "create_role": "manage_roles",
    "delete_role": "manage_roles",
    "dm_user": "manage_messages",
    "set_status": "manage_guild",
    "set_server_name": "manage_guild",
    "list_roles": None,
    "timeout_user": "moderate_members",
    "remove_timeout": "moderate_members",
    "set_nickname": "manage_nicknames",
    "purge_messages": "manage_messages",
    "create_channel": "manage_channels",
    "delete_channel": "manage_channels",
    "set_slowmode": "manage_channels",
    "set_channel_topic": "manage_channels",
    "react_message": None,
    "deny_media_perms": "manage_channels",
}

_MAX_REACTS = 5
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):(\d+)>")

_MAX_TIMEOUT_MINUTES = 40320
_MAX_PURGE = 100
_MAX_SLOWMODE = 21600

_STATUS_KINDS = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}


_TYPE_ALIASES = {
    "rename": "set_nickname",
    "rename_user": "set_nickname",
    "change_nickname": "set_nickname",
    "nick": "set_nickname",
    "nickname": "set_nickname",
    "set_nick": "set_nickname",
    "kick": "kick_user",
    "kick_member": "kick_user",
    "ban": "ban_user",
    "ban_member": "ban_user",
    "timeout": "timeout_user",
    "mute": "timeout_user",
    "timeout_member": "timeout_user",
    "unmute": "remove_timeout",
    "untimeout": "remove_timeout",
    "remove_mute": "remove_timeout",
    "assign_role": "assign_role",
    "add_role": "assign_role",
    "give_role": "assign_role",
    "remove_role": "remove_role",
    "take_role": "remove_role",
    "delete_role": "delete_role",
    "create_role": "create_role",
    "purge": "purge_messages",
    "clear": "purge_messages",
    "purge_messages": "purge_messages",
    "delete_messages": "purge_messages",
    "dm": "dm_user",
    "dm_user": "dm_user",
    "send_dm": "dm_user",
    "pm": "dm_user",
    "react": "react_message",
    "react_message": "react_message",
    "add_reaction": "react_message",
}


def _uid(raw) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip().strip("<@!>").strip()
    return int(s) if s.isdigit() else None


def _has(member: discord.Member, perm: Optional[str], channel=None) -> bool:
    """Whether *member* holds *perm*.

    Guild owner and administrator always pass. When *channel* is given, use
    effective channel overwrites (not just guild-level flags) so a denied
    override in #mod-only can't be bypassed via the bot.
    """
    if perm is None:
        return True
    if member is None or not isinstance(member, discord.Member):
        return False
    if member.guild.owner_id == member.id:
        return True
    if channel is not None and hasattr(channel, "permissions_for"):
        perms = channel.permissions_for(member)
    else:
        perms = member.guild_permissions
    if getattr(perms, "administrator", False):
        return True
    return bool(getattr(perms, perm, False))


def _bot_member(guild) -> Optional[discord.Member]:
    if guild is None:
        return None
    return getattr(guild, "me", None)


def _role_above(actor: discord.Member, other: discord.Member) -> bool:
    """True if actor's top role is strictly above other's (owners always win)."""
    if actor is None or other is None:
        return False
    if actor.guild.owner_id == actor.id:
        return True
    if other.guild.owner_id == other.id:
        return False
    return actor.top_role > other.top_role


def _bot_can_act_on(guild, target: discord.Member) -> Optional[str]:
    """Return an error string if the bot cannot moderate *target*, else None."""
    me = _bot_member(guild)
    if me is None:
        return "blocked: I am not a member of this server"
    if target.id == me.id:
        return "blocked: I won't act on myself"
    if target.guild.owner_id == target.id:
        return "blocked: can't moderate the server owner"
    if not _role_above(me, target):
        return (
            f"blocked: my role is not above {target.display_name}'s "
            f"(move my role higher)"
        )
    return None


def _requester_can_act_on(requester: discord.Member, target: discord.Member) -> Optional[str]:
    """Prevent junior mods from using the bot to moderate seniors."""
    if requester.id == target.id:
        return None
    if not _role_above(requester, target):
        return (
            f"denied: your role is not above {target.display_name}'s "
            f"(can't moderate equals or seniors via the bot)"
        )
    return None


def _bot_can_manage_role(guild, role: discord.Role) -> Optional[str]:
    me = _bot_member(guild)
    if me is None:
        return "blocked: I am not a member of this server"
    if not me.guild_permissions.manage_roles and not me.guild_permissions.administrator:
        return "blocked: I need `manage_roles`"
    if role.is_default():
        return "blocked: can't manage @everyone that way"
    if role >= me.top_role:
        return (
            f"blocked: role `{role.name}` is at or above my top role "
            f"(move my role higher)"
        )
    if role.managed:
        return f"blocked: `{role.name}` is managed by an integration"
    return None


def _resolve_role(guild, raw) -> Optional[discord.Role]:
    """Resolve a role by id, mention, or name."""
    if guild is None or raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("<@&") and s.endswith(">"):
        s = s[3:-1]
    if s.isdigit():
        role = guild.get_role(int(s))
        if role is not None:
            return role
    return discord.utils.find(lambda r: r.name.lower() == s.lower(), guild.roles)


async def _resolve_member(guild, raw, requester=None) -> Optional[discord.Member]:
    """Resolve a user id, mention, or name to a Member."""
    if raw is None or guild is None:
        return None
    s_raw = str(raw).strip()
    if s_raw.lower() in ("me", "myself", "self") and requester is not None:
        if isinstance(requester, discord.Member):
            return requester
        m = guild.get_member(getattr(requester, "id", None))
        if m:
            return m
    uid = _uid(raw)
    if uid is not None:
        m = guild.get_member(uid)
        if m:
            return m
        try:
            return await guild.fetch_member(uid)
        except discord.HTTPException:
            pass
    s = s_raw.lstrip("@").lower()
    if not s:
        return None
    for m in guild.members:
        if (
            m.name.lower() == s
            or m.display_name.lower() == s
            or (getattr(m, "global_name", None) and m.global_name.lower() == s)
            or (getattr(m, "nick", None) and m.nick and m.nick.lower() == s)
        ):
            return m
    try:
        members = await guild.query_members(query=s, limit=5)
        if members:
            return members[0]
    except Exception:
        pass
    return None


def _resolve_channel(guild, current_channel, raw_name):
    """Look up a channel by name or id; fall back to the request channel."""
    name = str(raw_name or "").strip()
    if not name:
        return current_channel
    if name.isdigit():
        ch = guild.get_channel(int(name))
        if ch is not None:
            return ch
    if name.startswith("<#") and name.endswith(">"):
        try:
            ch = guild.get_channel(int(name[2:-1]))
            if ch is not None:
                return ch
        except ValueError:
            pass
    name = name.lstrip("#")
    found = discord.utils.find(
        lambda c: c.name.lower() == name.lower(), guild.channels
    )
    return found or current_channel


def _resolve_emoji(guild, raw) -> Optional[Union[str, discord.Emoji, discord.PartialEmoji]]:
    """Turn model output into something message.add_reaction accepts.

    Accepts unicode ('😂'), :name: / name for server custom emoji, raw id,
    or full Discord markup <:name:id> / <a:name:id>.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _CUSTOM_EMOJI_RE.fullmatch(s)
    if m:
        return discord.PartialEmoji(
            name=m.group(1), id=int(m.group(2)), animated=s.startswith("<a:")
        )
    if s.isdigit() and guild is not None:
        e = guild.get_emoji(int(s))
        if e is not None:
            return e
    name = s.strip(":")
    if guild is not None and name:
        e = discord.utils.get(guild.emojis, name=name)
        if e is not None:
            return e
    return s


async def _react_message(a: dict, guild, channel, source_message) -> Optional[str]:
    """Add one or more emoji reactions to a message (default: the trigger msg)."""
    raw_list = []
    if a.get("emojis") is not None:
        if isinstance(a["emojis"], list):
            raw_list.extend(a["emojis"])
        else:
            raw_list.append(a["emojis"])
    if a.get("emoji") is not None:
        if isinstance(a["emoji"], list):
            raw_list.extend(a["emoji"])
        else:
            raw_list.append(a["emoji"])
    if a.get("reactions") is not None:
        if isinstance(a["reactions"], list):
            raw_list.extend(a["reactions"])
        else:
            raw_list.append(a["reactions"])

    if not raw_list:
        return "react: no emoji given"

    target_msg = source_message
    mid = a.get("message_id") or a.get("target_message")
    if mid is not None:
        try:
            mid_i = int(str(mid).strip())
        except (TypeError, ValueError):
            return "react: bad message_id"
        ch = channel
        if a.get("channel") and guild is not None:
            ch = _resolve_channel(guild, channel, a.get("channel")) or channel
        if ch is None or not hasattr(ch, "fetch_message"):
            return "react: no channel to fetch message from"
        try:
            target_msg = await ch.fetch_message(mid_i)
        except discord.HTTPException:
            return f"react: message {mid_i} not found"

    if target_msg is None:
        return "react: no message to react to"

    g = guild or getattr(target_msg, "guild", None)
    me = _bot_member(g) if g is not None else None
    react_ch = getattr(target_msg, "channel", channel)
    if me is not None and react_ch is not None and hasattr(react_ch, "permissions_for"):
        bp = react_ch.permissions_for(me)
        if not (bp.add_reactions or bp.administrator):
            return "react failed: I need `add_reactions` here"
        if not (bp.read_message_history or bp.administrator):
            return "react failed: I need `read_message_history` here"

    added = []
    failed = []
    for raw in raw_list[:_MAX_REACTS]:
        emoji = _resolve_emoji(g, raw)
        if emoji is None:
            failed.append(str(raw))
            continue
        try:
            await target_msg.add_reaction(emoji)
            added.append(str(raw).strip())
        except discord.Forbidden:
            failed.append(f"{raw} (missing permission)")
        except discord.HTTPException as e:
            failed.append(f"{raw} ({e})")
    if not added:
        return f"react failed: {'; '.join(failed) or 'unknown'}"
    if failed:
        return f"reacted {', '.join(added)}; failed {', '.join(failed)}"
    return None


async def execute_all(
    actions, requester, guild, client, channel=None, source_message=None
) -> List[str]:
    """Run each action; return short human-readable result lines for the embed.

    `requester` is the member/user who triggered the exchange; `guild` is the
    server it happened in (None in a DM); `channel` is where the request was
    made (used as the default target for channel-scoped actions).
    `source_message` is the triggering Discord message (for react_message).
    Works from both the message path and the slash-command path.
    """
    rid = getattr(requester, "id", None) if requester is not None else None
    if rid is not None and config.is_blocked(rid):
        return []

    out = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        try:
            line = await _one(a, requester, guild, client, channel, source_message)
        except discord.Forbidden:
            act_name = a.get("type") or a.get("action") or "action"
            line = f"blocked: I lack permission or role position for `{act_name}`"
        except Exception as e:
            act_name = a.get("type") or a.get("action") or "action"
            line = f"failed `{act_name}`: {e}"
        if line:
            out.append(line)
    return out


async def _one(
    a: dict, requester, guild, client, channel=None, source_message=None
) -> Optional[str]:
    raw_type = a.get("type") or a.get("action") or a.get("name")
    t = _TYPE_ALIASES.get(str(raw_type or "").lower(), raw_type)
    if t not in _PERMS:
        return None

    if t == "react_message":
        return await _react_message(a, guild, channel, source_message)

    if guild is None:
        return "actions only work in a server"

    if not isinstance(requester, discord.Member) and hasattr(requester, "id"):
        m = guild.get_member(requester.id)
        if not m:
            try:
                m = await guild.fetch_member(requester.id)
            except discord.HTTPException:
                m = None
        requester = m

    if requester is None or not isinstance(requester, discord.Member):
        return "actions only work in a server"

    reason = str(a.get("reason") or "").strip() or f"requested via SefBot by {requester} ({requester.id})"
    raw_target = (
        a.get("target_user")
        or a.get("user")
        or a.get("target")
        or a.get("member")
        or a.get("user_id")
        or a.get("target_member")
    )
    target = await _resolve_member(guild, raw_target, requester=requester) if raw_target else None
    me = _bot_member(guild)

    _CHANNEL_SCOPED = {
        "purge_messages", "set_slowmode", "set_channel_topic",
        "deny_media_perms", "create_channel", "delete_channel",
    }
    scope_channel = channel
    if t in ("purge_messages", "set_slowmode", "set_channel_topic", "deny_media_perms"):
        scope_channel = _resolve_channel(guild, channel, a.get("channel"))

    perm_needed = _PERMS[t]
    if t == "set_nickname" and target and requester.id == target.id:
        if not (
            _has(requester, "manage_nicknames")
            or _has(requester, "change_nickname")
        ):
            return "denied: you need `change_nickname` to set your own nickname"
    else:
        check_ch = scope_channel if t in _CHANNEL_SCOPED else None
        if not _has(requester, perm_needed, channel=check_ch):
            return f"denied: you need `{perm_needed}` to use `{t}`"

    if t == "list_roles":
        if not target:
            return f"list_roles: target user '{raw_target or ''}' not found"
        names = [r.name for r in target.roles if r.name != "@everyone"]
        return f"{target.display_name} roles: {', '.join(names) or 'none'}"

    if t == "kick_user":
        if not target:
            return f"kick: target user '{raw_target or ''}' not found"
        if target.id == requester.id:
            return "kick: won't kick yourself"
        if me is None or not (me.guild_permissions.kick_members or me.guild_permissions.administrator):
            return "blocked: I need `kick_members`"
        err = _bot_can_act_on(guild, target) or _requester_can_act_on(requester, target)
        if err:
            return err
        await target.kick(reason=reason)
        return f"kicked {target.display_name}"

    if t == "ban_user":
        if not target:
            return f"ban: target user '{raw_target or ''}' not found"
        if target.id == requester.id:
            return "ban: won't ban yourself"
        if me is None or not (me.guild_permissions.ban_members or me.guild_permissions.administrator):
            return "blocked: I need `ban_members`"
        err = _bot_can_act_on(guild, target) or _requester_can_act_on(requester, target)
        if err:
            return err
        await target.ban(reason=reason, delete_message_seconds=0)
        return f"banned {target.display_name}"

    if t in ("assign_role", "remove_role"):
        if not target:
            return f"{t}: target user '{raw_target or ''}' not found"
        role_target = str(a.get("role") or a.get("role_name") or a.get("name") or "").strip()
        role = _resolve_role(guild, role_target)
        if not role:
            return f"{t}: role '{role_target}' not found"
        err = _bot_can_manage_role(guild, role)
        if err:
            return err
        if requester.guild.owner_id != requester.id and role >= requester.top_role:
            return f"denied: role `{role.name}` is at or above your top role"
        if t == "assign_role":
            await target.add_roles(role, reason=reason)
            return f"gave {target.display_name} the {role.name} role"
        await target.remove_roles(role, reason=reason)
        return f"removed {role.name} from {target.display_name}"

    if t == "create_role":
        name = str(a.get("role") or a.get("name") or a.get("role_name") or "").strip()
        if not name:
            return "create_role: no name given"
        if me is None or not (me.guild_permissions.manage_roles or me.guild_permissions.administrator):
            return "blocked: I need `manage_roles`"
        colour = discord.Colour.default()
        hex_colour = str(a.get("color") or "").strip().lstrip("#")
        if hex_colour:
            try:
                colour = discord.Colour(int(hex_colour, 16))
            except ValueError:
                pass
        role = await guild.create_role(
            name=name, colour=colour,
            hoist=bool(a.get("hoist", False)),
            mentionable=bool(a.get("mentionable", False)),
            reason=reason,
        )
        return f"created role {role.name}"

    if t == "delete_role":
        name = str(a.get("role") or a.get("name") or a.get("role_name") or "").strip()
        role = _resolve_role(guild, name)
        if not role:
            return f"delete_role: role '{name}' not found"
        if role.is_default():
            return "delete_role: can't delete @everyone"
        err = _bot_can_manage_role(guild, role)
        if err:
            return err
        if requester.guild.owner_id != requester.id and role >= requester.top_role:
            return f"denied: role `{role.name}` is at or above your top role"
        await role.delete(reason=reason)
        return f"deleted role {role.name}"

    if t == "dm_user":
        if not target:
            return f"dm: target user '{raw_target or ''}' not found"
        import db as _db

        if _db.user_flag_get(str(target.id), "dm_block") == "1":
            return (
                f"dm blocked: {target.display_name} opted out of bot DMs "
                f"(`!dmunblock` to re-enable)"
            )
        raw = (a.get("dm_content") or a.get("message") or a.get("content") or a.get("text") or "(no content)").strip()
        header = (
            f"Message from **{requester.display_name}** "
            f"(@{requester.name}, id `{requester.id}`) via SefBot\n"
            f"_Reply in the server, not here. Opt out: `!dmblock` · status: `!mydm`_\n\n"
        )
        body = header + raw
        if len(body) > 1900:
            body = body[:1900] + "…"
        try:
            await target.send(body)
        except discord.Forbidden:
            return f"dm failed: {target.display_name} has DMs closed or blocked the bot"
        return f"dm'd {target.display_name} (attributed to {requester.display_name})"

    if t == "timeout_user":
        if not target:
            return f"timeout: target user '{raw_target or ''}' not found"
        if target.id == requester.id:
            return "timeout: won't timeout yourself"
        if me is None or not (me.guild_permissions.moderate_members or me.guild_permissions.administrator):
            return "blocked: I need `moderate_members`"
        err = _bot_can_act_on(guild, target) or _requester_can_act_on(requester, target)
        if err:
            return err
        try:
            minutes = max(1, min(_MAX_TIMEOUT_MINUTES, int(a.get("minutes") or a.get("duration") or a.get("time") or 10)))
        except (TypeError, ValueError):
            minutes = 10
        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        await target.timeout(until, reason=reason)
        return f"timed out {target.display_name} for {minutes}m"

    if t == "remove_timeout":
        if not target:
            return f"remove_timeout: target user '{raw_target or ''}' not found"
        if me is None or not (me.guild_permissions.moderate_members or me.guild_permissions.administrator):
            return "blocked: I need `moderate_members`"
        err = _bot_can_act_on(guild, target) or _requester_can_act_on(requester, target)
        if err:
            return err
        await target.timeout(None, reason=reason)
        return f"cleared timeout for {target.display_name}"

    if t == "set_nickname":
        if not target:
            return f"set_nickname: target user '{raw_target or ''}' not found"
        self_rename = requester.id == target.id
        if me is None:
            return "blocked: I am not a member of this server"
        if not (me.guild_permissions.manage_nicknames or me.guild_permissions.administrator):
            return "blocked: I need `manage_nicknames`"
        if not self_rename:
            err = _bot_can_act_on(guild, target) or _requester_can_act_on(requester, target)
            if err:
                return err
        else:
            err = _bot_can_act_on(guild, target)
            if err:
                return err
        nick = str(
            a.get("nickname")
            or a.get("nick")
            or a.get("name")
            or a.get("new_nickname")
            or a.get("new_nick")
            or ""
        ).strip() or None
        if nick and len(nick) > 32:
            nick = nick[:32]
        await target.edit(nick=nick, reason=reason)
        return f"set {target.display_name}'s nickname to {nick or '(reset)'}"

    if t == "purge_messages":
        ch = scope_channel
        if ch is None or not hasattr(ch, "purge"):
            return "purge_messages: no channel to purge"
        if me is not None and hasattr(ch, "permissions_for"):
            bp = ch.permissions_for(me)
            if not (bp.manage_messages or bp.administrator):
                return f"blocked: I need `manage_messages` in #{getattr(ch, 'name', ch.id)}"
        try:
            count = max(1, min(_MAX_PURGE, int(a.get("count") or a.get("amount") or a.get("limit") or a.get("number") or 10)))
        except (TypeError, ValueError):
            count = 10
        purge_target = await _resolve_member(guild, a.get("target_user") or a.get("user")) if (a.get("target_user") or a.get("user")) else None
        check = (lambda m: m.author.id == purge_target.id) if purge_target else None
        deleted = await ch.purge(limit=count, check=check, reason=reason)
        return f"purged {len(deleted)} message(s) in #{getattr(ch, 'name', ch.id)}"

    if t == "create_channel":
        name = str(a.get("name") or "").strip()
        if not name:
            return "create_channel: no name given"
        if me is None or not (me.guild_permissions.manage_channels or me.guild_permissions.administrator):
            return "blocked: I need `manage_channels`"
        kind = str(a.get("channel_type") or "text").lower()
        topic = a.get("topic") or None
        if kind == "voice":
            ch = await guild.create_voice_channel(name, reason=reason)
        else:
            ch = await guild.create_text_channel(name, topic=topic, reason=reason)
        return f"created #{ch.name}"

    if t == "delete_channel":
        name = str(a.get("channel") or a.get("name") or "").strip()
        ch = _resolve_channel(guild, None, name) if name else None
        if ch is None and name:
            ch = discord.utils.find(lambda c: c.name.lower() == name.lstrip("#").lower(), guild.channels)
        if not ch:
            return f"delete_channel: '{name}' not found"
        if me is None or not (me.guild_permissions.manage_channels or me.guild_permissions.administrator):
            return "blocked: I need `manage_channels`"
        ch_name = getattr(ch, "name", str(ch.id))
        await ch.delete(reason=reason)
        return f"deleted #{ch_name}"

    if t == "set_slowmode":
        ch = scope_channel
        if ch is None or not isinstance(ch, discord.TextChannel):
            return "set_slowmode: no text channel to set"
        if me is not None and hasattr(ch, "permissions_for"):
            bp = ch.permissions_for(me)
            if not (bp.manage_channels or bp.administrator):
                return f"blocked: I need `manage_channels` in #{ch.name}"
        try:
            seconds = max(0, min(_MAX_SLOWMODE, int(a.get("seconds") or 0)))
        except (TypeError, ValueError):
            seconds = 0
        await ch.edit(slowmode_delay=seconds, reason=reason)
        return f"slowmode in #{ch.name} set to {seconds}s"

    if t == "set_channel_topic":
        ch = scope_channel
        if ch is None or not isinstance(ch, discord.TextChannel):
            return "set_channel_topic: no text channel to set"
        if me is not None and hasattr(ch, "permissions_for"):
            bp = ch.permissions_for(me)
            if not (bp.manage_channels or bp.administrator):
                return f"blocked: I need `manage_channels` in #{ch.name}"
        topic = str(a.get("topic") or "")[:1024]
        await ch.edit(topic=topic, reason=reason)
        return f"updated #{ch.name}'s topic"

    if t == "set_server_name":
        name = str(a.get("name") or "").strip()
        if not name:
            return "set_server_name: no name given"
        if me is None or not (me.guild_permissions.manage_guild or me.guild_permissions.administrator):
            return "blocked: I need `manage_guild`"
        await guild.edit(name=name, reason=reason)
        return f"renamed server to {name}"

    if t == "set_status":
        kind = _STATUS_KINDS.get(str(a.get("status_kind", "playing")).lower(),
                                 discord.ActivityType.playing)
        text = a.get("status_text", "") or "around"
        await client.change_presence(activity=discord.Activity(type=kind, name=text))
        return f"status set to {a.get('status_kind','playing')} {text}"

    if t == "deny_media_perms":
        if not target:
            return "deny_media_perms: target user not found"
        ch = scope_channel
        if ch is None or not isinstance(
            ch,
            (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel),
        ):
            return "deny_media_perms: no valid channel to modify permissions"
        if me is not None and hasattr(ch, "permissions_for"):
            bp = ch.permissions_for(me)
            if not (bp.manage_roles or bp.manage_channels or bp.administrator):
                return f"blocked: I need `manage_roles`/`manage_channels` in #{ch.name}"
        overwrite = ch.overwrites_for(target)
        overwrite.update(attach_files=False, embed_links=False)
        try:
            await ch.set_permissions(target, overwrite=overwrite, reason=reason)
            return f"denied attach files and embed links for {target.display_name} in #{ch.name}"
        except discord.Forbidden:
            return f"denied: I lack permission to modify channel overrides for #{ch.name}"
        except discord.HTTPException as e:
            return f"failed to set permissions: {e}"

    return None