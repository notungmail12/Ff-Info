import asyncio
import io
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests

try:
    from PIL import Image
except ImportError:  # images are optional
    Image = None
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
# These libraries log full request URLs (which contain the API key / bot token).
for _noisy in ("httpx", "httpcore", "urllib3", "telegram.ext.Application"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WPM_API_KEY = os.getenv("WPM_API_KEY", "").strip()
TARGET_GROUP = os.getenv("TARGET_GROUP", "").strip()  # optional numeric chat ID
API_ENDPOINT = os.getenv(
    "PLAYER_API_URL", "https://wpmtopup.com/api/info_main.php"
).strip()
# Optional: name of a query parameter that carries the region (e.g. "region" or "server").
# Leave empty if the API only needs `info` and `k`.
REGION_PARAM = os.getenv("PLAYER_API_REGION_PARAM", "").strip()
# Your Telegram username, shown as the credit line under every result and in /start.
BOT_OWNER = os.getenv("BOT_OWNER", "@kog_shuvo").strip()

# ---- HL Gaming Official API (https://www.hlgamingofficial.com/p/free-fire-api-data-documentation.html)
# When HL_API_KEY and HL_USER_UID are both set, this provider is used instead of the WPM API.
HL_API_KEY = os.getenv("HL_API_KEY", "").strip()
HL_USER_UID = os.getenv("HL_USER_UID", "").strip()
HL_API_URL = os.getenv(
    "HL_API_URL", "https://proapis.hlgamingofficial.com/main/games/freefire/account/api"
).strip()
USE_HL = bool(HL_API_KEY and HL_USER_UID)
# Send avatar / banner / outfit pictures (HL `filter=isIMG`). Set HL_IMAGES=0 to turn off.
HL_IMAGES = USE_HL and os.getenv("HL_IMAGES", "1").strip() != "0"

# Optional: query-parameter name that gets a unique timestamp on every request, to defeat
# server/CDN caching of repeated UID lookups (e.g. "t" -> ...&t=1761500000123).
CACHE_BUST_PARAM = os.getenv("CACHE_BUST_PARAM", "").strip()
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "5") or 0)

# Only Bangladeshi UIDs are supported, so the region is fixed.
DEFAULT_REGION = "bd"
TIMEOUT_SECONDS = 20

_last_use = {}  # user_id -> monotonic time of last /ffinfo


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def get_nested(data, *keys, default=None):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def pick(d, *keys):
    """First key whose value is not None/'' (so 0 is kept, unlike `a or b`)."""
    if not isinstance(d, dict):
        return None
    for key in keys:
        value = d.get(key)
        if value is not None and value != "":
            return value
    return None


def safe_value(value, fallback="N/A"):
    if value is None or value == "":
        return fallback
    return str(value)


