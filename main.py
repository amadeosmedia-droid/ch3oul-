import asyncio
import io
import json
import os
import ctypes.util
import traceback
import re
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

import aiohttp

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from werkzeug.serving import make_server


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

PORT = int(os.getenv("PORT", "10000"))

DATA_FILE = Path(
    os.getenv("DATA_FILE", "bot_data.json")
)

FOREVER_VOICE_CHANNEL_ID = 1524066756514287837
MEMBER_COUNT_CHANNEL_ID = 1544821289506574388

TARGET_USER_ID = int(
    os.getenv("TARGET_USER_ID", "0") or "0"
)

TARGET_EMOJI = os.getenv(
    "TARGET_EMOJI",
    "👀"
)

COLOR_CHANNEL_ID = int(
    os.getenv("COLOR_CHANNEL_ID", "0") or "0"
)

RENAME_REQUEST_CHANNEL_ID = int(
    os.getenv("RENAME_REQUEST_CHANNEL_ID", "0") or "0"
)

SECURITY_LOGS_CHANNEL_ID = int(
    os.getenv("SECURITY_LOGS_CHANNEL_ID", "0") or "0"
)


# ============================================================
# FLASK / RENDER KEEP-ALIVE
# ============================================================

app = Flask(__name__)

# Keep one HTTP server object for the whole process. This is important on
# Render: replacing the Python process with os.execv() would inherit the
# listening socket and can cause "Address already in use". We therefore
# never exec() the process; on a Discord 429 we let Render restart it.
http_server = None
http_server_lock = threading.Lock()
flask_thread = None


@app.route("/")
def home():
    return "Discord bot is alive! ✅", 200


@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot": "running",
    }, 200


def run_flask():
    global http_server

    try:
        server = make_server(
            "0.0.0.0",
            PORT,
            app,
            threaded=True,
        )

        with http_server_lock:
            http_server = server

        print(
            f"[HEALTH] HTTP server listening on 0.0.0.0:{PORT}"
        )

        server.serve_forever()

    except OSError as exc:
        print(
            f"[FLASK FATAL] Could not bind port {PORT}: {exc}"
        )
        # A second server on the same Render instance is never expected.
        # Do not silently continue with a dead health endpoint.
        raise

    except Exception as exc:
        print(
            f"[FLASK FATAL] {repr(exc)}"
        )
        raise

    finally:
        with http_server_lock:
            http_server = None


def keep_alive():
    global flask_thread

    if flask_thread is not None and flask_thread.is_alive():
        print("[HEALTH] HTTP server already running; not starting another one.")
        return flask_thread

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="FlaskThread",
    )
    flask_thread.start()

    # Give the HTTP server a short moment to bind before Discord login.
    # This prevents Render from seeing a dead port during startup.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with http_server_lock:
            if http_server is not None:
                return flask_thread

        if not flask_thread.is_alive():
            raise RuntimeError(
                f"Health server failed to start on port {PORT}."
            )

        time.sleep(0.05)

    raise RuntimeError(
        f"Health server did not bind to port {PORT} within 10 seconds."
    )


# ============================================================
# DEFAULT DATA
# ============================================================

DEFAULT_DATA = {
    "security_punishments": {
        "delete_message": "timeout",
        "timeout": "timeout",
        "ban": "ban",
        "delete_channel": "kick",
        "create_channel": "kick",
        "delete_role": "kick",
        "create_role": "kick",
        "change_server_settings": "ban",
        "kick_member": "kick",
    },

    "punishment_categories": {},

    "security_logs_channel": 0,

    "ticket": {
        "open_category": 0,
        "closed_category": 0,
        "staff_role": 0,
        "logs_channel": 0,
        "panel_message_id": 0,
        "panel_channel_id": 0,
        "counter": 0,

        "reasons": [
            {
                "label": "Buy VIP",
                "description": "Buy VIP role here",
            },
            {
                "label": "Support",
                "description": "General assistance",
            },
        ],

        "tickets": {},
    },

    "automessages": {},

    "warns": {},

    "welcome": {
        "text": None,
        "attachment_url": None,
        "attachment_name": None,
    },

    "blacklist_servers": [],

    "bad_words": {},

    "status": {
        "status": "online",
        "activity_type": "playing",
        "text": "Active",
        "stream_url": None,
    },

    "antinuke": False,
    "antiraid": False,
    "deafen": True,

    "rename_requests": {},
}


# ============================================================
# DATA FUNCTIONS
# ============================================================

def copy_default_data():
    return json.loads(
        json.dumps(DEFAULT_DATA)
    )


def deep_merge(dst, src):
    if not isinstance(src, dict):
        return

    for key, value in src.items():

        if (
            isinstance(value, dict)
            and isinstance(dst.get(key), dict)
        ):
            deep_merge(dst[key], value)

        else:
            dst[key] = value


def load_data():

    if not DATA_FILE.exists():
        return copy_default_data()

    try:

        raw = json.loads(
            DATA_FILE.read_text(
                encoding="utf-8"
            )
        )

        data = copy_default_data()

        if isinstance(raw, dict):
            deep_merge(data, raw)

        return data

    except Exception as exc:

        print(
            f"[DATA] Could not load data: {exc}"
        )

        return copy_default_data()


DATA = load_data()


def save_data():

    try:

        DATA_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_file = DATA_FILE.with_suffix(
            ".tmp"
        )

        temporary_file.write_text(
            json.dumps(
                DATA,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        temporary_file.replace(DATA_FILE)

    except Exception as exc:

        print(
            f"[DATA] Could not save data: {exc}"
        )


# ============================================================
# RUNTIME STATE
# ============================================================

VOICE_LOCKS = set()

TICKET_CREATION_LOCKS = {}

READY_ONCE = False


# ============================================================
# DISCORD INTENTS
# ============================================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.voice_states = True
intents.reactions = True


# ============================================================
# HELPERS
# ============================================================

def has_user_permission(
    interaction: discord.Interaction,
    permission: str,
) -> bool:

    if interaction.guild is None:
        return False

    if not isinstance(
        interaction.user,
        discord.Member,
    ):
        return False

    return bool(
        getattr(
            interaction.user.guild_permissions,
            permission,
            False,
        )
    )


def check_command(
    interaction: discord.Interaction,
    permission: str = "administrator",
):

    if interaction.guild is None:

        return (
            "This command can only be used inside a server."
        )

    if not has_user_permission(
        interaction,
        permission,
    ):

        return (
            f"You need `{permission.replace('_', ' ').title()}` "
            "permission."
        )

    return None


def get_bot_member(
    guild: discord.Guild,
):

    return guild.me


def bot_can_act_on(
    guild: discord.Guild,
    target: discord.Member,
):

    me = get_bot_member(guild)

    if me is None:
        return False, "I cannot resolve my member object."

    if target.id == me.id:
        return False, "I cannot act on myself."

    if target.id == guild.owner_id:
        return False, "I cannot act on the server owner."

    if target.guild_permissions.administrator:
        return (
            False,
            "I will not moderate another administrator.",
        )

    if target.top_role >= me.top_role:

        return (
            False,
            "That member's highest role is equal to "
            "or higher than my highest role.",
        )

    return True, ""


def bot_can_manage_role(
    guild: discord.Guild,
    role: discord.Role,
):

    me = guild.me

    if me is None:
        return False, "I cannot resolve my member object."

    if role.is_default():
        return False, "I cannot manage @everyone."

    if role.managed:
        return False, "I cannot manage an integration role."

    if role >= me.top_role:

        return (
            False,
            "That role is equal to or higher than my highest role.",
        )

    return True, ""


def valid_hex(value: str):

    cleaned = (
        value
        .strip()
        .replace("#", "")
    )

    if not re.fullmatch(
        r"[0-9a-fA-F]{6}",
        cleaned,
    ):

        raise ValueError(
            "HEX must contain exactly 6 characters. "
            "Example: #5865F2"
        )

    return int(cleaned, 16)


def safe_channel_name(name: str):

    name = re.sub(
        r"[^a-zA-Z0-9-]+",
        "-",
        name.lower(),
    )

    name = name.strip("-")

    return (
        name or "ticket"
    )[:90]


async def send_error(
    interaction: discord.Interaction,
    text: str,
):

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                text,
                ephemeral=True,
            )

        else:

            await interaction.response.send_message(
                text,
                ephemeral=True,
            )

    except Exception as exc:

        print(
            f"[INTERACTION ERROR] {exc}"
        )


# ============================================================
# SECURITY LOGGING
# ============================================================

async def security_log(
    guild: discord.Guild,
    title: str,
    description: str,
    color: int = 0xED4245,
):

    channel_id = int(
        DATA.get(
            "security_logs_channel",
            0,
        )
        or SECURITY_LOGS_CHANNEL_ID
    )

    if not channel_id:
        return

    channel = guild.get_channel(
        channel_id
    )

    if not isinstance(
        channel,
        discord.TextChannel,
    ):
        return

    try:

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        await channel.send(
            embed=embed
        )

    except Exception as exc:

        print(
            f"[SECURITY LOG] {exc}"
        )


async def ticket_log(
    guild: discord.Guild,
    title: str,
    description: str,
    color: int = 0x5865F2,
):

    channel_id = int(
        DATA["ticket"].get(
            "logs_channel",
            0,
        )
        or 0
    )

    if not channel_id:
        return

    channel = guild.get_channel(
        channel_id
    )

    if not isinstance(
        channel,
        discord.TextChannel,
    ):
        return

    try:

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        await channel.send(
            embed=embed
        )

    except Exception as exc:

        print(
            f"[TICKET LOG] {exc}"
        )


# ============================================================
# MEMBER COUNT
# ============================================================

async def update_member_count_channel(
    guild: discord.Guild,
):

    saved_counter = DATA.get("member_count", {}).get(str(guild.id), 0)
    channel_id = int(saved_counter or MEMBER_COUNT_CHANNEL_ID)
    channel = guild.get_channel(channel_id)

    if not isinstance(
        channel,
        discord.VoiceChannel,
    ):
        return

    try:

        new_name = (
            f"ᴍᴇᴍʙᴇʀꜱ: {guild.member_count}"
        )

        if channel.name != new_name:

            await channel.edit(
                name=new_name,
                reason="Updating member count.",
            )

    except discord.Forbidden:

        print(
            f"[COUNT] Missing permission in {guild.name}"
        )

    except Exception as exc:

        print(
            f"[COUNT] {exc}"
        )


# ============================================================
# FOREVER VOICE
# ============================================================

async def ensure_forever_voice(
    guild: discord.Guild,
):

    if not DATA.get(
        "deafen",
        True,
    ):
        return

    channel = guild.get_channel(
        FOREVER_VOICE_CHANNEL_ID
    )

    if not isinstance(
        channel,
        discord.VoiceChannel,
    ):
        return

    if guild.id in VOICE_LOCKS:
        return

    VOICE_LOCKS.add(
        guild.id
    )

    try:

        voice = guild.voice_client

        if voice and voice.is_connected():
            return

        await channel.connect(
            reconnect=True,
            self_deaf=True,
        )

        print(
            f"[VOICE] Connected to "
            f"{channel.name} in {guild.name}"
        )

    except discord.ClientException:
        pass

    except discord.Forbidden:

        print(
            f"[VOICE] Missing permission in {guild.name}"
        )

    except Exception as exc:

        print(
            f"[VOICE] {exc}"
        )

    finally:

        VOICE_LOCKS.discard(
            guild.id
        )


# ============================================================
# PRESENCE
# ============================================================

async def apply_saved_presence():

    try:

        settings = DATA.get(
            "status",
            {},
        )

        status_map = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "offline": discord.Status.invisible,
        }

        status = status_map.get(
            settings.get(
                "status",
                "online",
            ),
            discord.Status.online,
        )

        activity_type = settings.get(
            "activity_type",
            "playing",
        )

        text = str(
            settings.get(
                "text",
                "Active",
            )
        )

        activity = None

        if activity_type == "playing":

            activity = discord.Game(
                name=text
            )

        elif activity_type == "streaming":

            url = settings.get(
                "stream_url"
            )

            if not url:
                url = "https://twitch.tv/discord"

            activity = discord.Streaming(
                name=text,
                url=url,
            )

        elif activity_type == "listening":

            activity = discord.Activity(
                type=discord.ActivityType.listening,
                name=text,
            )

        elif activity_type == "watching":

            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=text,
            )

        await bot.change_presence(
            status=status,
            activity=activity,
        )

    except Exception as exc:

        print(
            f"[PRESENCE] {exc}"
        )


