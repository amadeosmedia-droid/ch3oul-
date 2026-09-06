import asyncio
import json
import os
import re
import threading
from datetime import timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask


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


@app.route("/")
def home():
    return "Discord bot is alive! ✅"


@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot": "running"
    }


def run_flask():
    try:
        app.run(
            host="0.0.0.0",
            port=PORT,
            debug=False,
            use_reloader=False,
        )
    except Exception as exc:
        print(f"[FLASK] {exc}")


def keep_alive():
    thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="FlaskThread",
    )
    thread.start()


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

    channel = guild.get_channel(
        MEMBER_COUNT_CHANNEL_ID
    )

    if not isinstance(
        channel,
        discord.VoiceChannel,
    ):
        return

    try:

        new_name = (
            f"members: {guild.member_count}"
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
            .replace(
                "{user}",
                member.mention,
            )
            .replace(
                "{server}",
                member.guild.name,
            )
        )

        try:

            await member.send(
                text
            )

        except Exception:
            pass

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

    # Start Render web server regardless of Discord
    # connection state.
    keep_alive()

    if not TOKEN:

        print(
            "[FATAL] DISCORD_TOKEN is not configured."
        )

        print(
            "[FATAL] Add DISCORD_TOKEN to Render Environment Variables."
        )

        # Keep the web service alive instead of immediately
        # killing the Render process.
        while True:

            try:
                threading.Event().wait(60)

            except KeyboardInterrupt:
                break

        return

    # Discord/Cloudflare may temporarily return HTTP 429 (Error 1015)
    # while the service IP is rate-limited.  Do not let a temporary
    # rate-limit kill the Render process: wait and retry with backoff.
    retry_delay = 30
    max_retry_delay = 15 * 60

    while True:
        try:

            bot.run(
                TOKEN,
                log_handler=None,
            )

            # A normal shutdown should not be treated as a failure.
            print("[SHUTDOWN] Discord bot stopped normally.")
            break

        except discord.LoginFailure:

            print(
                "[FATAL] Discord rejected the bot token."
            )

            print(
                "[FATAL] Generate/copy a new bot token and "
                "update DISCORD_TOKEN in Render."
            )
            break

        except discord.PrivilegedIntentsRequired:

            print(
                "[FATAL] Discord requires privileged intents."
            )

            print(
                "[FATAL] Enable Server Members Intent and "
                "Message Content Intent in the Discord Developer Portal."
            )
            break

        except discord.HTTPException as exc:

            if getattr(exc, "status", None) == 429:
                retry_after = getattr(exc, "retry_after", None)
                if not isinstance(retry_after, (int, float)) or retry_after <= 0:
                    retry_after = retry_delay

                # Keep the retry bounded so a bad/huge server value cannot
                # accidentally sleep forever.
                retry_after = min(max(float(retry_after), 5), max_retry_delay)

                print(
                    f"[RATE LIMIT] Discord returned HTTP 429. "
                    f"Retrying in {retry_after:.0f}s..."
                )

                try:
                    time.sleep(retry_after)
                except KeyboardInterrupt:
                    print("[SHUTDOWN] Interrupted while waiting to retry.")
                    break

                # Exponential backoff for repeated 429s.
                retry_delay = min(retry_delay * 2, max_retry_delay)
                continue

            print(f"[FATAL] Discord HTTP error: {repr(exc)}")
            break

        except Exception as exc:

            print(
                f"[FATAL] Bot stopped: {repr(exc)}"
            )
            raise

    return



# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()