def fmt_time(value):
    """Convert unix timestamps (seconds or ms) to a readable UTC date."""
    if isinstance(value, str) and "T" in value and "-" in value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            return value
    try:
        num = int(str(value))
    except (TypeError, ValueError):
        return safe_value(value)
    if num > 10_000_000_000:  # milliseconds
        num //= 1000
    if num <= 0:
        return "N/A"
    try:
        return datetime.fromtimestamp(num, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return str(value)


def as_dict(value):
    return value if isinstance(value, dict) else {}


PARTNER_KEY_HINTS = ("partner", "duo", "couple", "bond", "relation", "lover", "bff")
PARTNER_NAME_KEYS = ("nickname", "name", "partnerName", "partner_name", "playerName")
PARTNER_TIME_KEYS = (
    "bindTime", "bindAt", "bind_time", "createAt", "createTime", "create_time",
    "startTime", "start_time", "establishTime", "since", "time", "timestamp",
)
PARTNER_DAY_KEYS = ("days", "day", "dayCount", "bindDays", "togetherDays")


def _is_partner_key(key):
    k = str(key).lower()
    return k == "cp" or any(h in k for h in PARTNER_KEY_HINTS)


def _find_partner_node(node, depth=0):
    if depth > 5:
        return None
    if isinstance(node, dict):
        for key, value in node.items():
            if _is_partner_key(key) and value not in (None, "", [], {}):
                if isinstance(value, list):
                    value = next((v for v in value if v), None)
                if value is not None:
                    return value
        for value in node.values():
            found = _find_partner_node(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node[:20]:
            found = _find_partner_node(value, depth + 1)
            if found is not None:
                return found
    return None


def extract_partner(payload):
    """Best-effort: returns (name, days, since_ts). Any may be None."""
    node = _find_partner_node(payload)
    if node is None:
        return None, None, None
    if not isinstance(node, dict):
        return str(node), None, None
    name = pick(node, *PARTNER_NAME_KEYS)
    if name is None:  # name may sit one level deeper (e.g. partner.basicInfo.nickname)
        for sub in node.values():
            name = pick(sub, *PARTNER_NAME_KEYS)
            if name is not None:
                break
    since = pick(node, *PARTNER_TIME_KEYS)
    days = pick(node, *PARTNER_DAY_KEYS)
    try:
        since_num = int(str(since)) if since is not None else None
    except ValueError:
        since_num = None
    if since_num is not None:
        if since_num > 10_000_000_000:
            since_num //= 1000
        if since_num > 0:
            days_calc = (int(time.time()) - since_num) // 86400
            return name, max(days_calc, 0), since_num
        since_num = None
    try:
        days = int(str(days)) if days is not None else None
    except ValueError:
        days = None
    return name, days, since_num


def key_paths(node, prefix="", depth=0):
    """Key names only (no values), for safe debugging in logs."""
    out = []
    if depth > 4:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            out.append(path)
            out.extend(key_paths(v, path, depth + 1))
    elif isinstance(node, list) and node:
        out.extend(key_paths(node[0], prefix + "[]", depth + 1))
    return out


BASIC_KEYS = ("basicInfo", "basic_info", "player", "accountInfo", "userInfo", "profile")


def deep_find_key(node, keys, depth=0):
    """First dict value stored under any of `keys`, searched at any depth."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        for k in keys:
            if isinstance(node.get(k), dict) and node[k]:
                return node[k]
        for v in node.values():
            found = deep_find_key(v, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node[:20]:
            found = deep_find_key(v, keys, depth + 1)
            if found is not None:
                return found
    return None


def deep_find_with(node, key, depth=0):
    """First dict that directly contains `key` with a non-empty value."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        if node.get(key) not in (None, ""):
            return node
        for v in node.values():
            found = deep_find_with(v, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node[:20]:
            found = deep_find_with(v, key, depth + 1)
            if found is not None:
                return found
    return None


def find_player_payload(payload):
    """Return (data, basic). Works whatever the nesting of the API response."""
    if not isinstance(payload, dict):
        payload = {"data": payload} if isinstance(payload, list) else {}
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload.get("result")
    if not isinstance(data, dict):
        data = payload
    basic = deep_find_key(payload, BASIC_KEYS) or deep_find_with(payload, "nickname") or {}
    if not basic and (data.get("name") or data.get("playerName")):
        basic = data
    return as_dict(data), as_dict(basic)


def error_detail(payload):
    """Human-readable message from an API error response (ignores booleans)."""
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "error", "msg", "status", "detail"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _build_legacy_message(payload, uid, region):
    """Returns (message, error_detail). message is None when no player found."""
    data, b = find_player_payload(payload)

    if not b:
        return None, error_detail(payload)

    name = pick(b, "nickname", "name", "playerName")
    if not name:
        return None, error_detail(payload) or "API response did not contain a recognizable player name."

    guild = as_dict(deep_find_key(payload, ("clanBasicInfo", "guild", "clan", "guildInfo")))
    captain = as_dict(deep_find_key(payload, ("captainBasicInfo", "captain", "leader")))
    pet = as_dict(deep_find_key(payload, ("petInfo", "pet")))

    avatar_id = (
        get_nested(data, "cdnUrls", "avatarId")
        or get_nested(data, "profileInfo", "avatarId")
        or pick(b, "headPic", "avatarId")
    )

    p_name, p_days, p_since = extract_partner(payload)
    if p_name is None and p_days is None:
        log.info("No partner info recognized. Response key paths: %s", ", ".join(key_paths(payload))[:1500])
    if p_days is None:
        together = "N/A"
    else:
        together = f"{p_days} days"
        if p_since:
            together += f" (since {fmt_time(p_since)[:10]})"

    lines = [
        "🎮 ━━━━━━━━━━━━━━━ 🎮",
        "🔥 FREE FIRE PLAYER INFO 🔥",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"👤 Name: {safe_value(name)}",
        f"🆔 UID: {safe_value(pick(b, 'accountId', 'uid'), uid)}",
        f"🆙 Level: {safe_value(pick(b, 'level'))} (✨ EXP {safe_value(pick(b, 'exp'))})",
        f"🌎 Region: {safe_value(pick(b, 'region'), region)}",
        f"👍 Likes: {safe_value(pick(b, 'liked', 'likes'))}",
        f"🎖️ Badges: {safe_value(pick(b, 'badgeCnt', 'badges'))}",
        f"🖼️ Avatar ID: {safe_value(avatar_id)}",
        f"📅 Created: {fmt_time(pick(b, 'createAt', 'createdAt'))}",
        f"⌛ Last Login: {fmt_time(pick(b, 'lastLoginAt'))}",
        f"⚙️ Version: {safe_value(pick(b, 'releaseVersion'))}",
        f"💞 Duo Partner: {safe_value(p_name)}",
        f"⏳ Partner Since: {together}",
        "🏆 ━━━━━━━━━━━━━━━ 🏆",
        "⚔️ RANKED STATS ⚔️",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🎖️ BR Rank: {safe_value(pick(b, 'rank'))} (Max {safe_value(pick(b, 'maxRank'))})",
        f"💠 BR Points: {safe_value(pick(b, 'rankingPoints'))}",
        f"🥇 CS Rank: {safe_value(pick(b, 'csRank'))} (Max {safe_value(pick(b, 'csMaxRank'))})",
        f"💎 CS Points: {safe_value(pick(b, 'csRankingPoints'))}",
        f"📅 Season: {safe_value(pick(b, 'seasonId'))}",
        "🛡️ ━━━━━━━━━━━━━━━ 🛡️",
        "🏰 GUILD INFO 🏰",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🏰 Name: {safe_value(pick(guild, 'clanName', 'name'))}",
        f"⭐ Level: {safe_value(pick(guild, 'clanLevel', 'level'))}",
        f"👥 Members: {safe_value(pick(guild, 'memberNum', 'members'))}/{safe_value(pick(guild, 'capacity'))}",
        f"🆔 Clan ID: {safe_value(pick(guild, 'clanId', 'id'))}",
        f"👑 Leader: {safe_value(pick(captain, 'nickname') or pick(guild, 'leaderName'))}",
        f"🆔 Leader UID: {safe_value(pick(captain, 'accountId') or pick(guild, 'leaderUid'))}",
        "🐾 ━━━━━━━━━━━━━━━ 🐾",
        "🐶 PET INFO 🐶",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🐾 Name: {safe_value(pick(pet, 'name'))}",
        f"🆙 Level: {safe_value(pick(pet, 'level'))}",
        f"✨ EXP: {safe_value(pick(pet, 'exp'))}",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines), ""


def _id_text(value):
    """With filter=isIMG, IDs become image URLs: pull the numeric ID out of the file name."""
    if value is None or value == "":
        return "N/A"
    text = str(value)
    if text.startswith(("http://", "https://")):
        found = re.findall(r"\d{6,}", urlparse(text).path)
        return found[-1] if found else "N/A"
    return text


def _section(result, name):
    """Case-insensitive lookup of a section dict inside HL's `result`."""
    if not isinstance(result, dict):
        return {}
    for k, v in result.items():
        if str(k).lower() == name.lower() and isinstance(v, dict):
            return v
    return {}


def _age_label(ts):
    try:
        days = (int(time.time()) - int(ts)) // 86400
    except (TypeError, ValueError):
        return "N/A"
    if days < 0:
        return "N/A"
    years, rem = divmod(days, 365)
    months = rem // 30
    return f"{years} years {months} months ({days} days)"


def build_hl_message(payload, uid, region):
    """Formats the HL Gaming `AllData` response."""
    result = payload.get("result") if isinstance(payload, dict) else None
    info = _section(result, "AccountInfo")
    name = pick(info, "AccountName")
    if not name:
        return None, error_detail(payload) or "API response did not contain a player name."
    guild = _section(result, "GuildInfo")
    cap = _section(result, "captainBasicInfo")
    pet = _section(result, "petInfo")
    credit = _section(result, "creditScoreInfo")
    social = _section(result, "socialinfo")

    usage = as_dict(payload.get("usage"))
    if usage:
        log.info("HL API usage: %s/%s used today", usage.get("usedToday"), usage.get("dailyLimit"))

    bio = " ".join(safe_value(pick(social, "AccountSignature")).replace("\\n", " ").split())[:150]
    cap_exp = pick(cap, "exp")
    cap_level = safe_value(pick(cap, "level")) + (f" (✨ EXP {cap_exp})" if cap_exp is not None else "")

    p_name, p_days, p_since = extract_partner(payload)
    partner_lines = []
    if p_name is not None or p_days is not None:  # shown only if the API actually returns it
        together = "N/A" if p_days is None else f"{p_days} days" + (f" (since {fmt_time(p_since)[:10]})" if p_since else "")
        partner_lines = [f"💞 Duo Partner: {safe_value(p_name)}", f"⏳ Partner Since: {together}"]

    lines = [
        "🎮 ━━━━━━━━━━━━━━━ 🎮",
        "🔥 FREE FIRE PLAYER INFO 🔥",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"👤 Name: {safe_value(name)}",
        f"🆔 UID: {uid}",
        f"🆙 Level: {safe_value(pick(info, 'AccountLevel'))} (✨ EXP {safe_value(pick(info, 'AccountEXP'))})",
        f"🌎 Region: {safe_value(pick(info, 'AccountRegion'), region).upper()}",
        f"👍 Likes: {safe_value(pick(info, 'AccountLikes'))}",
        f"🎖️ BP Badges: {safe_value(pick(info, 'AccountBPBadges'))}",
        f"🛡️ Credit Score: {safe_value(pick(credit, 'creditScore'))}",
        f"🖼️ Avatar ID: {_id_text(pick(info, 'AccountAvatarId'))}",
        f"🚩 Banner ID: {_id_text(pick(info, 'AccountBannerId'))}",
        f"📝 Bio: {bio}",
        f"📅 Created: {fmt_time(pick(info, 'AccountCreateTime'))}",
        f"🕰️ Account Age: {_age_label(pick(info, 'AccountCreateTime'))}",
        f"⌛ Last Login: {fmt_time(pick(info, 'AccountLastLogin'))}",
        f"⚙️ Version: {safe_value(pick(info, 'ReleaseVersion'))}",
        *partner_lines,
        "🏆 ━━━━━━━━━━━━━━━ 🏆",
        "⚔️ RANKED STATS ⚔️",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🎖️ BR Max Rank: {safe_value(pick(info, 'BrMaxRank'))}",
        f"💠 BR Points: {safe_value(pick(info, 'BrRankPoint'))}",
        f"🥇 CS Max Rank: {safe_value(pick(info, 'CsMaxRank'))}",
        f"💎 CS Points: {safe_value(pick(info, 'CsRankPoint'))}",
        f"📅 Season: {safe_value(pick(info, 'AccountSeasonId'))}",
        "🛡️ ━━━━━━━━━━━━━━━ 🛡️",
        "🏰 GUILD INFO 🏰",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🏰 Guild Name: {safe_value(pick(guild, 'GuildName'))}",
        f"🆔 Guild ID: {safe_value(pick(guild, 'Guildid', 'GuildID', 'GuildId'))}",
        f"⭐ Guild Level: {safe_value(pick(guild, 'GuildLevel'))}",
        f"👥 Members: {safe_value(pick(guild, 'GuildMember'))}/{safe_value(pick(guild, 'GuildCapacity'))}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "👑 GUILD OWNER 👑",
        f"👤 Name: {safe_value(pick(cap, 'nickname'))}",
        f"🆔 UID: {safe_value(pick(cap, 'accountId') or pick(guild, 'GuildOwner'))}",
        f"🆙 Level: {cap_level}",
        f"👍 Likes: {safe_value(pick(cap, 'liked'))}",
        f"🎖️ Badges: {safe_value(pick(cap, 'badgeCnt'))}",
        f"🌎 Region: {safe_value(pick(cap, 'region'))}",
        f"🎖️ BR Rank: {safe_value(pick(cap, 'rank'))} (Max {safe_value(pick(cap, 'maxRank'))}) • {safe_value(pick(cap, 'rankingPoints'))} pts",
        f"🥇 CS Rank: {safe_value(pick(cap, 'csRank'))} (Max {safe_value(pick(cap, 'csMaxRank'))}) • {safe_value(pick(cap, 'csRankingPoints'))} pts",
        f"📅 Created: {fmt_time(pick(cap, 'createAt'))}",
        f"⌛ Last Login: {fmt_time(pick(cap, 'lastLoginAt'))}",
        f"⚙️ Version: {safe_value(pick(cap, 'releaseVersion'))}",
        "🐾 ━━━━━━━━━━━━━━━ 🐾",
        "🐶 PET INFO 🐶",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🐾 Pet ID: {safe_value(pick(pet, 'id'))}",
        f"🆙 Level: {safe_value(pick(pet, 'level'))}",
        f"✨ EXP: {safe_value(pick(pet, 'exp'))}",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines), ""


def build_player_message(payload, uid, region):
    """Returns (message, error_detail). Uses the WPM API layout (data.identity / guild_info /
    pet_details); falls back to the generic parser for other layouts."""
    if isinstance(payload, dict) and _section(payload.get("result"), "AccountInfo"):
        return build_hl_message(payload, uid, region)
    identity = as_dict(deep_find_key(payload, ("identity",)))
    if not identity:
        return _build_legacy_message(payload, uid, region)

    # In the real API these blocks sit NEXT TO `identity`, so search the whole payload
    # (and still accept them nested inside `identity`).
    def block(key):
        return as_dict(deep_find_key(payload, (key,)) or identity.get(key))

    profile = block("profile")
    acc = block("account_info")
    rank = block("rank_info")
    eq = block("equipped_items")
    guild = as_dict(deep_find_key(payload, ("guild_info",)))
    pet = as_dict(deep_find_key(payload, ("pet_details",)))
    leader = as_dict(guild.get("leader"))
    members = guild.get("members")
    members = members if isinstance(members, dict) else {}

    name = (
        pick(payload, "player_name")
        or pick(identity, "username", "nickname")
        or pick(as_dict(payload.get("data")), "player_name")
    )
    if not name:
        return None, error_detail(payload) or "API response did not contain a player name."

    def named(d, key):
        v = d.get(key)
        return v.get("name") if isinstance(v, dict) else v

    age = as_dict(profile.get("account_age"))
    bio = safe_value(pick(profile, "bio"))
    bio = " ".join(bio.replace("\\n", " ").split())[:150]

    p_name, p_days, p_since = extract_partner(payload)
    if p_name is None and p_days is None:
        log.info("No partner info in response. Key paths: %s", ", ".join(key_paths(payload))[:1500])
    if p_days is None:
        together = "N/A"
    else:
        together = f"{p_days} days" + (f" (since {fmt_time(p_since)[:10]})" if p_since else "")

    leader_exp = pick(leader, "exp")
    leader_level = safe_value(pick(leader, "level")) + (f" (✨ EXP {leader_exp})" if leader_exp is not None else "")

    lines = [
        "🎮 ━━━━━━━━━━━━━━━ 🎮",
        "🔥 FREE FIRE PLAYER INFO 🔥",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"👤 Name: {safe_value(name)}",
        f"🆔 UID: {safe_value(pick(identity, 'uid'), uid)}",
        f"🆙 Level: {safe_value(pick(profile, 'level'))} (✨ EXP {safe_value(pick(acc, 'exp'))})",
        f"🌎 Region: {safe_value(pick(identity, 'region'), region.upper())}",
        f"👍 Likes: {safe_value(pick(profile, 'likes'))}",
        f"💎 Prime Level: {safe_value(pick(profile, 'prime_level'))}",
        f"🎖️ BP Badges: {safe_value(pick(eq, 'bp_badges'))}",
        f"🏷️ Title: {safe_value(named(acc, 'title'))}",
        f"🛡️ Credit Score: {safe_value(pick(acc, 'credit_score'))}",
        f"🖼️ Avatar: {safe_value(named(eq, 'avatar_id'))}",
        f"🚩 Banner: {safe_value(named(eq, 'banner_id'))}",
        f"📝 Bio: {bio}",
        f"📅 Created: {fmt_time(pick(profile, 'created_at'))}",
        f"🕰️ Account Age: {safe_value(pick(age, 'label'))}",
        f"⌛ Last Login: {fmt_time(pick(profile, 'last_login'))}",
        f"⚙️ Version: {safe_value(pick(acc, 'release_version'))}",
        f"💞 Duo Partner: {safe_value(p_name)}",
        f"⏳ Partner Since: {together}",
        "🏆 ━━━━━━━━━━━━━━━ 🏆",
        "⚔️ RANKED STATS ⚔️",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🎖️ BR Rank: {safe_value(pick(rank, 'br_rank'), 'N/A')} (Max {safe_value(pick(rank, 'br_max_rank'))})",
        f"💠 BR Points: {safe_value(pick(rank, 'br_rank_points'))}",
        f"🥇 CS Rank: {safe_value(pick(rank, 'cs_rank'), 'N/A')} (Max {safe_value(pick(rank, 'cs_max_rank'))})",
        f"💎 CS Points: {safe_value(pick(rank, 'cs_rank_points'))} (Peak {safe_value(pick(rank, 'cs_peak_points'))})",
        f"📅 Season: {safe_value(pick(acc, 'season_id'))}",
        "🛡️ ━━━━━━━━━━━━━━━ 🛡️",
        "🏰 GUILD INFO 🏰",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🏰 Guild Name: {safe_value(pick(guild, 'name'))}",
        f"🆔 Guild ID: {safe_value(pick(guild, 'id'))}",
        f"⭐ Guild Level: {safe_value(pick(guild, 'level'))}",
        f"👥 Members: {safe_value(pick(members, 'current'))}/{safe_value(pick(members, 'capacity') or pick(guild, 'capacity'))}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "👑 GUILD OWNER 👑",
        f"👤 Name: {safe_value(pick(leader, 'name', 'username'))}",
        f"🆔 UID: {safe_value(pick(leader, 'uid') or pick(guild, 'owner_id'))}",
        f"🆙 Level: {leader_level}",
        f"👍 Likes: {safe_value(pick(leader, 'likes'))}",
        f"💎 Prime Level: {safe_value(pick(leader, 'prime_level'))}",
        f"🎖️ Badges: {safe_value(pick(leader, 'badge_count', 'bp_badges'))}",
        f"🏷️ Title: {safe_value(named(leader, 'title'))}",
        f"🌎 Region: {safe_value(pick(leader, 'region'))}",
        f"🎖️ BR Rank: {safe_value(pick(leader, 'br_rank'))} (Max {safe_value(pick(leader, 'br_max_rank'))}) • {safe_value(pick(leader, 'br_points'))} pts",
        f"🥇 CS Rank: {safe_value(pick(leader, 'cs_rank'))} (Max {safe_value(pick(leader, 'cs_max_rank'))}) • {safe_value(pick(leader, 'cs_points'))} pts",
        f"📅 Created: {fmt_time(pick(leader, 'created_at'))}",
        f"⌛ Last Login: {fmt_time(pick(leader, 'last_login'))}",
        f"⚙️ Version: {safe_value(pick(leader, 'release_version'))}",
        "🐾 ━━━━━━━━━━━━━━━ 🐾",
        "🐶 PET INFO 🐶",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🐾 Name: {safe_value(pick(pet, 'name'))}",
        f"🆙 Level: {safe_value(pick(pet, 'level'))}",
        f"✨ EXP: {safe_value(pick(pet, 'exp'))}",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines), ""


# ---- Optional images: profile banner + outfit/character picture ----
# Set these in Render if your API provider offers image endpoints. Placeholders:
# {uid} {region} {key}. Example: https://example.com/banner?uid={uid}&region={region}
BANNER_URL_TEMPLATE = os.getenv("BANNER_URL_TEMPLATE", "").strip()
OUTFIT_URL_TEMPLATE = os.getenv("OUTFIT_URL_TEMPLATE", "").strip()


def download_image(template, uid, region):
    """Blocking: returns image bytes or None. Never logs the URL (it may contain the key)."""
    if not template:
        return None
    url = template.format(uid=uid, region=region, key=WPM_API_KEY)
    try:
        resp = requests.get(url, timeout=TIMEOUT_SECONDS, headers={"User-Agent": "FFINFO-Bot/1.1"})
    except requests.RequestException as exc:
        log.warning("Image download failed: %s", type(exc).__name__)
        return None
    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code != 200 or not ctype.startswith("image/") or not resp.content:
        log.warning("Image endpoint gave HTTP %s, type %s", resp.status_code, ctype[:40])
        return None
    return resp.content


async def send_player_images(message, uid, region):
    banner, outfit = await asyncio.gather(
        asyncio.to_thread(download_image, BANNER_URL_TEMPLATE, uid, region),
        asyncio.to_thread(download_image, OUTFIT_URL_TEMPLATE, uid, region),
    )
    media = [InputMediaPhoto(img) for img in (banner, outfit) if img]
    if not media:
        return
    try:
        if len(media) == 1:
            await message.reply_photo(photo=media[0].media)
        else:
            await message.reply_media_group(media=media)
    except Exception as exc:
        log.warning("Sending images failed: %s", type(exc).__name__)


def _urls(node, depth=0):
    out = []
    if depth > 4:
        return out
    if isinstance(node, str):
        if node.startswith(("http://", "https://")):
            out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out += _urls(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            out += _urls(v, depth + 1)
    return out


def _download(url):
    """Blocking. Returns image bytes or None. Never logs the URL."""
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "FFINFO-Bot/1.1"})
    except requests.RequestException as exc:
        log.warning("Image download failed: %s", type(exc).__name__)
        return None
    if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
        log.warning("Image URL gave HTTP %s / %s", resp.status_code, resp.headers.get("Content-Type", "")[:30])
        return None
    if not resp.content or len(resp.content) > 8_000_000:
        return None
    return resp.content


def make_collage(images, cols=4, cell=256):
    """Grid of item pictures (outfit + weapons) as one PNG. Needs Pillow."""
    if Image is None or not images:
        return None
    tiles = []
    for data in images:
        try:
            tiles.append(Image.open(io.BytesIO(data)).convert("RGBA"))
        except Exception:
            continue
    if not tiles:
        return None
    cols = min(cols, len(tiles))
    rows = -(-len(tiles) // cols)
    canvas = Image.new("RGBA", (cols * cell, rows * cell), (22, 18, 40, 255))
    for i, tile in enumerate(tiles):
        tile.thumbnail((cell - 16, cell - 16))
        x = (i % cols) * cell + (cell - tile.width) // 2
        y = (i // cols) * cell + (cell - tile.height) // 2
        canvas.paste(tile, (x, y), tile)
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def build_hl_photos(payload):
    """Blocking. Returns [(jpeg/png bytes, caption), ...] for avatar, banner, outfit collage."""
    result = payload.get("result") if isinstance(payload, dict) else None
    info = _section(result, "AccountInfo")
    prof = _section(result, "AccountProfileInfo")
    avatar = _urls(info.get("AccountAvatarId"))[:1]
    banner = _urls(info.get("AccountBannerId"))[:1]
    items = (_urls(prof.get("EquippedOutfit")) + _urls(info.get("EquippedWeapon")))[:12]
    urls = avatar + banner + items
    if not urls:
        log.info("isIMG gave no image URLs in the response.")
        return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        data = dict(zip(urls, pool.map(_download, urls)))
    photos = []
    if avatar and data.get(avatar[0]):
        photos.append((data[avatar[0]], "🖼️ Avatar"))
    if banner and data.get(banner[0]):
        photos.append((data[banner[0]], "🚩 Banner"))
    collage = make_collage([data[u] for u in items if data.get(u)])
    if collage:
        photos.append((collage, "👕 Equipped Outfit & Weapons"))
    return photos


async def send_hl_photos(message, payload):
    try:
        photos = await asyncio.to_thread(build_hl_photos, payload)
        if not photos:
            return
        if len(photos) == 1:
            await message.reply_photo(photo=photos[0][0], caption=photos[0][1])
        else:
            await message.reply_media_group(
                media=[InputMediaPhoto(img, caption=cap) for img, cap in photos]
            )
    except Exception as exc:
        log.warning("Sending HL images failed: %s", type(exc).__name__)


def fetch_player(uid, region):
    """Blocking HTTP call — always run via asyncio.to_thread."""
    if USE_HL:
        # HL Gaming: credentials are query parameters, AllData returns every section at once.
        params = {
            "sectionName": "AllData",
            "PlayerUid": uid,
            "region": region,
            "useruid": HL_USER_UID,
            "api": HL_API_KEY,
        }
        if HL_IMAGES:
            params["filter"] = "isIMG"  # numeric item IDs -> temporary image URLs (valid ~300 s)
        url = HL_API_URL
    else:
        params = {"info": uid, "k": WPM_API_KEY}
        if REGION_PARAM:
            params[REGION_PARAM] = region
        url = API_ENDPOINT
    if CACHE_BUST_PARAM:
        params[CACHE_BUST_PARAM] = str(int(time.time() * 1000))
    return requests.get(
        url,
        params=params,
        timeout=TIMEOUT_SECONDS,
        headers={
            "Accept": "application/json",
            "User-Agent": "FFINFO-Bot/1.1",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


# --------------------------------------------------------------------------
# Telegram handlers
# --------------------------------------------------------------------------
# ---- Force-join: users must be members of these channels to use /ffinfo ----
# FORCE_CHANNELS: comma separated, e.g. "@mychannel" or "@a,@b" (or numeric -100... ids).
# FORCE_JOIN_LINKS: optional comma separated join links in the same order (needed for numeric
# ids / private channels). Defaults to https://t.me/<username> for @username channels.
FORCE_CHANNELS = [c.strip() for c in os.getenv("FORCE_CHANNELS", "").split(",") if c.strip()]
FORCE_JOIN_LINKS = [c.strip() for c in os.getenv("FORCE_JOIN_LINKS", "").split(",") if c.strip()]
JOINED_STATUSES = {"member", "administrator", "creator", "owner"}


def channel_link(index):
    if index < len(FORCE_JOIN_LINKS):
        return FORCE_JOIN_LINKS[index]
    ch = FORCE_CHANNELS[index]
    return f"https://t.me/{ch.lstrip('@')}" if not ch.lstrip("-").isdigit() else None


async def not_joined_channels(bot, user_id):
    """Indexes of required channels the user has not joined. Raises on Telegram errors."""
    missing = []
    for i, ch in enumerate(FORCE_CHANNELS):
        target = int(ch) if ch.lstrip("-").isdigit() else ch
        member = await bot.get_chat_member(chat_id=target, user_id=user_id)
        joined = member.status in JOINED_STATUSES or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
        if not joined:
            missing.append(i)
    return missing


def join_keyboard(missing):
    rows = []
    for i in missing:
        link = channel_link(i)
        if link:
            label = f"📢 Join {FORCE_CHANNELS[i]}" if FORCE_CHANNELS[i].startswith("@") else "📢 Join Channel"
            rows.append([InlineKeyboardButton(label, url=link)])
    rows.append([InlineKeyboardButton("✅ আমি জয়েন করেছি", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


JOIN_TEXT = (
    "🔒 <b>প্লেয়ার ইনফো দেখতে হলে আগে আমাদের চ্যানেলে জয়েন করুন।</b>\n\n"
    "👇 নিচের বাটনে চ্যানেলে জয়েন করে <b>✅ আমি জয়েন করেছি</b> চাপুন, তারপর আবার কমান্ড দিন।"
)


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        missing = await not_joined_channels(context.bot, query.from_user.id)
    except Exception as exc:
        log.error("Join check failed: %s", type(exc).__name__)
        await query.answer("⚠️ যাচাই করা যায়নি, একটু পরে আবার চেষ্টা করুন।", show_alert=True)
        return
    if missing:
        await query.answer("❌ আপনি এখনো চ্যানেলে জয়েন করেননি!", show_alert=True)
        return
    await query.answer("✅ ধন্যবাদ!")
    try:
        await query.edit_message_text("✅ জয়েন সম্পন্ন! এখন <code>/ffinfo UID</code> দিয়ে তথ্য দেখুন।", parse_mode="HTML")
    except Exception:
        pass


START_CAPTION = os.getenv(
    "START_CAPTION",
    "🎮 <b>FF INFO BOT</b>\n"
    "⚡ Free Fire প্লেয়ারের তথ্য এক নিমেষে!\n\n"
    "📌 ব্যবহার: <code>/ffinfo UID</code>\n"
    "উদাহরণ: <code>/ffinfo 123456789</code>\n\n"
    "🇧🇩 শুধু বাংলাদেশি (BD) UID সাপোর্ট করে।\n\n"
    f"👨‍💻 Developer: {BOT_OWNER}",
)
# A photo URL (https://...) or a file path inside the repo (default: start.jpg if present).
START_PHOTO = os.getenv("START_PHOTO", "").strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    photo = START_PHOTO
    if not photo:
        for name in ("start.jpg", "start.jpeg", "start.png"):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
            if os.path.isfile(path):
                photo = path
                break
    if photo:
        try:
            if photo.startswith(("http://", "https://")):
                await message.reply_photo(photo=photo, caption=START_CAPTION[:1024], parse_mode="HTML")
            else:
                with open(photo, "rb") as fh:
                    await message.reply_photo(photo=fh, caption=START_CAPTION[:1024], parse_mode="HTML")
            return
        except Exception as exc:
            log.warning("Start photo failed (%s); sending text only.", type(exc).__name__)
    await message.reply_text(START_CAPTION, parse_mode="HTML")


async def ffinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat:
        return

    if TARGET_GROUP:
        try:
            allowed_chat_id = int(TARGET_GROUP)
        except ValueError:
            log.error("TARGET_GROUP must be a numeric Telegram chat ID.")
            await message.reply_text("⚠️ Bot configuration error: TARGET_GROUP is invalid.")
            return
        # Private chats with the bot always work; groups are limited to TARGET_GROUP.
        if chat.type != "private" and chat.id != allowed_chat_id:
            await message.reply_text("⛔ This command is not enabled in this chat.")
            return

    # Force-join gate: the command does nothing until the user has joined every channel.
    if FORCE_CHANNELS:
        if not user:
            return
        try:
            missing = await not_joined_channels(context.bot, user.id)
        except Exception as exc:
            # Usually: bot is not an admin of the channel, or the channel name is wrong.
            log.error("Join check failed (is the bot an admin in the channel?): %s", type(exc).__name__)
            await message.reply_text("⚠️ চ্যানেল যাচাই করা যাচ্ছে না। অ্যাডমিনকে জানান।")
            return
        if missing:
            await message.reply_text(JOIN_TEXT, parse_mode="HTML", reply_markup=join_keyboard(missing))
            return

    if len(context.args) < 1:
        await message.reply_text("💡 Usage: /ffinfo <UID>\nExample: /ffinfo 123456789")
        return

    uid = context.args[0].strip()
    region = DEFAULT_REGION

    if not (uid.isascii() and uid.isdigit()):
        await message.reply_text("❌ Invalid UID. Please enter a numeric ID.")
        return
    if not (USE_HL or WPM_API_KEY):
        await message.reply_text(
            "⚠️ API key is not configured. Set HL_API_KEY + HL_USER_UID (or WPM_API_KEY) in Render Environment."
        )
        return

    # Per-user cooldown so one person cannot burn the API quota.
    if user and COOLDOWN_SECONDS > 0:
        now = time.monotonic()
        wait = COOLDOWN_SECONDS - (now - _last_use.get(user.id, -1e9))
        if wait > 0:
            await message.reply_text(f"⏳ Please wait {int(wait) + 1}s before using this again.")
            return
        _last_use[user.id] = now

    loading = await message.reply_text("🔍 Please wait... Fetching player info ⚡")
    try:
        # requests is blocking: run it in a thread so the bot stays responsive.
        response = await asyncio.to_thread(fetch_player, uid, region)

        if response.status_code != 200:
            log.warning("Player API returned HTTP %s: %s", response.status_code, response.text[:300])
            await loading.edit_text(
                f"⚠️ Player API returned HTTP {response.status_code}. Please try again later."
            )
            return

        try:
            payload = response.json()
        except ValueError:
            log.warning("Player API returned non-JSON response: %s", response.text[:300])
            await loading.edit_text("⚠️ The player API did not return JSON. Check the API endpoint.")
            return

        result, detail = build_player_message(payload, uid, region)
        if not result:
            log.info(
                "Player data not recognized. Response keys: %s",
                list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
            )
            paths = ", ".join(key_paths(payload))
            log.info("Raw API shape (keys only): %s", paths[:1500])
            msg = "❌ Player data was not found in the API response."
            if detail:
                msg += "\nDetails: " + detail[:250]
            else:
                msg += "\nThe UID may not exist, or the API key/plan may not be valid."
            msg += "\n\n🔧 Response keys: " + (paths[:300] or "(empty)")
            await loading.edit_text(msg)
            return

        # Telegram messages have a 4096-character limit.
        # Pictures first (banner + outfit), then the details text.
        if HL_IMAGES and isinstance(payload, dict) and payload.get("result"):
            await send_hl_photos(message, payload)
        elif BANNER_URL_TEMPLATE or OUTFIT_URL_TEMPLATE:
            await send_player_images(message, uid, region)
        credit = f"\n👨‍💻 Developer: {BOT_OWNER}" if BOT_OWNER else ""
        await loading.edit_text(result[:3900] + credit if len(result) <= 4000 else result[:3900] + "\n…" + credit)
    except requests.RequestException as exc:
        # Log only the exception type: str(exc) contains the full URL including the API key.
        log.error("Player API request failed: %s", type(exc).__name__)
        await loading.edit_text("⚠️ Could not reach the player API. Please try again later.")
    except Exception:
        log.exception("Unexpected error while handling /ffinfo")
        try:
            await loading.edit_text("⚠️ Unexpected bot error. Check the Render logs.")
        except Exception:
            pass


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    # Never log the raw exception message for network errors: it can include the bot token URL.
    log.error("Unhandled error: %s", type(context.error).__name__)


# --------------------------------------------------------------------------
# Optional health server (only when Render provides $PORT, i.e. a Web Service)
# --------------------------------------------------------------------------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"FFINFO bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server():
    port = os.getenv("PORT", "").strip()
    if not port.isdigit():
        return
    server = HTTPServer(("0.0.0.0", int(port)), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in Render Environment.")
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ffinfo", ffinfo))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app.add_error_handler(on_error)
    log.info("FFINFO Bot starting with long polling.")
    # drop_pending_updates avoids replaying old commands after a redeploy/restart.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