# ============================================================
# BAD WORD SYSTEM
# ============================================================

def find_bad_word(
    content: str,
    bad_words: dict,
):

    if not content:
        return None

    for word, reply in bad_words.items():

        word = str(word).strip()

        if not word:
            continue

        pattern = (
            r"(?<!\w)"
            + re.escape(word)
            + r"(?!\w)"
        )

        if re.search(
            pattern,
            content,
            re.IGNORECASE,
        ):

            return (
                word,
                str(reply or ""),
            )

    return None


async def apply_message_punishment(
    message: discord.Message,
):

    if not message.guild:
        return

    member = message.author

    if not isinstance(
        member,
        discord.Member,
    ):
        return

    punishment = DATA.get(
        "security_punishments",
        {},
    ).get(
        "delete_message",
        "timeout",
    )

    can_act, reason = bot_can_act_on(
        message.guild,
        member,
    )

    if not can_act:

        print(
            f"[MODERATION] {reason}"
        )

        return

    try:

        if punishment == "timeout":

            await member.timeout(
                timedelta(minutes=10),
                reason="Bad word filter.",
            )

            await security_log(
                message.guild,
                "🔇 Member Timed Out",
                f"{member.mention} was timed out by the bad-word filter.",
                0xFEE75C,
            )

        elif punishment == "kick":

            await member.kick(
                reason="Bad word filter."
            )

            await security_log(
                message.guild,
                "👢 Member Kicked",
                f"{member.mention} was kicked by the bad-word filter.",
            )

        elif punishment == "ban":

            await member.ban(
                reason="Bad word filter.",
                delete_message_days=0,
            )

            await security_log(
                message.guild,
                "🔨 Member Banned",
                f"{member.mention} was banned by the bad-word filter.",
            )

    except discord.Forbidden:

        print(
            f"[MODERATION] Missing permission for {member}"
        )

    except Exception as exc:

        print(
            f"[MODERATION] {exc}"
        )


# ============================================================
# TICKET HELPERS
# ============================================================

def ticket_record(channel_id: int):

    return DATA["ticket"]["tickets"].get(
        str(channel_id)
    )


def staff_can_manage_ticket(
    interaction: discord.Interaction,
):

    if not isinstance(
        interaction.user,
        discord.Member,
    ):
        return False

    permissions = (
        interaction.user.guild_permissions
    )

    return (
        permissions.manage_channels
        or permissions.administrator
    )


def ticket_is_manager(
    interaction: discord.Interaction,
):

    if not interaction.channel:
        return False

    record = ticket_record(
        interaction.channel.id
    )

    if not record:
        return False

    if (
        interaction.user.id
        == record.get("owner_id")
    ):
        return True

    return staff_can_manage_ticket(
        interaction
    )


def get_ticket_lock(
    guild_id: int,
):

    if guild_id not in TICKET_CREATION_LOCKS:

        TICKET_CREATION_LOCKS[
            guild_id
        ] = asyncio.Lock()

    return TICKET_CREATION_LOCKS[
        guild_id
    ]


# ============================================================
# TICKET CONTROL
# ============================================================

class TicketControlView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ticket:close",
    )
    async def close_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not ticket_is_manager(
            interaction
        ):

            return await send_error(
                interaction,
                "You are not allowed to close this ticket.",
            )

        await close_ticket_channel(
            interaction
        )

    @discord.ui.button(
        label="Reopen",
        style=discord.ButtonStyle.success,
        emoji="🔓",
        custom_id="ticket:reopen",
    )
    async def reopen_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not ticket_is_manager(
            interaction
        ):

            return await send_error(
                interaction,
                "You are not allowed to reopen this ticket.",
            )

        await reopen_ticket_channel(
            interaction
        )

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.secondary,
        emoji="🗑️",
        custom_id="ticket:delete",
    )
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not staff_can_manage_ticket(
            interaction
        ):

            return await send_error(
                interaction,
                "Only ticket staff can delete tickets.",
            )

        await interaction.response.send_message(
            "Deleting this ticket in 3 seconds...",
            ephemeral=True,
        )

        await asyncio.sleep(3)

        channel = interaction.channel

        if channel is None:
            return

        DATA["ticket"]["tickets"].pop(
            str(channel.id),
            None,
        )

        save_data()

        try:

            await channel.delete(
                reason=(
                    f"Ticket deleted by "
                    f"{interaction.user}"
                )
            )

        except Exception as exc:

            print(
                f"[TICKET DELETE] {exc}"
            )


# ============================================================
# TICKET PANEL SELECT
# ============================================================

class TicketPanelSelect(
    discord.ui.Select
):

    def __init__(self):

        reasons = DATA["ticket"].get(
            "reasons",
            [],
        )

        options = []

        for index, reason in enumerate(
            reasons[:25]
        ):

            label = str(
                reason.get(
                    "label",
                    "Support",
                )
            )[:100]

            description = str(
                reason.get(
                    "description",
                    "General support",
                )
            )[:100]

            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(index),
                    emoji="🎫",
                )
            )

        if not options:

            options.append(
                discord.SelectOption(
                    label="Support",
                    description="General support",
                    value="0",
                    emoji="🎫",
                )
            )

        super().__init__(
            placeholder="Select a ticket reason...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket:reason",
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):

        if not interaction.guild:

            return await send_error(
                interaction,
                "This panel can only be used in a server.",
            )

        if not isinstance(
            interaction.user,
            discord.Member,
        ):

            return await send_error(
                interaction,
                "Unable to resolve your member account.",
            )

        guild = interaction.guild

        lock = get_ticket_lock(
            guild.id
        )

        async with lock:

            existing = []

            for record in DATA["ticket"][
                "tickets"
            ].values():

                if (
                    record.get("guild_id")
                    == guild.id
                    and record.get("owner_id")
                    == interaction.user.id
                    and record.get("open", True)
                ):

                    existing.append(
                        record
                    )

            if existing:

                existing_channel = (
                    guild.get_channel(
                        int(
                            existing[0][
                                "channel_id"
                            ]
                        )
                    )
                )

                mention = (
                    existing_channel.mention
                    if existing_channel
                    else "your existing ticket"
                )

                return await send_error(
                    interaction,
                    f"You already have an open ticket: {mention}",
                )

            await interaction.response.defer(
                ephemeral=True
            )

            category_id = int(
                DATA["ticket"].get(
                    "open_category",
                    0,
                )
                or 0
            )

            category = guild.get_channel(
                category_id
            ) if category_id else None

            if not isinstance(
                category,
                discord.CategoryChannel,
            ):
                category = None

            overwrites = {
                guild.default_role:
                    discord.PermissionOverwrite(
                        view_channel=False
                    ),

                interaction.user:
                    discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                    ),
            }

            if guild.me:

                overwrites[guild.me] = (
                    discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_channels=True,
                        manage_messages=True,
                    )
                )

            staff_role_id = int(
                DATA["ticket"].get(
                    "staff_role",
                    0,
                )
                or 0
            )

            staff_role = (
                guild.get_role(
                    staff_role_id
                )
                if staff_role_id
                else None
            )

            if staff_role:

                overwrites[staff_role] = (
                    discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                    )
                )

            DATA["ticket"]["counter"] = (
                int(
                    DATA["ticket"].get(
                        "counter",
                        0,
                    )
                )
                + 1
            )

            number = DATA["ticket"]["counter"]

            channel_name = safe_channel_name(
                f"ticket-{number}-{interaction.user.name}"
            )

            try:

                channel = (
                    await guild.create_text_channel(
                        channel_name,
                        category=category,
                        overwrites=overwrites,
                        reason=(
                            f"Ticket opened by "
                            f"{interaction.user}"
                        ),
                    )
                )

            except discord.Forbidden:

                return await interaction.followup.send(
                    "I need Manage Channels permission to create tickets.",
                    ephemeral=True,
                )

            except Exception as exc:

                print(
                    f"[TICKET CREATE] {exc}"
                )

                return await interaction.followup.send(
                    "Failed to create the ticket.",
                    ephemeral=True,
                )

            try:

                reason_index = int(
                    self.values[0]
                )

                reasons = DATA["ticket"].get(
                    "reasons",
                    [],
                )

                reason = reasons[
                    reason_index
                ]

            except Exception:

                reason = {
                    "label": "Support",
                    "description": "General support",
                }

            DATA["ticket"]["tickets"][
                str(channel.id)
            ] = {
                "channel_id": channel.id,
                "guild_id": guild.id,
                "owner_id": interaction.user.id,
                "reason": reason.get(
                    "label",
                    "Support",
                ),
                "open": True,
                "created_at": (
                    discord.utils.utcnow()
                    .isoformat()
                ),
            }

            save_data()

            embed = discord.Embed(
                title=(
                    f"🎫 Ticket • "
                    f"{reason.get('label', 'Support')}"
                ),
                description=(
                    f"Welcome {interaction.user.mention}!\n\n"
                    f"**Reason:** "
                    f"{reason.get('description', 'General support')}\n\n"
                    "A member of the support team "
                    "will be with you shortly."
                ),
                color=0x5865F2,
            )

            embed.set_footer(
                text=(
                    f"Ticket #{number} • "
                    f"{guild.name}"
                )
            )

            try:

                await channel.send(
                    content=interaction.user.mention,
                    embed=embed,
                    view=TicketControlView(),
                )

                await ticket_log(
                    guild,
                    "🎫 Ticket Opened",
                    (
                        f"{channel.mention} opened by "
                        f"{interaction.user.mention}\n"
                        f"Reason: **{reason.get('label', 'Support')}**"
                    ),
                    0x57F287,
                )

            except Exception as exc:

                print(
                    f"[TICKET MESSAGE] {exc}"
                )

            await interaction.followup.send(
                f"Ticket created: {channel.mention} ✅",
                ephemeral=True,
            )


# ============================================================
# TICKET PANEL
# ============================================================

class TicketPanelView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

        self.add_item(
            TicketPanelSelect()
        )


# ============================================================
# CLOSE TICKET
# ============================================================

async def close_ticket_channel(
    interaction: discord.Interaction,
):

    channel = interaction.channel

    if channel is None:

        return await send_error(
            interaction,
            "Unable to resolve this channel.",
        )

    record = ticket_record(
        channel.id
    )

    if not record:

        return await send_error(
            interaction,
            "This is not a registered ticket.",
        )

    try:

        if not interaction.response.is_done():

            await interaction.response.defer(
                ephemeral=True
            )

    except Exception:
        pass

    guild = interaction.guild

    if guild is None:
        return

    owner = guild.get_member(
        int(record["owner_id"])
    )

    closed_id = int(
        DATA["ticket"].get(
            "closed_category",
            0,
        )
        or 0
    )

    category = (
        guild.get_channel(
            closed_id
        )
        if closed_id
        else None
    )

    if not isinstance(
        category,
        discord.CategoryChannel,
    ):
        category = None

    try:

        if owner:

            await channel.set_permissions(
                owner,
                view_channel=False,
                send_messages=False,
                read_message_history=False,
                reason="Ticket closed.",
            )

        new_name = safe_channel_name(
            f"closed-{channel.name.replace('ticket-', '')}"
        )

        kwargs = {
            "name": new_name,
            "reason": "Ticket closed.",
        }

        if category:
            kwargs["category"] = category

        await channel.edit(
            **kwargs
        )

        record["open"] = False

        record["closed_at"] = (
            discord.utils.utcnow()
            .isoformat()
        )

        save_data()

        await channel.send(
            "🔒 **Ticket closed.** Staff can reopen it below.",
            view=TicketControlView(),
        )

        await ticket_log(
            guild,
            "🔒 Ticket Closed",
            (
                f"{channel.mention} closed by "
                f"{interaction.user.mention}"
            ),
            0xFEE75C,
        )

        await interaction.followup.send(
            "Ticket closed successfully. 🔒",
            ephemeral=True,
        )

    except Exception as exc:

        print(
            f"[TICKET CLOSE] {exc}"
        )

        await send_error(
            interaction,
            "Failed to close the ticket.",
        )


# ============================================================
# REOPEN TICKET
# ============================================================

async def reopen_ticket_channel(
    interaction: discord.Interaction,
):

    channel = interaction.channel

    if channel is None:

        return await send_error(
            interaction,
            "Unable to resolve this channel.",
        )

    record = ticket_record(
        channel.id
    )

    if not record:

        return await send_error(
            interaction,
            "This is not a registered ticket.",
        )

    guild = interaction.guild

    if guild is None:
        return

    owner = guild.get_member(
        int(record["owner_id"])
    )

    open_id = int(
        DATA["ticket"].get(
            "open_category",
            0,
        )
        or 0
    )

    category = (
        guild.get_channel(
            open_id
        )
        if open_id
        else None
    )

    if not isinstance(
        category,
        discord.CategoryChannel,
    ):
        category = None

    try:

        if owner:

            await channel.set_permissions(
                owner,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                reason="Ticket reopened.",
            )

        new_name = safe_channel_name(
            f"ticket-{channel.name.replace('closed-', '')}"
        )

        kwargs = {
            "name": new_name,
            "reason": "Ticket reopened.",
        }

        if category:
            kwargs["category"] = category

        await channel.edit(
            **kwargs
        )

        record["open"] = True

        record["reopened_at"] = (
            discord.utils.utcnow()
            .isoformat()
        )

        save_data()

        await interaction.response.send_message(
            "Ticket reopened. 🔓",
            ephemeral=True,
        )

        await channel.send(
            "🔓 **Ticket reopened.**"
        )

        await ticket_log(
            guild,
            "🔓 Ticket Reopened",
            (
                f"{channel.mention} reopened by "
                f"{interaction.user.mention}"
            ),
            0x57F287,
        )

    except Exception as exc:

        print(
            f"[TICKET REOPEN] {exc}"
        )

        await send_error(
            interaction,
            "Failed to reopen the ticket.",
        )


# ============================================================
# RENAME REQUESTS
# ============================================================

def get_rename_request(
    message_id: int,
):

    return DATA.setdefault(
        "rename_requests",
        {}
    ).get(
        str(message_id)
    )


def save_rename_request(
    message_id: int,
    member_id: int,
    guild_id: int,
    new_nickname: str,
):

    DATA.setdefault(
        "rename_requests",
        {}
    )[str(message_id)] = {
        "message_id": message_id,
        "member_id": member_id,
        "guild_id": guild_id,
        "new_nickname": new_nickname,
        "created_at": (
            discord.utils.utcnow()
            .isoformat()
        ),
        "status": "pending",
    }

    save_data()


class RenameApprovalView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    def request_from_interaction(
        self,
        interaction: discord.Interaction,
    ):

        if not interaction.message:
            return None

        return get_rename_request(
            interaction.message.id
        )

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="rename:accept",
    )
    async def accept_rename(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not isinstance(
            interaction.user,
            discord.Member,
        ):

            return await send_error(
                interaction,
                "Unable to resolve your member account.",
            )

        if not (
            interaction.user.guild_permissions.manage_nicknames
            or interaction.user.guild_permissions.administrator
        ):

            return await send_error(
                interaction,
                "You need Manage Nicknames permission.",
            )

        request = self.request_from_interaction(
            interaction
        )

        if not request:

            return await send_error(
                interaction,
                "This nickname request no longer exists.",
            )

        if request.get("status") != "pending":

            return await send_error(
                interaction,
                "This request has already been processed.",
            )

        guild = interaction.guild

        if guild is None:
            return await send_error(
                interaction,
                "This can only be used inside a server.",
            )

        member = guild.get_member(
            int(request["member_id"])
        )

        if member is None:

            request["status"] = "invalid"
            save_data()

            return await send_error(
                interaction,
                "That member is no longer in the server.",
            )

        me = guild.me

        if me is None:

            return await send_error(
                interaction,
                "I cannot resolve my member object.",
            )

        if member.id == guild.owner_id:

            return await send_error(
                interaction,
                "I cannot change the owner's nickname.",
            )

        if member.id == me.id:

            return await send_error(
                interaction,
                "I cannot change my own nickname this way.",
            )

        if member.top_role >= me.top_role:

            return await send_error(
                interaction,
                "That member's role is too high for me to change their nickname.",
            )

        nickname = str(
            request.get(
                "new_nickname",
                "",
            )
        ).strip()

        if len(nickname) > 32:

            return await send_error(
                interaction,
                "Nickname must be 32 characters or fewer.",
            )

        try:

            await member.edit(
                nick=nickname,
                reason=(
                    f"Nickname request approved by "
                    f"{interaction.user}"
                ),
            )

            request["status"] = "accepted"

            request["processed_by"] = (
                interaction.user.id
            )

            request["processed_at"] = (
                discord.utils.utcnow()
                .isoformat()
            )

            save_data()

            for child in self.children:
                child.disabled = True

            try:

                await interaction.message.edit(
                    view=self
                )

            except Exception:
                pass

            await interaction.response.send_message(
                f"Nickname approved for {member.mention}. ✅",
                ephemeral=True,
            )

            try:

                await member.send(
                    f"Your nickname request in **{guild.name}** was accepted. 🎉"
                )

            except Exception:
                pass

        except discord.Forbidden:

            await send_error(
                interaction,
                "I don't have permission to change that nickname.",
            )

        except Exception as exc:

            print(
                f"[RENAME ACCEPT] {exc}"
            )

            await send_error(
                interaction,
                "Failed to apply the nickname.",
            )

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="rename:reject",
    )
    async def reject_rename(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not isinstance(
            interaction.user,
            discord.Member,
        ):

            return await send_error(
                interaction,
                "Unable to resolve your member account.",
            )

        if not (
            interaction.user.guild_permissions.manage_nicknames
            or interaction.user.guild_permissions.administrator
        ):

            return await send_error(
                interaction,
                "You need Manage Nicknames permission.",
            )

        request = self.request_from_interaction(
            interaction
        )

        if not request:

            return await send_error(
                interaction,
                "This nickname request no longer exists.",
            )

        if request.get("status") != "pending":

            return await send_error(
                interaction,
                "This request has already been processed.",
            )

        request["status"] = "rejected"

        request["processed_by"] = (
            interaction.user.id
        )

        request["processed_at"] = (
            discord.utils.utcnow()
            .isoformat()
        )

        save_data()

        for child in self.children:
            child.disabled = True

        try:

            await interaction.message.edit(
                view=self
            )

        except Exception:
            pass

        await interaction.response.send_message(
            f"Nickname request for <@{request['member_id']}> rejected. ❌",
            ephemeral=True,
        )


# ============================================================
# COLOR ROLE SYSTEM
# ============================================================

COLOR_MAP = {
    "red": 0xFF0000,
    "green": 0x00FF00,
    "blue": 0x0000FF,
    "yellow": 0xFFFF00,
    "cyan": 0x00FFFF,
    "magenta": 0xFF00FF,
    "purple": 0x800080,
    "pink": 0xFFC0CB,
    "orange": 0xFFA500,
    "black": 0x000001,
    "white": 0xFFFFFF,
    "grey": 0x808080,
    "navy": 0x000080,
    "teal": 0x008080,
    "maroon": 0x800000,
    "olive": 0x808000,
    "lime": 0x00FF00,
    "silver": 0xC0C0C0,
    "gold": 0xFFD700,
    "coral": 0xFF7F50,
    "indigo": 0x4B0082,
    "turquoise": 0x40E0D0,
    "crimson": 0xDC143C,
    "chocolate": 0xD2691E,
    "lavender": 0xE6E6FA,
    "salmon": 0xFA8072,
}


async def handle_color_role(
    message: discord.Message,
):

    guild = message.guild

    if guild is None:
        return

    try:

        cleaned = (
            message.content
            .strip()
            .lower()
            .replace("#", "")
        )

        color_int = COLOR_MAP.get(
            cleaned
        )

        if color_int is None:

            color_int = valid_hex(
                cleaned
            )

            role_name = (
                f"#{cleaned.upper()}"
            )

        else:

            role_name = cleaned.capitalize()

        member = message.author
        me = guild.me

        if me is None:
            return

        if not me.guild_permissions.manage_roles:

            print(
                f"[COLOR] Missing Manage Roles in {guild.name}"
            )

            return

        role = discord.utils.get(
            guild.roles,
            name=role_name,
        )

        if role is None:

            role = await guild.create_role(
                name=role_name,
                color=discord.Color(
                    color_int
                ),
                reason="Color role requested.",
            )

        else:

            can_manage, reason = (
                bot_can_manage_role(
                    guild,
                    role,
                )
            )

            if not can_manage:

                print(
                    f"[COLOR] {reason}"
                )

                return

        generated_names = {
            name.capitalize()
            for name in COLOR_MAP
        }

        roles_to_remove = []

        for role_obj in member.roles:

            if role_obj.is_default():
                continue

            if (
                role_obj.name in generated_names
                or role_obj.name.startswith("#")
            ):

                can_manage, _ = (
                    bot_can_manage_role(
                        guild,
                        role_obj,
                    )
                )

                if can_manage:
                    roles_to_remove.append(
                        role_obj
                    )

        if roles_to_remove:

            await member.remove_roles(
                *roles_to_remove,
                reason="Replacing color role.",
            )

        await member.add_roles(
            role,
            reason="Color role system.",
        )

    except ValueError as exc:

        try:

            await message.channel.send(
                f"{message.author.mention}, {exc}",
                delete_after=5,
            )

        except Exception:
            pass

    except discord.Forbidden:

        print(
            f"[COLOR] Missing permission in {guild.name}"
        )

    except Exception as exc:

        print(
            f"[COLOR] {exc}"
        )


# ============================================================
# FILE SYSTEM
# ============================================================

async def collect_files(
    *attachments,
):

    files = []

    for attachment in attachments:

        if attachment is None:
            continue

        try:

            files.append(
                await attachment.to_file()
            )

        except Exception as exc:

            print(
                f"[FILES] {exc}"
            )

            raise

    return files


# ============================================================
# BOT
# ============================================================

class ProBot(
    commands.Bot
):

    async def setup_hook(self):

        # Persistent buttons
        self.add_view(
            TicketControlView()
        )

        self.add_view(
            RenameApprovalView()
        )

        # Ticket panel select is built from saved
        # configuration. Only register it when
        # the bot starts successfully.
        try:

            self.add_view(
                TicketPanelView()
            )

        except Exception as exc:

            print(
                f"[TICKET PANEL] Could not register: {exc}"
            )

        print(
            "[BOT] Persistent views registered."
        )

        # Sync slash commands here.
        try:

            synced = await self.tree.sync()

            print(
                f"[BOT] Synced {len(synced)} slash commands."
            )

        except Exception as exc:

            print(
                f"[SYNC] {exc}"
            )


bot = ProBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():

    global READY_ONCE

    if bot.user is None:
        return

    print(
        "=================================================="
    )

    print(
        f"[BOT] Logged in as {bot.user} "
        f"(ID: {bot.user.id})"
    )

    print(
        f"[BOT] Connected to {len(bot.guilds)} server(s)."
    )

    print(
        "=================================================="
    )

    if not READY_ONCE:

        READY_ONCE = True

        await apply_saved_presence()

    for guild in bot.guilds:

        try:

            await update_member_count_channel(
                guild
            )

        except Exception as exc:

            print(
                f"[READY COUNT] {exc}"
            )

        if DATA.get(
            "deafen",
            True,
        ):

            try:

                await ensure_forever_voice(
                    guild
                )

            except Exception as exc:

                print(
                    f"[READY VOICE] {exc}"
                )


# ============================================================
# VOICE RECONNECT
# ============================================================

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):

    if bot.user is None:
        return

    if member.id != bot.user.id:
        return

    if before.channel and not after.channel:

        await asyncio.sleep(3)

        if DATA.get(
            "deafen",
            True,
        ):

            await ensure_forever_voice(
                member.guild
            )


# ============================================================
# MEMBER JOIN
# ============================================================

@bot.event
async def on_member_join(
    member: discord.Member,
):

    blacklist = DATA.get(
        "blacklist_servers",
        [],
    )

    if member.guild.id in blacklist:

        try:

            await member.ban(
                reason="Server is blacklisted."
            )

        except Exception as exc:

            print(
                f"[BLACKLIST] {exc}"
            )

        return

    welcome = DATA.get(
        "welcome",
        {},
    )

    welcome_text = welcome.get(
        "text"
    )

    if welcome_text:

        text = (
            str(welcome_text)
            .replace("{user}", member.mention)
            .replace("{server}", member.guild.name)
        )

        try:
            channel_id = int(welcome.get("channel_id", 0) or 0)
            channel = member.guild.get_channel(channel_id) if channel_id else None
            target = channel if isinstance(channel, discord.TextChannel) else None
            if target:
                if welcome.get("attachment_url"):
                    data = await fetch_attachment_bytes(welcome["attachment_url"])
                    await target.send(text, file=discord.File(io.BytesIO(data), filename=welcome.get("attachment_name") or "welcome"))
                else:
                    await target.send(text)
            else:
                await member.send(text)
        except Exception as exc:
            print(f"[WELCOME] {exc}")

    try:

        await update_member_count_channel(
            member.guild
        )

    except Exception as exc:

        print(
            f"[JOIN COUNT] {exc}"
        )


# ============================================================
# MEMBER LEAVE
# ============================================================

@bot.event
async def on_member_remove(
    member: discord.Member,
):

    try:

        await update_member_count_channel(
            member.guild
        )

    except Exception as exc:

        print(
            f"[REMOVE COUNT] {exc}"
        )


# ============================================================
# MESSAGE EVENT
# ============================================================

@bot.event
async def on_message(
    message: discord.Message,
):

    try:

        if message.author.bot:
            return

        # ----------------------------------------------------
        # BAD WORD FILTER
        # ----------------------------------------------------

        if message.guild:

            bad_words = DATA.get(
                "bad_words",
                {},
            )

            match = find_bad_word(
                message.content,
                bad_words,
            )

            if match:

                _, reply_message = match

                try:

                    await message.delete()

                except discord.NotFound:
                    pass

                except discord.Forbidden:

                    print(
                        "[FILTER] Missing Manage Messages."
                    )

                except Exception as exc:

                    print(
                        f"[FILTER DELETE] {exc}"
                    )

                if reply_message:

                    try:

                        await message.channel.send(
                            (
                                f"{message.author.mention} "
                                f"{reply_message}"
                            ),
                            delete_after=5,
                        )

                    except Exception as exc:

                        print(
                            f"[FILTER REPLY] {exc}"
                        )

                await apply_message_punishment(
                    message
                )

                return

        # ----------------------------------------------------
        # RENAME REQUEST CHANNEL
        # ----------------------------------------------------

        rename_channel_id = RENAME_REQUEST_CHANNEL_ID

        if (
            message.guild
            and rename_channel_id
            and message.channel.id
            == rename_channel_id
        ):

            new_nickname = (
                message.content.strip()
            )

            try:

                await message.delete()

            except Exception:
                pass

            if not new_nickname:

                try:

                    await message.channel.send(
                        (
                            f"{message.author.mention}, "
                            "please enter a nickname."
                        ),
                        delete_after=5,
                    )

                except Exception:
                    pass

                return

            if len(new_nickname) > 32:

                try:

                    await message.channel.send(
                        (
                            f"{message.author.mention}, "
                            "nickname must be 32 characters "
                            "or fewer!"
                        ),
                        delete_after=5,
                    )

                except Exception:
                    pass

                return

            try:

                embed = discord.Embed(
                    title="📝 New Nickname Request",
                    description=(
                        f"**User:** {message.author.mention}\n"
                        f"**Requested Nickname:** "
                        f"`{discord.utils.escape_markdown(new_nickname)}`"
                    ),
                    color=0xF1C40F,
                )

                sent = await message.channel.send(
                    embed=embed,
                    view=RenameApprovalView(),
                )

                save_rename_request(
                    sent.id,
                    message.author.id,
                    message.guild.id,
                    new_nickname,
                )

            except Exception as exc:

                print(
                    f"[RENAME] {exc}"
                )

            return

        # ----------------------------------------------------
        # COLOR CHANNEL
        # ----------------------------------------------------

        if (
            message.guild
            and COLOR_CHANNEL_ID
            and message.channel.id
            == COLOR_CHANNEL_ID
        ):

            await handle_color_role(
                message
            )

            return

        # ----------------------------------------------------
        # MENTION REACTION
        # ----------------------------------------------------

        if (
            TARGET_USER_ID
            and any(
                user.id == TARGET_USER_ID
                for user in message.mentions
            )
        ):

            try:

                await message.add_reaction(
                    TARGET_EMOJI
                )

            except Exception as exc:

                print(
                    f"[REACTION] {exc}"
                )

        await bot.process_commands(
            message
        )

    except Exception as exc:

        print(
            f"[MESSAGE EVENT] {exc}"
        )


# ============================================================
# EXTRA COMMANDS / AUTOMATION / MUSIC
# ============================================================

VOICE_TARGETS = {}
MUSIC_STATE = {}
AUTOMESSAGE_TASKS = {}
SHORTCUTS = {}


def parse_duration(value: str):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdy])\s*", value.lower())
    if not match:
        raise ValueError("Duration must look like `10s`, `5m`, `2h`, `7d`, or `1y`.")
    amount = float(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "y": 31536000}
    seconds = amount * multipliers[unit]
    if seconds <= 0:
        raise ValueError("Duration must be greater than zero.")
    return seconds


def format_duration(seconds: float):
    if seconds % 31536000 == 0:
        return f"{int(seconds / 31536000)}y"
    if seconds % 86400 == 0:
        return f"{int(seconds / 86400)}d"
    if seconds % 3600 == 0:
        return f"{int(seconds / 3600)}h"
    if seconds % 60 == 0:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds)}s"


async def fetch_attachment_bytes(url: str):
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()


async def send_command_error(interaction, text):
    return await send_error(interaction, text)



# -------------------- ROBUST VOICE HELPERS --------------------

def ensure_opus_loaded():
    """Load libopus explicitly on Linux/Render when available."""
    if discord.opus.is_loaded():
        return True, "already loaded"

    candidates = [
        os.getenv("OPUS_LIBRARY", "").strip(),
        ctypes.util.find_library("opus") or "",
        "libopus.so.0",
        "libopus.so",
    ]

    tried = []
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in tried:
            continue
        tried.append(candidate)
        try:
            discord.opus.load_opus(candidate)
            if discord.opus.is_loaded():
                print(f"[VOICE] Opus loaded: {candidate}")
                return True, candidate
        except Exception as exc:
            print(f"[VOICE] Could not load Opus from {candidate!r}: {type(exc).__name__}: {exc}")

    return False, " / ".join(tried) or "no Opus library found"


def voice_exception_details(exc: Exception) -> str:
    """Return a useful, short diagnosis for Discord voice failures."""
    if isinstance(exc, discord.Forbidden):
        return "Discord denied the voice connection (check Connect/Speak permissions and channel overrides)."
    if isinstance(exc, discord.ClientException):
        return f"Discord voice client state error: {exc}"
    if isinstance(exc, RuntimeError):
        msg = str(exc).strip()
        if msg:
            return f"RuntimeError: {msg}"
        return "RuntimeError (often PyNaCl/voice encryption or a broken voice client state)."
    if isinstance(exc, discord.opus.OpusNotLoaded):
        return "Opus is not loaded on the server. Install libopus/ffmpeg and load libopus.so.0."
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


async def connect_voice(channel: discord.VoiceChannel, *, self_deaf=True):
    """Connect to a voice channel with cleanup/retry and full diagnostics."""
    guild = channel.guild
    me = guild.me

    if me is None:
        raise RuntimeError("Bot member is unavailable in this guild.")

    perms = channel.permissions_for(me)
    if not perms.connect:
        raise discord.Forbidden(
            discord.Object(id=0),
            "Missing Connect permission for the selected voice channel.",
        )

    # Voice playback needs Speak. Joining/deafening alone does not, but
    # requiring it here prevents /play from connecting and then failing later.
    if not self_deaf and not perms.speak:
        raise RuntimeError("Missing Speak permission for the selected voice channel.")

    # /play uses Opus to encode PCM. Load it before creating a playback client.
    opus_ok, opus_info = ensure_opus_loaded()
    if not opus_ok:
        print(f"[VOICE] Opus is unavailable: {opus_info}")

    existing = guild.voice_client
    if existing is not None:
        try:
            if existing.is_connected():
                if existing.channel != channel:
                    print(f"[VOICE] Moving existing connection to {channel.name}...")
                    await existing.move_to(channel)
                return existing
        except Exception as exc:
            print(f"[VOICE] Existing client unusable: {type(exc).__name__}: {exc}")
            try:
                await existing.disconnect(force=True)
            except Exception:
                pass

    last_exc = None
    for attempt in range(1, 4):
        try:
            print(
                f"[VOICE] Connecting to {channel.name} "
                f"(attempt {attempt}/3, self_deaf={self_deaf})..."
            )
            voice = await channel.connect(
                timeout=30.0,
                reconnect=True,
                self_deaf=self_deaf,
            )
            print(
                f"[VOICE] Connected to {channel.name} in {guild.name} "
                f"(opus_loaded={discord.opus.is_loaded()})"
            )
            return voice
        except Exception as exc:
            last_exc = exc
            print(
                f"[VOICE ERROR] {guild.name} / #{channel.name} / "
                f"attempt {attempt}: {voice_exception_details(exc)}"
            )
            traceback.print_exc()
            stale = guild.voice_client
            if stale is not None and not stale.is_connected():
                try:
                    await stale.disconnect(force=True)
                except Exception:
                    pass
            if attempt < 3:
                await asyncio.sleep(2 * attempt)

    raise last_exc or RuntimeError("Voice connection failed.")


async def start_music_idle_timer(guild_id: int, voice: discord.VoiceClient):
    state = MUSIC_STATE.setdefault(guild_id, {})
    old = state.get("idle_task")
    if old and not old.done():
        old.cancel()
    state["idle_task"] = asyncio.create_task(music_leave_later(guild_id, voice))

# -------------------- /JOINVC --------------------

@bot.tree.command(name="joinvc", description="Make the bot join and stay in a voice channel.")
@app_commands.describe(channel="Voice channel to stay in.")
async def joinvc(interaction: discord.Interaction, channel: discord.VoiceChannel):
    error = check_command(interaction, "move_members")
    if error:
        return await send_command_error(interaction, error)
    try:
        old = interaction.guild.voice_client
        if old and old.channel != channel:
            await old.move_to(channel)
        elif not old or not old.is_connected():
            await connect_voice(channel, self_deaf=DATA.get("deafen", True))
        VOICE_TARGETS[interaction.guild.id] = channel.id
        DATA.setdefault("voice", {})[str(interaction.guild.id)] = channel.id
        save_data()
        await interaction.response.send_message(f"Joined {channel.mention} and will reconnect automatically. ✅", ephemeral=True)
    except discord.Forbidden:
        print("[JOINVC] Forbidden: Discord denied the voice connection.")
        await interaction.response.send_message(
            "❌ I cannot connect to that voice channel. Check **Connect** permission and channel overrides.",
            ephemeral=True,
        )
    except Exception as exc:
        print(f"[JOINVC] {voice_exception_details(exc)}")
        traceback.print_exc()
        await interaction.response.send_message(
            f"❌ Could not join that voice channel: `{voice_exception_details(exc)}`",
            ephemeral=True,
        )


# -------------------- /DEAFEN --------------------

@bot.tree.command(name="deafen", description="Deafen the bot in its current voice channel.")
async def deafen(interaction: discord.Interaction):
    error = check_command(interaction, "move_members")
    if error:
        return await send_command_error(interaction, error)
    voice = interaction.guild.voice_client
    if not voice or not voice.is_connected():
        return await interaction.response.send_message("I am not connected to a voice channel.", ephemeral=True)
    try:
        await voice.guild.change_voice_state(channel=voice.channel, self_deaf=True)
        DATA["deafen"] = True
        save_data()
        await interaction.response.send_message("Bot deafened. 🔇", ephemeral=True)
    except Exception as exc:
        print(f"[DEAFEN] {exc}")
        await interaction.response.send_message("Could not deafen the bot.", ephemeral=True)


# -------------------- /UNBANALL --------------------

@bot.tree.command(name="unbanall", description="Unban every banned member from this server.")
async def unbanall(interaction: discord.Interaction):
    error = check_command(interaction, "ban_members")
    if error:
        return await send_command_error(interaction, error)
    await interaction.response.defer(ephemeral=True)
    count = 0
    try:
        bans = [entry async for entry in interaction.guild.bans(limit=None)]
        for entry in bans:
            try:
                await interaction.guild.unban(entry.user, reason=f"Unban all by {interaction.user}")
                count += 1
                await asyncio.sleep(0.25)
            except discord.HTTPException as exc:
                print(f"[UNBANALL] {entry.user}: {exc}")
        await interaction.followup.send(f"Unbanned **{count}** member(s). ✅", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I need Ban Members permission.", ephemeral=True)
    except Exception as exc:
        print(f"[UNBANALL] {exc}")
        await interaction.followup.send("Failed to read the server ban list.", ephemeral=True)


# -------------------- /SETWELCOMEMESSAGE --------------------

@bot.tree.command(name="setwelcomemessage", description="Set the welcome message and channel for new members.")
@app_commands.describe(channel="Channel where the welcome message is sent.", message="Welcome message. Use {user} and {server} placeholders.", file="Optional welcome image/file.")
async def setwelcomemessage(interaction: discord.Interaction, channel: discord.TextChannel, message: str, file: Optional[discord.Attachment] = None):
    error = check_command(interaction, "manage_guild")
    if error:
        return await send_command_error(interaction, error)
    DATA["welcome"] = {
        "text": message,
        "channel_id": channel.id,
        "attachment_url": file.url if file else None,
        "attachment_name": file.filename if file else None,
    }
    save_data()
    await interaction.response.send_message(f"Welcome message enabled in {channel.mention}. ✅", ephemeral=True)


# -------------------- /SENDDM --------------------

@bot.tree.command(name="senddm", description="Send a DM to all members of this server.")
@app_commands.describe(message="DM message to send.", include_bots="Whether bots should also receive the DM.")
async def senddm(interaction: discord.Interaction, message: str, include_bots: bool = False):
    error = check_command(interaction, "administrator")
    if error:
        return await send_command_error(interaction, error)
    await interaction.response.defer(ephemeral=True)
    sent = 0
    failed = 0
    for member in interaction.guild.members:
        if member.bot and not include_bots:
            continue
        try:
            await member.send(message)
            sent += 1
            await asyncio.sleep(1.0)
        except Exception:
            failed += 1
    await interaction.followup.send(f"DM complete: **{sent}** sent, **{failed}** failed. ⚠️", ephemeral=True)


# -------------------- /UNTIMEOUTALL --------------------

@bot.tree.command(name="untimeoutall", description="Remove timeouts from all members who can be moderated.")
async def untimeoutall(interaction: discord.Interaction):
    error = check_command(interaction, "moderate_members")
    if error:
        return await send_command_error(interaction, error)
    await interaction.response.defer(ephemeral=True)
    count = 0
    me = interaction.guild.me
    for member in interaction.guild.members:
        if member.is_timed_out() and me and member.id != me.id and member.top_role < me.top_role and member.id != interaction.guild.owner_id:
            try:
                await member.timeout(None, reason=f"Untimeout all by {interaction.user}")
                count += 1
                await asyncio.sleep(0.2)
            except Exception:
                pass
    await interaction.followup.send(f"Removed timeouts from **{count}** member(s). ✅", ephemeral=True)


# -------------------- BOOST MESSAGE --------------------

@bot.tree.command(name="boostmessage", description="Enable or disable a server boost announcement.")
@app_commands.describe(enabled="on or off.", channel="Channel for boost messages.")
@app_commands.choices(enabled=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def boostmessage(interaction: discord.Interaction, enabled: app_commands.Choice[str], channel: Optional[discord.TextChannel] = None):
    error = check_command(interaction, "manage_guild")
    if error:
        return await send_command_error(interaction, error)
    config = DATA.setdefault("boost", {})
    if enabled.value == "off":
        config[str(interaction.guild.id)] = {"enabled": False, "channel_id": 0}
        save_data()
        return await interaction.response.send_message("Boost messages disabled. ✅", ephemeral=True)
    if channel is None:
        return await interaction.response.send_message("Choose a channel when enabling boost messages.", ephemeral=True)
    config[str(interaction.guild.id)] = {"enabled": True, "channel_id": channel.id}
    save_data()
    await interaction.response.send_message(f"Boost messages enabled in {channel.mention}. 🚀", ephemeral=True)


# -------------------- /AUTOMESSAGE --------------------

@bot.tree.command(name="automessage", description="Send a repeating message on a timer and delete each copy after 5 seconds.")
@app_commands.describe(time="Interval such as 30s, 5m, or 1h.", message="Message to repeat.", file="Optional file to attach.")
async def automessage(interaction: discord.Interaction, time: str, message: str, file: Optional[discord.Attachment] = None):
    error = check_command(interaction, "manage_messages")
    if error:
        return await send_command_error(interaction, error)
    try:
        seconds = parse_duration(time)
    except ValueError as exc:
        return await interaction.response.send_message(str(exc), ephemeral=True)
    if seconds < 5:
        return await interaction.response.send_message("The minimum interval is 5 seconds.", ephemeral=True)
    key = (interaction.guild.id, interaction.channel.id)
    old = AUTOMESSAGE_TASKS.get(key)
    if old:
        old.cancel()
    config = {"channel_id": interaction.channel.id, "guild_id": interaction.guild.id, "message": message, "attachment_url": file.url if file else None, "attachment_name": file.filename if file else None, "seconds": seconds}
    DATA.setdefault("automessages", {})[str(key)] = config
    save_data()
    AUTOMESSAGE_TASKS[key] = asyncio.create_task(automessage_worker(config))
    await interaction.response.send_message(f"Automessage started every **{format_duration(seconds)}**. Each copy is deleted after 5s. ✅", ephemeral=True)


async def automessage_worker(config):
    await asyncio.sleep(config["seconds"])
    while True:
        try:
            channel = bot.get_channel(int(config["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                return
            kwargs = {"content": config["message"] or None}
            if config.get("attachment_url"):
                data = await fetch_attachment_bytes(config["attachment_url"])
                kwargs["file"] = discord.File(io.BytesIO(data), filename=config.get("attachment_name") or "file")
            sent = await channel.send(**kwargs)
            await asyncio.sleep(5)
            try:
                await sent.delete()
            except Exception:
                pass
            await asyncio.sleep(config["seconds"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[AUTOMESSAGE] {exc}")
            await asyncio.sleep(config["seconds"])


# -------------------- /CREATEVCMEMBERS --------------------

@bot.tree.command(name="createvcmembers", description="Create/update a voice channel showing the server member count.")
async def createvcmembers(interaction: discord.Interaction):
    error = check_command(interaction, "manage_channels")
    if error:
        return await send_command_error(interaction, error)
    channel = interaction.guild.get_channel(MEMBER_COUNT_CHANNEL_ID)
    try:
        if not isinstance(channel, discord.VoiceChannel):
            channel = await interaction.guild.create_voice_channel(f"ᴍᴇᴍʙᴇʀꜱ: {interaction.guild.member_count}", reason="Create member counter")
            DATA.setdefault("member_count", {})[str(interaction.guild.id)] = channel.id
            save_data()
        await channel.edit(name=f"ᴍᴇᴍʙᴇʀꜱ: {interaction.guild.member_count}")
        await interaction.response.send_message(f"Member counter ready: {channel.mention} ✅", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I need Manage Channels permission.", ephemeral=True)
    except Exception as exc:
        print(f"[CREATEVCMEMBERS] {exc}")
        await interaction.response.send_message("Could not create the member counter.", ephemeral=True)


# -------------------- /EMBED --------------------

@bot.tree.command(name="embed", description="Send a custom embed with an optional file/image.")
@app_commands.describe(title="Embed title.", message="Embed description.", file="Optional image/gif/file.")
async def embed_command(interaction: discord.Interaction, title: str, message: str, file: Optional[discord.Attachment] = None):
    error = check_command(interaction, "manage_messages")
    if error:
        return await send_command_error(interaction, error)
    try:
        emb = discord.Embed(title=title, description=message, color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        if file:
            emb.set_image(url=f"attachment://{file.filename}")
            await interaction.channel.send(embed=emb, file=await file.to_file())
        else:
            await interaction.channel.send(embed=emb)
        await interaction.response.send_message("Embed sent. ✅", ephemeral=True)
    except Exception as exc:
        print(f"[EMBED] {exc}")
        await interaction.response.send_message("Could not send the embed.", ephemeral=True)


# -------------------- MUSIC --------------------

async def extract_audio(query: str):
    import yt_dlp
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "source_address": "0.0.0.0",
    }
    def run_extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {"url": info["url"], "title": info.get("title", "Unknown"), "webpage_url": info.get("webpage_url", query)}
    return await asyncio.to_thread(run_extract)


async def music_leave_later(guild_id: int, voice: discord.VoiceClient):
    state = MUSIC_STATE.get(guild_id)
    if state:
        state["idle_task"] = asyncio.current_task()
    try:
        await asyncio.sleep(300)
        state = MUSIC_STATE.get(guild_id, {})
        if state.get("voice") is voice and not voice.is_playing() and not voice.is_paused():
            await voice.disconnect()
            await asyncio.sleep(1)
            await ensure_saved_voice_channel(guild_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[MUSIC IDLE] {exc}")


async def ensure_saved_voice_channel(guild_id: int):
    target_id = VOICE_TARGETS.get(guild_id) or DATA.get("voice", {}).get(str(guild_id))
    if not target_id:
        return
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(int(target_id)) if guild else None
    if isinstance(channel, discord.VoiceChannel):
        try:
            voice = guild.voice_client
            if voice and voice.is_connected():
                if voice.channel != channel:
                    await voice.move_to(channel)
            else:
                await connect_voice(channel, self_deaf=DATA.get("deafen", True))
        except Exception as exc:
            print(f"[VOICE SAVED] {exc}")


@bot.tree.command(name="play", description="Play music from YouTube/search in your current voice channel.")
@app_commands.describe(query="Song name or URL.")
async def play(interaction: discord.Interaction, query: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in a server.", ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)

    await interaction.response.defer()
    channel = interaction.user.voice.channel

    try:
        # Load Opus before playback. This is separate from FFmpeg.
        opus_ok, opus_info = ensure_opus_loaded()
        if not opus_ok:
            raise RuntimeError(
                f"Opus library is not available on Render ({opus_info}). "
                "Install libopus and PyNaCl."
            )

        me = interaction.guild.me
        if me is None:
            raise RuntimeError("Bot member is unavailable.")
        channel_perms = channel.permissions_for(me)
        if not channel_perms.connect:
            raise RuntimeError("Missing Connect permission for the selected voice channel.")
        if not channel_perms.speak:
            raise RuntimeError("Missing Speak permission for the selected voice channel.")

        voice = await connect_voice(channel, self_deaf=DATA.get("deafen", True))

        info = await extract_audio(query)

        if voice.is_playing() or voice.is_paused():
            voice.stop()

        source = discord.FFmpegPCMAudio(
            info["url"],
            before_options=(
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 5 "
                "-nostdin"
            ),
            options="-vn",
        )

        def playback_after(error):
            if error:
                print(f"[MUSIC AFTER] {type(error).__name__}: {error}")
                traceback.print_exception(type(error), error, error.__traceback__)
            else:
                print("[MUSIC] Playback finished.")
            # Schedule the 5-minute idle timer back on the bot loop.
            try:
                bot.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        music_leave_later(interaction.guild.id, voice)
                    )
                )
            except Exception as exc:
                print(f"[MUSIC IDLE SCHEDULE] {exc}")

        voice.play(source, after=playback_after)

        state = MUSIC_STATE.setdefault(interaction.guild.id, {})
        idle = state.get("idle_task")
        if idle and not idle.done():
            idle.cancel()

        state.update({
            "voice": voice,
            "title": info["title"],
        })

        await interaction.followup.send(
            f"▶️ Now playing **{info['title']}**"
        )

    except FileNotFoundError:
        print("[PLAY] FFmpeg executable was not found.")
        traceback.print_exc()
        await interaction.followup.send(
            "❌ FFmpeg is not installed on the server. Add `ffmpeg` to Aptfile."
        )
    except Exception as exc:
        print(f"[PLAY] {voice_exception_details(exc)}")
        traceback.print_exc()
        await interaction.followup.send(
            f"❌ Could not play that track: `{voice_exception_details(exc)}`"
        )


# -------------------- /CREATESHORTCUT --------------------

@bot.tree.command(name="createshortcut", description="Create a text shortcut that triggers a bot command.")
@app_commands.describe(command="Command name without the slash, e.g. play.", shortcut="Shortcut text, e.g. p.")
async def createshortcut(interaction: discord.Interaction, command: str, shortcut: str):
    error = check_command(interaction, "manage_guild")
    if error:
        return await send_command_error(interaction, error)
    command = command.lower().lstrip("/")
    shortcut = shortcut.strip().lower()
    allowed = {c.name for c in bot.tree.get_commands()}
    if command not in allowed:
        return await interaction.response.send_message("That bot command does not exist.", ephemeral=True)
    if not shortcut or len(shortcut) > 32 or " " in shortcut:
        return await interaction.response.send_message("Shortcut must be 1-32 characters with no spaces.", ephemeral=True)
    SHORTCUTS.setdefault(str(interaction.guild.id), {})[shortcut] = command
    DATA.setdefault("shortcuts", {})[str(interaction.guild.id)] = SHORTCUTS[str(interaction.guild.id)]
    save_data()
    await interaction.response.send_message(f"Shortcut `{shortcut}` → `/{command}` created. Use `{shortcut}` in chat. ✅", ephemeral=True)


# -------------------- /BAN --------------------

@bot.tree.command(name="ban", description="Ban a member for a specified duration.")
@app_commands.describe(member="Member to ban.", duration="Examples: 10m, 2h, 7d, 1y.", reason="Reason for the ban.")
async def ban(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
    error = check_command(interaction, "ban_members")
    if error:
        return await send_command_error(interaction, error)
    ok, why = bot_can_act_on(interaction.guild, member)
    if not ok:
        return await interaction.response.send_message(why, ephemeral=True)
    try:
        seconds = parse_duration(duration)
    except ValueError as exc:
        return await interaction.response.send_message(str(exc), ephemeral=True)
    try:
        await member.ban(reason=f"{reason} | Duration: {duration} | By: {interaction.user}")
        await interaction.response.send_message(f"Banned {member.mention} for **{format_duration(seconds)}**. 🔨")
        asyncio.create_task(temporary_unban(interaction.guild.id, member.id, seconds, reason))
    except discord.Forbidden:
        await interaction.response.send_message("I cannot ban that member. Check my role position and permissions.", ephemeral=True)
    except Exception as exc:
        print(f"[BAN] {exc}")
        await interaction.response.send_message("Ban failed.", ephemeral=True)


async def temporary_unban(guild_id: int, user_id: int, seconds: float, reason: str):
    try:
        await asyncio.sleep(seconds)
        guild = bot.get_guild(guild_id)
        if guild:
            await guild.unban(discord.Object(id=user_id), reason=f"Temporary ban expired: {reason}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[TEMP UNBAN] {exc}")


# -------------------- /TIMEOUT --------------------

@bot.tree.command(name="timeout", description="Timeout a member for a specified duration.")
@app_commands.describe(member="Member to timeout.", duration="Examples: 10s, 5m, 2h, 7d, 1y.", reason="Reason for the timeout.")
async def timeout(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
    error = check_command(interaction, "moderate_members")
    if error:
        return await send_command_error(interaction, error)
    ok, why = bot_can_act_on(interaction.guild, member)
    if not ok:
        return await interaction.response.send_message(why, ephemeral=True)
    try:
        seconds = parse_duration(duration)
        if seconds > 28 * 86400:
            return await interaction.response.send_message("Discord timeouts cannot exceed 28 days.", ephemeral=True)
        await member.timeout(timedelta(seconds=seconds), reason=reason)
        await interaction.response.send_message(f"Timed out {member.mention} for **{format_duration(seconds)}**. ⏱️")
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I cannot timeout that member.", ephemeral=True)
    except Exception as exc:
        print(f"[TIMEOUT] {exc}")
        await interaction.response.send_message("Timeout failed.", ephemeral=True)


# -------------------- /REACTTAG --------------------

@bot.tree.command(name="reacttag", description="React to messages that directly mention you with a chosen emoji.")
@app_commands.describe(emoji="Emoji to react with.", user="The user who must be directly mentioned.")
async def reacttag(interaction: discord.Interaction, emoji: str, user: discord.Member):
    error = check_command(interaction, "manage_guild")
    if error:
        return await send_command_error(interaction, error)
    DATA.setdefault("reacttag", {})[str(interaction.guild.id)] = {"user_id": user.id, "emoji": emoji}
    save_data()
    await interaction.response.send_message(f"React-tag enabled for {user.mention} with {emoji}. It only reacts to direct @mentions. ✅", ephemeral=True)


# -------------------- EXTRA MESSAGE HANDLERS --------------------

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    try:
        if before.premium_since is None and after.premium_since is not None:
            cfg = DATA.get("boost", {}).get(str(after.guild.id), {})
            if cfg.get("enabled") and cfg.get("channel_id"):
                channel = after.guild.get_channel(int(cfg["channel_id"]))
                if isinstance(channel, discord.TextChannel):
                    emb = discord.Embed(
                        title="🚀 New Server Boost!",
                        description=f"Thank you {after.mention} for boosting **{after.guild.name}**! 💜",
                        color=0xF47FFF,
                        timestamp=discord.utils.utcnow(),
                    )
                    if after.guild.icon:
                        emb.set_thumbnail(url=after.guild.icon.url)
                    if after.guild.banner:
                        emb.set_image(url=after.guild.banner.url)
                    boost_gif = os.getenv("BOOST_GIF_URL", "").strip()
                    if boost_gif:
                        emb.set_image(url=boost_gif)
                    await channel.send(embed=emb)
    except Exception as exc:
        print(f"[BOOST] {exc}")


# Load persisted shortcuts and voice targets once the bot is ready.
async def restore_extra_state():
    for guild_id, mapping in DATA.get("shortcuts", {}).items():
        try:
            SHORTCUTS[int(guild_id)] = dict(mapping)
        except Exception:
            pass
    for guild_id, channel_id in DATA.get("voice", {}).items():
        try:
            VOICE_TARGETS[int(guild_id)] = int(channel_id)
        except Exception:
            pass


# Extend on_ready behavior without replacing the existing handler.
_original_ready = on_ready
@bot.event
async def on_ready():
    await _original_ready()
    await restore_extra_state()
    # Recreate automessage workers after reconnect/startup.
    for raw_key, config in DATA.get("automessages", {}).items():
        try:
            guild_id, channel_id = [int(x.strip("() ")) for x in raw_key.split(",")[:2]]
            key = (guild_id, channel_id)
            if key not in AUTOMESSAGE_TASKS or AUTOMESSAGE_TASKS[key].done():
                AUTOMESSAGE_TASKS[key] = asyncio.create_task(automessage_worker(config))
        except Exception:
            pass
    # Prefer the saved /joinvc target over the hard-coded legacy channel.
    for guild in bot.guilds:
        if guild.id in VOICE_TARGETS:
            try:
                await ensure_saved_voice_channel(guild.id)
            except Exception as exc:
                print(f"[VOICE RESTORE] {exc}")


# Replace the original voice reconnect handler with one that supports /joinvc.
_original_voice_state_update = on_voice_state_update
@bot.event
async def on_voice_state_update(member, before, after):
    await _original_voice_state_update(member, before, after)
    if bot.user is None or member.id != bot.user.id or after.channel is not None:
        return
    await asyncio.sleep(3)
    try:
        await ensure_saved_voice_channel(member.guild.id)
    except Exception as exc:
        print(f"[VOICE RECONNECT] {exc}")


# React-tag and shortcut handling are intentionally kept in one extra event.
_original_on_message = on_message
@bot.event
async def on_message(message: discord.Message):
    await _original_on_message(message)
    if message.author.bot or not message.guild:
        return
    try:
        cfg = DATA.get("reacttag", {}).get(str(message.guild.id), {})
        target_id = int(cfg.get("user_id", 0) or 0)
        emoji = cfg.get("emoji")
        if target_id and emoji and any(m.id == target_id for m in message.mentions) and not message.reference:
            try:
                await message.add_reaction(emoji)
            except Exception as exc:
                print(f"[REACTTAG] {exc}")
        mapping = SHORTCUTS.get(message.guild.id, DATA.get("shortcuts", {}).get(str(message.guild.id), {}))
        raw = message.content.strip()
        parts = raw.split(maxsplit=1)
        shortcut = parts[0].lower() if parts else ""
        command_name = mapping.get(shortcut) if isinstance(mapping, dict) else None
        if command_name == "play":
            query = parts[1].strip() if len(parts) > 1 else ""
            if not query:
                await message.channel.send("Usage: `<shortcut> <song name or URL>`", delete_after=5)
            elif isinstance(message.author, discord.Member) and message.author.voice and message.author.voice.channel:
                try:
                    voice = message.guild.voice_client
                    if voice and voice.channel != message.author.voice.channel:
                        await voice.move_to(message.author.voice.channel)
                    elif not voice or not voice.is_connected():
                        voice = await connect_voice(message.author.voice.channel, self_deaf=DATA.get("deafen", True))
                    info = await extract_audio(query)
                    if voice.is_playing():
                        voice.stop()
                    opus_ok, opus_info = ensure_opus_loaded()
                    if not opus_ok:
                        raise RuntimeError(f"Opus library is not available ({opus_info}).")
                    source = discord.FFmpegPCMAudio(info["url"], before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin", options="-vn")
                    voice.play(source, after=lambda err: print(f"[SHORTCUT MUSIC] {err}") if err else print("[SHORTCUT MUSIC] Playback finished."))
                    state = MUSIC_STATE.setdefault(message.guild.id, {})
                    idle = state.get("idle_task")
                    if idle and not idle.done():
                        idle.cancel()
                    state.update({"voice": voice, "title": info["title"]})
                    await message.channel.send(f"▶️ Now playing **{info['title']}**", delete_after=8)
                except Exception as exc:
                    print(f"[SHORTCUT PLAY] {exc}")
        elif command_name:
            await message.channel.send(f"Shortcut `{shortcut}` is linked to `/{command_name}`. Add the command arguments after the shortcut.", delete_after=5)
    except Exception as exc:
        print(f"[EXTRA MESSAGE] {exc}")


# ============================================================
# /SENDHERE
# ============================================================

@bot.tree.command(
    name="sendhere",
    description="Send a message with optional files here.",
)
@app_commands.describe(
    message="Message to send.",
    file1="Optional file.",
    file2="Optional file.",
    file3="Optional file.",
    file4="Optional file.",
    file5="Optional file.",
    file6="Optional file.",
    file7="Optional file.",
    file8="Optional file.",
    file9="Optional file.",
    file10="Optional file.",
)
async def sendhere(
    interaction: discord.Interaction,
    message: str = "",
    file1: Optional[discord.Attachment] = None,
    file2: Optional[discord.Attachment] = None,
    file3: Optional[discord.Attachment] = None,
    file4: Optional[discord.Attachment] = None,
    file5: Optional[discord.Attachment] = None,
    file6: Optional[discord.Attachment] = None,
    file7: Optional[discord.Attachment] = None,
    file8: Optional[discord.Attachment] = None,
    file9: Optional[discord.Attachment] = None,
    file10: Optional[discord.Attachment] = None,
):

    error = check_command(
        interaction,
        "manage_messages",
    )

    if error:

        return await send_error(
            interaction,
            error,
        )

    attachments = [
        file1,
        file2,
        file3,
        file4,
        file5,
        file6,
        file7,
        file8,
        file9,
        file10,
    ]

    if (
        not message.strip()
        and not any(attachments)
    ):

        return await send_error(
            interaction,
            "Provide a message or at least one file.",
        )

    try:

        await interaction.response.defer(
            ephemeral=True
        )

        files = await collect_files(
            *attachments
        )

        channel = interaction.channel

        if channel is None:

            return await interaction.followup.send(
                "Unable to resolve this channel.",
                ephemeral=True,
            )

        await channel.send(
            content=(
                message
                if message.strip()
                else None
            ),
            files=files,
        )

        await interaction.followup.send(
            "Message sent successfully! ✅",
            ephemeral=True,
        )

    except discord.Forbidden:

        await interaction.followup.send(
            "I don't have permission to send messages/files here.",
            ephemeral=True,
        )

    except Exception as exc:

        print(
            f"[SENDHERE] {exc}"
        )

        await interaction.followup.send(
            "Failed to send the message.",
            ephemeral=True,
        )


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):

    print(
        f"[SLASH COMMAND ERROR] {repr(error)}"
    )

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                "An error occurred while running that command.",
                ephemeral=True,
            )

        else:

            await interaction.response.send_message(
                "An error occurred while running that command.",
                ephemeral=True,
            )

    except Exception as exc:

        print(
            f"[ERROR HANDLER] {exc}"
        )


# ============================================================
# STARTUP
# ============================================================

def main():

    print(
        "=================================================="
    )

    print(
        "[STARTUP] Starting Discord bot..."
    )

    print(
        f"[STARTUP] Python PID: {os.getpid()}"
    )

    print(
        f"[STARTUP] Port: {PORT}"
    )

    print(
        f"[STARTUP] Data file: {DATA_FILE}"
    )

    print(
        f"[STARTUP] Token configured: {'YES' if TOKEN else 'NO'}"
    )

    print(
        "=================================================="
    )

    # Start the Render health endpoint exactly once.
    # The HTTP server is intentionally independent from Discord login so
    # Cloudflare/Discord failures cannot make Render's health check time out.
    keep_alive()

    # Voice dependency diagnostics. These do not contact Discord and are safe
    # to run before login.
    try:
        import nacl  # noqa: F401
        print("[VOICE CHECK] PyNaCl: OK")
    except Exception as exc:
        print(f"[VOICE CHECK] PyNaCl: MISSING/BROKEN: {type(exc).__name__}: {exc}")

    opus_ok, opus_info = ensure_opus_loaded()
    print(
        f"[VOICE CHECK] Opus: {'OK' if opus_ok else 'MISSING'} "
        f"({opus_info})"
    )

    if not TOKEN:
        print(
            "[FATAL] DISCORD_TOKEN is not configured."
        )
        print(
            "[FATAL] Add DISCORD_TOKEN to Render Environment Variables."
        )

        while True:
            try:
                threading.Event().wait(60)
            except KeyboardInterrupt:
                print("[SHUTDOWN] Interrupted.")
                return

    # discord.py closes its aiohttp session when bot.run() exits after an
    # exception. Reusing the same Bot object causes "Session is closed".
    # We also must NOT use os.execv() here: execv preserves open file
    # descriptors, including the Flask listening socket, which can lead to
    # Render reporting "Address already in use".
    #
    # On a 429 we wait, then terminate this process. Render will start a
    # completely fresh Python process, which creates a fresh Bot/session and
    # releases the old port cleanly.
    fallback_retry_delay = 15 * 60
    max_retry_delay = 15 * 60

    while True:
        try:
            print("[DISCORD] Connecting to Discord...")

            bot.run(
                TOKEN,
                log_handler=None,
            )

            print(
                "[SHUTDOWN] Discord bot stopped cleanly."
            )
            return

        except discord.LoginFailure:
            print(
                "[FATAL] Discord rejected the bot token."
            )
            print(
                "[FATAL] Generate/copy a new bot token and "
                "update DISCORD_TOKEN in Render."
            )
            return

        except discord.PrivilegedIntentsRequired:
            print(
                "[FATAL] Discord requires privileged intents."
            )
            print(
                "[FATAL] Enable Server Members Intent and "
                "Message Content Intent in the Discord Developer Portal."
            )
            return

        except discord.HTTPException as exc:
            if getattr(exc, "status", None) != 429:
                print(
                    f"[FATAL] Discord HTTP error: {repr(exc)}"
                )
                raise

            # Prefer Discord's Retry-After header when it exists.
            retry_after = None
            try:
                response = getattr(exc, "response", None)
                headers = getattr(response, "headers", {})
                header_value = headers.get("Retry-After")
                if header_value:
                    retry_after = float(header_value)
            except Exception:
                retry_after = None

            # Cloudflare Error 1015 pages often have no Retry-After header.
            # Use a conservative 15-minute delay rather than hammering the
            # same Render/Cloudflare IP repeatedly.
            if retry_after is None or retry_after <= 0:
                retry_after = fallback_retry_delay

            retry_after = min(
                max(retry_after, 1),
                max_retry_delay,
            )

            print(
                "[RATE LIMIT] Discord returned HTTP 429. "
                f"Waiting {retry_after:.0f}s before Render restart..."
            )

            try:
                time.sleep(retry_after)
            except KeyboardInterrupt:
                print(
                    "[SHUTDOWN] Interrupted while waiting to retry."
                )
                return

            print(
                "[RATE LIMIT] Exiting cleanly so Render can start a "
                "fresh process/session..."
            )

            # Do NOT os.execv(). A new process must be created by Render so
            # the old listening socket on PORT is fully released.
            os._exit(75)

        except Exception as exc:
            print(
                f"[FATAL] Bot stopped: {repr(exc)}"
            )
            raise


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
