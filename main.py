import asyncio
import json
import os
import re
import threading
from datetime import timedelta
from pathlib import Path
from typing import Optional

from flask import Flask
import discord
from discord.ext import commands
from discord import app_commands

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


# ============================================================
# RENDER WEB SERVER
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive and running!"


def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()


# ============================================================
# CONFIG / PERSISTENT DATA
# ============================================================

DATA_FILE = Path(os.environ.get("DATA_FILE", "bot_data.json"))

# Requested permanent voice channel.
FOREVER_VOICE_CHANNEL_ID = 1524066756514287837

# Existing server settings from the old code.
MEMBER_COUNT_CHANNEL_ID = 1544821289506574388
TARGET_USER_ID = 0
TARGET_EMOJI = "👀"
COLOR_CHANNEL_ID = 0
RENAME_REQUEST_CHANNEL_ID = 0
SECURITY_LOGS_CHANNEL_ID = 0

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
            {"label": "Buy VIP", "description": "Buy VIP role here"},
            {"label": "Support", "description": "General assistance"},
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
}


def deep_merge(dst, src):
    if not isinstance(src, dict):
        return
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = value


def load_data():
    if not DATA_FILE.exists():
        return json.loads(json.dumps(DEFAULT_DATA))

    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        data = json.loads(json.dumps(DEFAULT_DATA))
        deep_merge(data, raw)
        return data
    except Exception as exc:
        print(f"[DATA] Failed to load: {exc}")
        return json.loads(json.dumps(DEFAULT_DATA))


DATA = load_data()


def save_data():
    temp = DATA_FILE.with_suffix(".tmp")
    try:
        temp.write_text(
            json.dumps(DATA, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(DATA_FILE)
    except Exception as exc:
        print(f"[DATA] Failed to save: {exc}")


# ============================================================
# RUNTIME STATE
# ============================================================

CUSTOM_WELCOME_TEXT = DATA["welcome"]["text"]

AUTOMESSAGE_TASKS = {}
LAST_AUTOMESSAGE_ID = {}

VOICE_LOCKS = set()
MUSIC_STATE = {}
MUSIC_IDLE_TASKS = {}

intents = discord.Intents.all()


# ============================================================
# SECURITY HELPERS
# ============================================================

def has_user_permission(interaction: discord.Interaction, permission: str) -> bool:
    return (
        interaction.guild is not None
        and isinstance(interaction.user, discord.Member)
        and bool(getattr(interaction.user.guild_permissions, permission, False))
    )


def check_command(
    interaction: discord.Interaction,
    permission: str = "administrator",
) -> Optional[str]:
    if interaction.guild is None:
        return "This command can only be used inside a server."

    if not has_user_permission(interaction, permission):
        return f"You need `{permission.replace('_', ' ').title()}` permission."

    return None


def bot_can_act_on(
    guild: discord.Guild,
    target: discord.Member,
    action: str,
) -> tuple[bool, str]:
    me = guild.me

    if me is None:
        return False, "I cannot resolve my member object."

    if target.id == me.id:
        return False, "I cannot act on myself."

    if target.id == guild.owner_id:
        return False, "I cannot act on the server owner."

    # Important security rule requested by you.
    if target.guild_permissions.administrator:
        return False, "For security, the bot will never moderate a server administrator."

    # Discord role hierarchy protection.
    if target.top_role >= me.top_role:
        return (
            False,
            "I cannot act on a member whose highest role is equal to or higher than my highest role.",
        )

    permission_map = {
        "kick": "kick_members",
        "ban": "ban_members",
        "timeout": "moderate_members",
        "manage_roles": "manage_roles",
        "manage_channels": "manage_channels",
    }

    required = permission_map.get(action)
    if required and not getattr(me.guild_permissions, required, False):
        return False, f"I am missing the `{required.replace('_', ' ')}` permission."

    return True, ""


def valid_hex(value: str) -> int:
    cleaned = value.strip().replace("#", "")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", cleaned):
        raise ValueError("HEX must be 6 characters, for example #5865F2.")
    return int(cleaned, 16)


def safe_channel_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9-]+", "-", name.lower()).strip("-")
    return (name or "ticket")[:90]


async def send_error(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass


async def security_log(
    guild: discord.Guild,
    title: str,
    description: str,
    color: int = 0xED4245,
):
    channel_id = DATA.get("security_logs_channel", 0) or SECURITY_LOGS_CHANNEL_ID
    channel = guild.get_channel(channel_id) if channel_id else None

    if not isinstance(channel, discord.TextChannel):
        return

    try:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(embed=embed)
    except Exception as exc:
        print(f"[SECURITY LOG] {exc}")


async def update_member_count_channel(guild: discord.Guild):
    channel = guild.get_channel(MEMBER_COUNT_CHANNEL_ID)

    if isinstance(channel, discord.VoiceChannel):
        try:
            new_name = f"members: {guild.member_count}"
            if channel.name != new_name:
                await channel.edit(
                    name=new_name,
                    reason="Updating member count stats.",
                )
        except Exception as exc:
            print(f"[COUNT] {exc}")


# ============================================================
# TICKET SYSTEM
# ============================================================

def ticket_record(channel_id: int):
    return DATA["ticket"]["tickets"].get(str(channel_id))


def staff_can_manage_ticket(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and (
            interaction.user.guild_permissions.manage_channels
            or interaction.user.guild_permissions.administrator
        )
    )


async def ticket_is_manager(interaction: discord.Interaction) -> bool:
    rec = ticket_record(interaction.channel.id) if interaction.channel else None

    if not rec:
        return False

    if interaction.user.id == rec.get("owner_id"):
        return True

    return staff_can_manage_ticket(interaction)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

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
        if not await ticket_is_manager(interaction):
            return await send_error(
                interaction,
                "You are not allowed to close this ticket.",
            )

        await close_ticket_channel(interaction)

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
        if not await ticket_is_manager(interaction):
            return await send_error(
                interaction,
                "You are not allowed to reopen this ticket.",
            )

        await reopen_ticket_channel(interaction)

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
        if not staff_can_manage_ticket(interaction):
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

        if channel:
            DATA["ticket"]["tickets"].pop(str(channel.id), None)
            save_data()

            try:
                await channel.delete(
                    reason=f"Ticket deleted by {interaction.user}",
                )
            except Exception as exc:
                print(f"[TICKET DELETE] {exc}")


class TicketPanelSelect(discord.ui.Select):
    def __init__(self):
        reasons = DATA["ticket"].get("reasons", [])[:25]

        options = [
            discord.SelectOption(
                label=str(reason.get("label", "Support"))[:100],
                description=str(
                    reason.get("description", "General support")
                )[:100],
                value=str(index),
                emoji="🎫",
            )
            for index, reason in enumerate(reasons)
        ]

        if not options:
            options = [
                discord.SelectOption(
                    label="Support",
                    description="General support",
                    value="0",
                    emoji="🎫",
                )
            ]

        super().__init__(
            placeholder="Select a ticket reason...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket:reason",
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(
            interaction.user,
            discord.Member,
        ):
            return await send_error(
                interaction,
                "This panel can only be used in a server.",
            )

        # One active ticket per user.
        existing = [
            record
            for record in DATA["ticket"]["tickets"].values()
            if record.get("guild_id") == interaction.guild.id
            and record.get("owner_id") == interaction.user.id
            and record.get("open", True)
        ]

        if existing:
            channel = interaction.guild.get_channel(
                int(existing[0]["channel_id"])
            )
            return await send_error(
                interaction,
                f"You already have an open ticket: "
                f"{channel.mention if channel else 'ticket'}",
            )

        await interaction.response.defer(ephemeral=True)

        category_id = int(
            DATA["ticket"].get("open_category", 0) or 0
        )
        category = (
            interaction.guild.get_channel(category_id)
            if category_id
            else None
        )

        if category is not None and not isinstance(
            category,
            discord.CategoryChannel,
        ):
            category = None

        me = interaction.guild.me

        overwrites = {
            interaction.guild.default_role:
                discord.PermissionOverwrite(view_channel=False),
            interaction.user:
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                ),
        }

        if me:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            )

        staff_role_id = int(
            DATA["ticket"].get("staff_role", 0) or 0
        )
        staff_role = (
            interaction.guild.get_role(staff_role_id)
            if staff_role_id
            else None
        )

        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
            )

        DATA["ticket"]["counter"] = (
            int(DATA["ticket"].get("counter", 0)) + 1
        )

        number = DATA["ticket"]["counter"]

        channel_name = safe_channel_name(
            f"ticket-{number}-{interaction.user.name}"
        )

        try:
            channel = await interaction.guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user}",
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "I need Manage Channels and correct role permissions "
                "to create tickets.",
                ephemeral=True,
            )

        reason = (
            DATA["ticket"]["reasons"][int(self.values[0])]
            if DATA["ticket"].get("reasons")
            else {"label": "Support", "description": "General support"}
        )

        DATA["ticket"]["tickets"][str(channel.id)] = {
            "channel_id": channel.id,
            "guild_id": interaction.guild.id,
            "owner_id": interaction.user.id,
            "reason": reason.get("label", "Support"),
            "open": True,
            "created_at": discord.utils.utcnow().isoformat(),
        }

        save_data()

        embed = discord.Embed(
            title=f"🎫 Ticket • {reason.get('label', 'Support')}",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                f"**Reason:** "
                f"{reason.get('description', reason.get('label', 'Support'))}\n\n"
                "A member of the support team will be with you shortly."
            ),
            color=0x5865F2,
        )

        embed.set_footer(
            text=f"Ticket #{number} • {interaction.guild.name}"
        )

        await channel.send(
            content=interaction.user.mention,
            embed=embed,
            view=TicketControlView(),
        )

        await ticket_log(
            interaction.guild,
            "🎫 Ticket Opened",
            f"{channel.mention} opened by {interaction.user.mention}\n"
            f"Reason: **{reason.get('label', 'Support')}**",
            0x57F287,
        )

        await interaction.followup.send(
            f"Ticket created: {channel.mention} ✅",
            ephemeral=True,
        )


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect())


async def close_ticket_channel(interaction: discord.Interaction):
    channel = interaction.channel
    rec = ticket_record(channel.id) if channel else None

    if not rec:
        return await send_error(
            interaction,
            "This is not a registered ticket.",
        )

    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    closed_id = int(
        DATA["ticket"].get("closed_category", 0) or 0
    )
    category = (
        interaction.guild.get_channel(closed_id)
        if closed_id
        else None
    )

    owner = interaction.guild.get_member(rec["owner_id"])

    if owner:
        await channel.set_permissions(
            owner,
            view_channel=False,
            send_messages=False,
            reason="Ticket closed",
        )

    if category:
        await channel.edit(
            category=category,
            name=safe_channel_name(
                f"closed-{channel.name.replace('ticket-', '')}"
            ),
            reason="Ticket closed",
        )
    else:
        await channel.edit(
            name=safe_channel_name(
                f"closed-{channel.name.replace('ticket-', '')}"
            ),
            reason="Ticket closed",
        )

    rec["open"] = False
    rec["closed_at"] = discord.utils.utcnow().isoformat()
    save_data()

    await channel.send(
        "🔒 **Ticket closed.** Staff can reopen it below.",
        view=TicketControlView(),
    )

    await ticket_log(
        interaction.guild,
        "🔒 Ticket Closed",
        f"{channel.mention} closed by {interaction.user.mention}",
        0xFEE75C,
    )

    await interaction.followup.send(
        "Ticket closed successfully. 🔒",
        ephemeral=True,
    )


async def reopen_ticket_channel(interaction: discord.Interaction):
    channel = interaction.channel
    rec = ticket_record(channel.id) if channel else None

    if not rec:
        return await send_error(
            interaction,
            "This is not a registered ticket.",
        )

    owner = interaction.guild.get_member(rec["owner_id"])

    if owner:
        await channel.set_permissions(
            owner,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )

    open_id = int(
        DATA["ticket"].get("open_category", 0) or 0
    )

    category = (
        interaction.guild.get_channel(open_id)
        if open_id
        else None
    )

    try:
        if category:
            await channel.edit(
                category=category,
                name=safe_channel_name(
                    f"ticket-{channel.name.replace('closed-', '')}"
                ),
                reason="Ticket reopened",
            )
        else:
            await channel.edit(
                name=safe_channel_name(
                    f"ticket-{channel.name.replace('closed-', '')}"
                ),
                reason="Ticket reopened",
            )

        rec["open"] = True
        rec["reopened_at"] = discord.utils.utcnow().isoformat()
        save_data()

        await interaction.response.send_message(
            "Ticket reopened. 🔓",
            ephemeral=True,
        )

        await channel.send("🔓 **Ticket reopened.**")

        await ticket_log(
            interaction.guild,
            "🔓 Ticket Reopened",
            f"{channel.mention} reopened by {interaction.user.mention}",
            0x57F287,
        )
    except Exception as exc:
        await send_error(
            interaction,
            f"Failed to reopen ticket: {exc}",
        )


async def ticket_log(
    guild: discord.Guild,
    title: str,
    description: str,
    color: int = 0x5865F2,
):
    channel_id = int(
        DATA["ticket"].get("logs_channel", 0) or 0
    )

    channel = guild.get_channel(channel_id)

    if not isinstance(channel, discord.TextChannel):
        return

    try:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(embed=embed)
    except Exception as exc:
        print(f"[TICKET LOG] {exc}")


# ============================================================
# BOT
# ============================================================

class ProBot(commands.Bot):
    async def setup_hook(self):
        # Persistent views survive bot process restarts.
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        print("[BOT] Persistent ticket views registered.")


bot = ProBot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user} ({bot.user.id})")

    try:
        synced = await bot.tree.sync()
        print(f"[BOT] Synced {len(synced)} application commands.")
    except Exception as exc:
        print(f"[SYNC] {exc}")

    await apply_saved_presence()

    for guild in bot.guilds:
        await update_member_count_channel(guild)

        # Automatically restore the permanent VC after restart.
        if DATA.get("deafen", True):
            await ensure_forever_voice(guild)


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id in DATA.get("blacklist_servers", []):
        try:
            await member.ban(reason="Server is blacklisted.")
        except Exception:
            pass
        return

    if CUSTOM_WELCOME_TEXT:
        text = (
            CUSTOM_WELCOME_TEXT
            .replace("{user}", member.mention)
            .replace("{server}", member.guild.name)
        )

        try:
            await member.send(text)
        except Exception:
            pass

    await update_member_count_channel(member.guild)


@bot.event
async def on_member_remove(member: discord.Member):
    await update_member_count_channel(member.guild)


# ============================================================
# EXISTING RENAME APPROVAL SYSTEM
# ============================================================

class RenameApprovalView(discord.ui.View):
    def __init__(self, member: discord.Member, new_nickname: str):
        super().__init__(timeout=None)
        self.member = member
        self.new_nickname = new_nickname

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
        if not interaction.user.guild_permissions.manage_nicknames:
            return await send_error(
                interaction,
                "You need Manage Nicknames.",
            )

        try:
            await self.member.edit(
                nick=self.new_nickname,
                reason=f"Nickname request approved by {interaction.user}",
            )

            for child in self.children:
                child.disabled = True

            await interaction.message.edit(view=self)

            await interaction.response.send_message(
                f"Nickname change approved for {self.member.mention}. ✅",
                ephemeral=True,
            )

            try:
                await self.member.send(
                    f"Your nickname request in **{interaction.guild.name}** "
                    "was accepted. 🎉"
                )
            except Exception:
                pass

        except Exception as exc:
            await send_error(
                interaction,
                f"Error applying nickname: {exc}",
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
        if not interaction.user.guild_permissions.manage_nicknames:
            return await send_error(
                interaction,
                "You need Manage Nicknames.",
            )

        for child in self.children:
            child.disabled = True

        await interaction.message.edit(view=self)

        await interaction.response.send_message(
            f"Nickname change rejected for {self.member.mention}. ❌",
            ephemeral=True,
        )


# ============================================================
# EXISTING MESSAGE SYSTEMS
# ============================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Bad words filter.
    bad_words = DATA.get("bad_words", {})
    content_lower = message.content.lower()

    for word, reply_msg in bad_words.items():
        if word.lower() in content_lower:
            try:
                await message.delete()

                if reply_msg:
                    await message.channel.send(
                        f"{message.author.mention} {reply_msg}",
                        delete_after=5,
                    )

                if action_category_allowed(
                    message.guild,
                    "delete_message",
                    message.channel,
                ):
                    await apply_message_punishment(message)

            except Exception:
                pass

            return

    # Rename request channel.
    if (
        RENAME_REQUEST_CHANNEL_ID
        and message.channel.id == RENAME_REQUEST_CHANNEL_ID
    ):
        new_nickname = message.content.strip()

        try:
            await message.delete()
        except Exception:
            pass

        if len(new_nickname) > 32:
            try:
                await message.channel.send(
                    f"{message.author.mention}, nickname must be "
                    "32 characters or fewer!",
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
                    f"**Requested Nickname:** `{new_nickname}`"
                ),
                color=0xF1C40F,
            )

            await message.channel.send(
                embed=embed,
                view=RenameApprovalView(
                    message.author,
                    new_nickname,
                ),
            )

        except Exception as exc:
            print(f"[RENAME] {exc}")

        return

    # Existing color role system.
    if (
        COLOR_CHANNEL_ID
        and message.channel.id == COLOR_CHANNEL_ID
        and message.guild
    ):
        await handle_color_role(message)
        return

    if TARGET_USER_ID and any(
        user.id == TARGET_USER_ID
        for user in message.mentions
    ):
        try:
            await message.add_reaction(TARGET_EMOJI)
        except Exception:
            pass

    await bot.process_commands(message)


async def handle_color_role(message: discord.Message):
    try:
        cleaned = (
            message.content.strip()
            .lower()
            .replace("#", "")
        )

        color_map = {
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

        color_int = color_map.get(cleaned)

        if color_int is None:
            color_int = valid_hex(cleaned)
            role_name = f"#{cleaned.upper()}"
        else:
            role_name = cleaned.capitalize()

        guild = message.guild
        member = message.author
        me = guild.me

        if not me.guild_permissions.manage_roles:
            return

        role = discord.utils.get(
            guild.roles,
            name=role_name,
        )

        if role and role >= me.top_role:
            return

        if not role:
            role = await guild.create_role(
                name=role_name,
                color=discord.Color(color_int),
                reason="Color role requested.",
            )

        # Remove previous generated color roles.
        generated_names = {
            name.capitalize()
            for name in color_map
        }

        roles_to_remove = [
            role_obj
            for role_obj in member.roles
            if role_obj.name in generated_names
            or role_obj.name.startswith("#")
        ]

        if roles_to_remove:
            await member.remove_roles(
                *roles_to_remove,
                reason="Replacing color role.",
            )

        await member.add_roles(
            role,
            reason="Color role system.",
        )

    except Exception as exc:
        print(f"[COLOR] {exc}")


# ============================================================
# SENDHERE
# ============================================================

async def collect_files(*attachments):
    files = []

    for attachment in attachments:
        if attachment:
            files.append(await attachment.to_file())

    return files


@bot.tree.command(
    name="sendhere",
    description="Send a message with optional files here.",
)
async def sendhere(
    interaction: discord.Interaction,
    message: str = "",
    file1: discord.Attachment = None,
    file2: discord.Attachment = None,
    file3: discord.Attachment = None,
    file4: discord.Attachment = None,
    file5: discord.Attachment = None,
    file6: discord.Attachment = None,
    file7: discord.Attachment = None,
    file8: discord.Attachment = None,
    file9: discord.Attachment = None,
    file10: discord.Attachment = None,
    file11: discord.Attachment = None,
    file12: discord.Attachment = None,
    file13: discord.Attachment = None,
    file14: discord.Attachment = None,
):
    err = check_command(interaction, "manage_messages")

    if err:
        return await send_error(interaction, err)

    attachments = [
        file1, file2, file3, file4, file5, file6, file7,
        file8, file9, file10, file11, file12, file13, file14,
    ]

    if not message and not any(attachments):
        return await send_error(
            interaction,
            "Provide a message or at least one file.",
        )

    await interaction.response.defer(ephemeral=True)

    try:
        files = await collect_files(*attachments)

        await interaction.channel.send(
            content=message or None,
            files=files or None,
        )

        await interaction.followup.send(
            "Sent successfully. ✅",
            ephemeral=True,
        )

    except Exception as exc:
        await interaction.followup.send(
            f"Failed to send: {exc}",
            ephemeral=True,
        )


# ============================================================
# EMBED - FILE REPLACES IMAGE URL
# ============================================================

@bot.tree.command(
    name="embed",
    description="Send an embed with an optional uploaded file.",
)
async def embed_command(
    interaction: discord.Interaction,
    title: str,
    description: str,
    color: str = "5865F2",
    file: discord.Attachment = None,
    channel: discord.TextChannel = None,
):
    err = check_command(interaction, "administrator")

    if err:
        return await send_error(interaction, err)

    await interaction.response.defer(ephemeral=True)

    try:
        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=valid_hex(color),
        )

        discord_file = None

        if file:
            discord_file = await file.to_file()

            if (
                file.content_type
                and file.content_type.startswith("image/")
            ):
                embed.set_image(
                    url=f"attachment://{file.filename}"
                )

        target = channel or interaction.channel

        await target.send(
            embed=embed,
            file=discord_file,
        )

        await interaction.followup.send(
            f"Embed sent to {target.mention}. ✅",
            ephemeral=True,
        )

    except Exception as exc:
        await interaction.followup.send(
            f"Failed to send embed: {exc}",
            ephemeral=True,
        )


# ============================================================
# STATUS - FIXED / PERSISTENT
# ============================================================

def build_activity(
    activity_type: str,
    text: str,
    stream_url: Optional[str] = None,
):
    if activity_type == "playing":
        return discord.Game(name=text)

    if activity_type == "listening":
        return discord.Activity(
            type=discord.ActivityType.listening,
            name=text,
        )

    if activity_type == "watching":
        return discord.Activity(
            type=discord.ActivityType.watching,
            name=text,
        )

    if activity_type == "competing":
        return discord.Activity(
            type=discord.ActivityType.competing,
            name=text,
        )

    if activity_type == "streaming":
        return discord.Streaming(
            name=text,
            url=stream_url or "https://www.twitch.tv/discord",
        )

    return discord.Game(name=text)


async def apply_saved_presence():
    cfg = DATA["status"]

    try:
        status_name = cfg.get("status", "online")

        if status_name == "invisible":
            status_name = "offline"

        status = getattr(
            discord.Status,
            status_name,
            discord.Status.online,
        )

        activity = build_activity(
            cfg.get("activity_type", "playing"),
            cfg.get("text", "Active"),
            cfg.get("stream_url"),
        )

        await bot.change_presence(
            status=status,
            activity=activity,
        )

    except Exception as exc:
        print(f"[STATUS] {exc}")


@bot.tree.command(
    name="status",
    description="Set the bot status/activity.",
)
@app_commands.choices(
    activity_type=[
        app_commands.Choice(name="Playing", value="playing"),
        app_commands.Choice(name="Streaming", value="streaming"),
        app_commands.Choice(name="Listening", value="listening"),
        app_commands.Choice(name="Watching", value="watching"),
        app_commands.Choice(name="Competing", value="competing"),
    ],
    status_type=[
        app_commands.Choice(name="Online", value="online"),
        app_commands.Choice(name="Idle", value="idle"),
        app_commands.Choice(name="Do Not Disturb", value="dnd"),
        app_commands.Choice(name="Invisible", value="invisible"),
    ],
)
async def status(
    interaction: discord.Interaction,
    activity_type: app_commands.Choice[str] = None,
    status_type: app_commands.Choice[str] = None,
    text: str = None,
    stream_url: str = None,
):
    err = check_command(interaction, "administrator")

    if err:
        return await send_error(interaction, err)

    cfg = DATA["status"]

    if activity_type:
        cfg["activity_type"] = activity_type.value

    if status_type:
        cfg["status"] = status_type.value

    if text is not None:
        cfg["text"] = text[:128]

    if stream_url is not None:
        cfg["stream_url"] = stream_url

    save_data()

    await apply_saved_presence()

    await interaction.response.send_message(
        "Status updated and saved for future restarts. ✅",
        ephemeral=True,
    )


# ============================================================
# VOICE: JOINVC / DEAFEN / LEAVEVC
# ============================================================

async def connect_voice(
    channel: discord.VoiceChannel,
    self_deaf: bool = True,
):
    vc = channel.guild.voice_client

    if vc and vc.is_connected():
        if vc.channel.id != channel.id:
            await vc.move_to(channel)
    else:
        vc = await channel.connect(
            self_deaf=self_deaf,
            reconnect=True,
        )

    if self_deaf:
        try:
            await channel.guild.change_voice_state(
                channel=channel,
                self_deaf=True,
            )
        except Exception:
            pass

    return vc


async def ensure_forever_voice(guild: discord.Guild):
    channel = guild.get_channel(FOREVER_VOICE_CHANNEL_ID)

    if not isinstance(channel, discord.VoiceChannel):
        return

    try:
        await connect_voice(
            channel,
            self_deaf=DATA.get("deafen", True),
        )

        VOICE_LOCKS.add(guild.id)

    except Exception as exc:
        print(f"[FOREVER VC] {guild.name}: {exc}")


@bot.tree.command(
    name="joinvc",
    description="Choose a voice channel for the bot to join.",
)
async def joinvc(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
):
    err = check_command(interaction, "administrator")

    if err:
        return await send_error(interaction, err)

    await interaction.response.defer(ephemeral=True)

    try:
        await connect_voice(
            channel,
            self_deaf=DATA.get("deafen", True),
        )

        await interaction.followup.send(
            f"Joined **{channel.name}**. 🔊",
            ephemeral=True,
        )

    except Exception as exc:
        await interaction.followup.send(
            f"Voice connection failed: {exc}",
            ephemeral=True,
        )


@bot.tree.command(
    name="deafen",
    description="Keep the bot self-deafened in voice.",
)
async def deafen(
    interaction: discord.Interaction,
    enabled: bool = True,
):
    err = check_command(interaction, "administrator")

    if err:
        return await send_error(interaction, err)

    DATA["deafen"] = enabled
    save_data()

    vc = interaction.guild.voice_client

    if vc and vc.channel:
        try:
            await interaction.guild.change_voice_state(
                channel=vc.channel,
                self_deaf=enabled,
            )
        except Exception:
            pass

    await interaction.response.send_message(
        f"Permanent bot self-deafen is now "
        f"**{'enabled' if enabled else 'disabled'}**. "
        f"{'🔇' if enabled else '🔊'}",
        ephemeral=True,
    )


@bot.tree.command(
    name="leavevc",
    description="Make the bot leave voice.",
)
async def leavevc(interaction: discord.Interaction):
    err = check_command(interaction, "administrator")

    if err:
        return await send_error(interaction, err)

    VOICE_LOCKS.discard(interaction.guild.id)

    vc = interaction.guild.voice_client

    if vc:
        await vc.disconnect(force=True)

    await interaction.response.send_message(
        "Left voice. 👋",
        ephemeral=True,
    )


@bot.tree.command(
    name="voiceinfo",
    description="Show current voice connection.",
)
async def voiceinfo(interaction: discord.Interaction):
    vc = interaction.guild.voice_client

    if vc and vc.channel:
        await interaction.response.send_message(
            f"Connected to **{vc.channel.name}** "
            f"(`{vc.channel.id}`) • "
            f"members: {len(vc.channel.members)} 🔊",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "Not connected to voice.",
            ephemeral=True,
        )


@bot.event
async def on_voice_state_update(member, before, after):
    if bot.user is None or member.id != bot.user.id:
        return

    guild = member.guild

    if guild.id not in VOICE_LOCKS:
        return

    if after.channel is None:
        await asyncio.sleep(2)
        await ensure_forever_voice(guild)

    elif after.channel.id == FOREVER_VOICE_CHANNEL_ID:
        if DATA.get("deafen", True):
            try:
                await guild.change_voice_state(
                    channel=after.channel,
                    self_deaf=True,
                )
            except Exception:
                pass


# ============================================================
# MUSIC - YOUTUBE URL OR SEARCH NAME
# ============================================================

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn",
}


async def resolve_youtube(query: str):
    if yt_dlp is None:
        raise RuntimeError(
            "yt-dlp is not installed. Run pip install -r requirements.txt."
        )

    def extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(
                query,
                download=False,
            )

            if "entries" in info:
                info = info["entries"][0]

            return {
                "title": info.get("title", "Unknown"),
                "stream_url": info["url"],
                "webpage_url": info.get(
                    "webpage_url",
                    query,
                ),
            }

    return await asyncio.to_thread(extract)


async def music_idle_leave(guild_id: int):
    # Requested 5-minute idle timeout.
    await asyncio.sleep(300)

    state = MUSIC_STATE.get(guild_id)

    if state and not state.get("playing"):
        guild = bot.get_guild(guild_id)

        if guild:
            vc = guild.voice_client

            if vc and vc.is_connected():
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass

            MUSIC_STATE.pop(guild_id, None)

            # Return to the permanent voice channel.
            forever = guild.get_channel(
                FOREVER_VOICE_CHANNEL_ID
            )

            if isinstance(forever, discord.VoiceChannel):
                try:
                    await connect_voice(
                        forever,
                        self_deaf=DATA.get("deafen", True),
                    )
                    VOICE_LOCKS.add(guild.id)
                except Exception as exc:
                    print(
                        f"[MUSIC -> FOREVER VC] {exc}"
                    )


def after_music(guild_id: int, error):
    if error:
        print(f"[MUSIC] Playback error: {error}")

    state = MUSIC_STATE.get(guild_id)

    if state:
        state["playing"] = False

    old_task = MUSIC_IDLE_TASKS.get(guild_id)

    if old_task and not old_task.done():
        old_task.cancel()

    MUSIC_IDLE_TASKS[guild_id] = (
        bot.loop.create_task(
            music_idle_leave(guild_id)
        )
    )


@bot.tree.command(
    name="play",
    description="Play a YouTube link or search YouTube by song name.",
)
async def play(
    interaction: discord.Interaction,
    query: str,
):
    if not interaction.guild:
        return await send_error(
            interaction,
            "Use this command in a server.",
        )

    if (
        not isinstance(interaction.user, discord.Member)
        or not interaction.user.voice
        or not interaction.user.voice.channel
    ):
        return await send_error(
            interaction,
            "Join a voice channel first.",
        )

    await interaction.response.defer(ephemeral=True)

    try:
        me = interaction.guild.me

        if not me.guild_permissions.connect:
            return await interaction.followup.send(
                "I need the Connect permission.",
                ephemeral=True,
            )

        if not me.guild_permissions.speak:
            return await interaction.followup.send(
                "I need the Speak permission.",
                ephemeral=True,
            )

        user_channel = interaction.user.voice.channel

        # During music, /play follows the user.
        VOICE_LOCKS.discard(interaction.guild.id)

        vc = await connect_voice(
            user_channel,
            self_deaf=DATA.get("deafen", True),
        )

        result = await resolve_youtube(query)

        if vc.is_playing():
            vc.stop()

        source = await discord.FFmpegOpusAudio.from_probe(
            result["stream_url"],
            **FFMPEG_OPTIONS,
        )

        vc.play(
            source,
            after=lambda error: after_music(
                interaction.guild.id,
                error,
            ),
        )

        MUSIC_STATE[interaction.guild.id] = {
            "playing": True,
            "title": result["title"],
        }

        old_task = MUSIC_IDLE_TASKS.get(
            interaction.guild.id
        )

        if old_task and not old_task.done():
            old_task.cancel()

        await interaction.followup.send(
            f"▶️ Now playing: **{result['title']}**",
            ephemeral=True,
        )

    except Exception as exc:
        await interaction.followup.send(
            f"Music error: {exc}\n"
            "Make sure FFmpeg is installed on the host.",
            ephemeral=True,
        )


# ============================================================
# MODERATION
# ============================================================

@bot.tree.command(name="warn", description="Warn a member.")
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    err = check_command(interaction, "manage_messages")

    if err:
        return await send_error(interaction, err)

    ok, why = bot_can_act_on(
        interaction.guild,
        member,
        "timeout",
    )

    if not ok:
        return await send_error(interaction, why)

    warns = DATA["warns"].setdefault(
        str(member.id),
        [],
    )

    warns.append(
        {
            "guild_id": interaction.guild.id,
            "reason": reason[:500],
            "moderator_id": interaction.user.id,
            "timestamp": discord.utils.utcnow().isoformat(),
        }
    )

    save_data()

    try:
        await member.send(
            f"You were warned in **{interaction.guild.name}**.\n"
            f"Reason: {reason}"
        )
    except Exception:
        pass

    await interaction.response.send_message(
        f"Warned {member.mention}. ⚠️",
        ephemeral=True,
    )


@bot.tree.command(
    name="check_warns",
    description="View warnings for a member.",
)
async def check_warns(
    interaction: discord.Interaction,
    member: discord.Member,
):
    err = check_command(
        interaction,
        "manage_messages",
    )

    if err:
        return await send_error(interaction, err)

    items = [
        item
        for item in DATA["warns"].get(
            str(member.id),
            [],
        )
        if item.get("guild_id") == interaction.guild.id
    ]

    if not items:
        return await interaction.response.send_message(
            f"{member.mention} has no recorded warnings.",
            ephemeral=True,
        )

    text = "\n".join(
        f"• {index + 1}. {item['reason']}"
        for index, item in enumerate(items[-10:])
    )

    await interaction.response.send_message(
        f"Warnings for {member.mention}:\n{text}",
        ephemeral=True,
    )


@bot.tree.command(
    name="clear_warns",
    description="Clear warnings for a member.",
)
async def clear_warns(
    interaction: discord.Interaction,
    member: discord.Member,
):
    err = check_command(
        interaction,
        "manage_messages",
    )

    if err:
        return await send_error(interaction, err)

    DATA["warns"].pop(str(member.id), None)
    save_data()

    await interaction.response.send_message(
        "Warnings cleared. ✅",
        ephemeral=True,
    )


@bot.tree.command(
    name="mute",
    description="Timeout a member for minutes.",
)
async def mute(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: int = 10,
    reason: str = "No reason provided",
):
    err = check_command(
        interaction,
        "moderate_members",
    )

    if err:
        return await send_error(interaction, err)

    if minutes < 1 or minutes > 40320:
        return await send_error(
            interaction,
            "Minutes must be between 1 and 40320.",
        )

    ok, why = bot_can_act_on(
        interaction.guild,
        member,
        "timeout",
    )

    if not ok:
        return await send_error(interaction, why)

    try:
        await member.timeout(
            timedelta(minutes=minutes),
            reason=reason,
        )

        await interaction.response.send_message(
            f"Timed out {member.mention} "
            f"for {minutes} minutes. 🔇",
            ephemeral=True,
        )

    except Exception as exc:
        await send_error(
            interaction,
            f"Timeout failed: {exc}",
        )


@bot.tree.command(
    name="unmute",
    description="Remove timeout from one member.",
)
async def unmute(
    interaction: discord.Interaction,
    member: discord.Member,
):
    err = check_command(
        interaction,
        "moderate_members",
    )

    if err:
        return await send_error(interaction, err)

    ok, why = bot_can_act_on(
        interaction.guild,
        member,
        "timeout",
    )

    if not ok:
        return await send_error(interaction, why)

    try:
        await member.timeout(
            None,
            reason=f"Timeout removed by {interaction.user}",
        )

        await interaction.response.send_message(
            f"Timeout removed from {member.mention}. ✅",
            ephemeral=True,
        )

    except Exception as exc:
        await send_error(
            interaction,
            f"Failed: {exc}",
        )


@bot.tree.command(
    name="untimeoutall",
    description="Remove timeout from every member the bot can moderate.",
)
async def untimeoutall(interaction: discord.Interaction):
    err = check_command(
        interaction,
        "moderate_members",
    )

    if err:
        return await send_error(interaction, err)

    await interaction.response.defer(
        ephemeral=True
    )

    count = 0
    skipped = 0

    for member in interaction.guild.members:
        if not member.is_timed_out():
            continue

        ok, _ = bot_can_act_on(
            interaction.guild,
            member,
            "timeout",
        )

        if not ok:
            skipped += 1
            continue

        try:
            await member.timeout(
                None,
                reason=f"Global untimeout by {interaction.user}",
            )
            count += 1
        except Exception:
            skipped += 1

        await asyncio.sleep(0.15)

    await interaction.followup.send(
        f"Untimeout complete: **{count}** removed, "
        f"**{skipped}** skipped. ✅",
        ephemeral=True,
    )


@bot.tree.command(
    name="kick",
    description="Kick a member.",
)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    err = check_command(
        interaction,
        "kick_members",
    )

    if err:
        return await send_error(interaction, err)

    ok, why = bot_can_act_on(
        interaction.guild,
        member,
        "kick",
    )

    if not ok:
        return await send_error(interaction, why)

    try:
        await member.kick(reason=reason)

        await interaction.response.send_message(
            f"Kicked {member.mention}. 👢",
            ephemeral=True,
        )

    except Exception as exc:
        await send_error(
            interaction,
            f"Kick failed: {exc}",
        )


@bot.tree.command(
    name="ban",
    description="Ban a member.",
)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    err = check_command(
        interaction,
        "ban_members",
    )

    if err:
        return await send_error(interaction, err)

    ok, why = bot_can_act_on(
        interaction.guild,
        member,
        "ban",
    )

    if not ok:
        return await send_error(interaction, why)

    try:
        await member.ban(reason=reason)

        await interaction.response.send_message(
            f"Banned {member.mention}. 🔨",
            ephemeral=True,
        )

    except Exception as exc:
        await send_error(
            interaction,
            f"Ban failed: {exc}",
        )


@bot.tree.command(
    name="unbanall",
    description="Unban every banned user.",
)
async def unbanall(interaction: discord.Interaction):
    err = check_command(
        interaction,
        "ban_members",
    )

    if err:
        return await send_error(interaction, err)

    await interaction.response.defer(
        ephemeral=True
    )

    count = 0
    failed = 0

    try:
        async for entry in interaction.guild.bans(
            limit=None
        ):
            try:
                await interaction.guild.unban(
                    entry.user,
                    reason=f"Unban all by {interaction.user}",
                )
                count += 1
                await asyncio.sleep(0.2)
            except Exception:
                failed += 1

        await interaction.followup.send(
            f"Unban all complete: **{count}** removed, "
            f"**{failed}** failed. ✅",
            ephemeral=True,
        )

    except Exception as exc:
        await interaction.followup.send(
            f"Unban all failed: {exc}",
            ephemeral=True,
        )


# ============================================================
# CHANNEL LOCK
# ============================================================

@bot.tree.command(
    name="lock",
    description="Lock a text channel.",
)
async def lock(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
):
    err = check_command(
        interaction,
        "manage_channels",
    )

    if err:
        return await send_error(interaction, err)

    target = channel or interaction.channel

    try:
        await target.set_permissions(
            interaction.guild.default_role,
            send_messages=False,
            reason=f"Locked by {interaction.user}",
        )

        await interaction.response.send_message(
            f"Locked {target.mention}. 🔒",
            ephemeral=True,
        )

    except Exception as exc:
        await send_error(
            interaction,
            f"Failed: {exc}",
        )


# ============================================================
# TICKET COMMANDS
# ============================================================

@bot.tree.command(
    name="sendpanel",
    description="Create a professional ticket panel.",
)
async def sendpanel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str = "Support Tickets",
    description: str = "Choose a reason below to open a ticket.",
    color: str = "5865F2",
    file: discord.Attachment = None,
    reason1: str = "Support:General assistance",
    reason2: str = None,
    reason3: str = None,
    reason4: str = None,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    await interaction.response.defer(
        ephemeral=True
    )

    try:
        reasons = []

        for raw in (
            reason1,
            reason2,
            reason3,
            reason4,
        ):
            if not raw:
                continue

            label, _, desc = raw.partition(":")

            label = label.strip()
            desc = desc.strip() or label

            if label:
                reasons.append(
                    {
                        "label": label[:100],
                        "description": desc[:100],
                    }
                )

        DATA["ticket"]["reasons"] = (
            reasons
            or DEFAULT_DATA["ticket"]["reasons"]
        )

        save_data()

        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=valid_hex(color),
        )

        if file:
            discord_file = await file.to_file()

            if (
                file.content_type
                and file.content_type.startswith("image/")
            ):
                embed.set_image(
                    url=f"attachment://{file.filename}"
                )

            msg = await channel.send(
                embed=embed,
                file=discord_file,
                view=TicketPanelView(),
            )
        else:
            msg = await channel.send(
                embed=embed,
                view=TicketPanelView(),
            )

        DATA["ticket"]["panel_message_id"] = msg.id
        DATA["ticket"]["panel_channel_id"] = channel.id

        save_data()

        await interaction.followup.send(
            f"Ticket panel created in {channel.mention}. ✅",
            ephemeral=True,
        )

    except Exception as exc:
        await interaction.followup.send(
            f"Panel creation failed: {exc}",
            ephemeral=True,
        )


@bot.tree.command(
    name="create",
    description="Create an opened-ticket embed message.",
)
async def create(
    interaction: discord.Interaction,
    title: str = "Ticket",
    description: str = "Your ticket is now open.",
    name: str = "ticket",
    file: discord.Attachment = None,
):
    err = check_command(
        interaction,
        "manage_messages",
    )

    if err:
        return await send_error(interaction, err)

    if not ticket_record(interaction.channel.id):
        return await send_error(
            interaction,
            "Use this command inside a ticket.",
        )

    await interaction.response.defer(
        ephemeral=True
    )

    embed = discord.Embed(
        title=title.replace(
            "{username}",
            interaction.user.display_name,
        )[:256],
        description=description.replace(
            "{username}",
            interaction.user.mention,
        )[:4096],
        color=0x5865F2,
    )

    embed.set_footer(
        text=name.replace(
            "{username}",
            interaction.user.name,
        )[:90]
    )

    discord_file = None

    if file:
        discord_file = await file.to_file()

        if (
            file.content_type
            and file.content_type.startswith("image/")
        ):
            embed.set_image(
                url=f"attachment://{file.filename}"
            )

    await interaction.channel.send(
        embed=embed,
        file=discord_file,
        view=TicketControlView(),
    )

    await interaction.followup.send(
        "Opened ticket message created. ✅",
        ephemeral=True,
    )


@bot.tree.command(
    name="setlogs",
    description="Set the ticket logs channel.",
)
async def setlogs(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    DATA["ticket"]["logs_channel"] = channel.id
    save_data()

    await interaction.response.send_message(
        f"Ticket logs channel set to {channel.mention}. ✅",
        ephemeral=True,
    )


@bot.tree.command(
    name="setticketcategory",
    description="Set the open or closed ticket category.",
)
@app_commands.choices(
    kind=[
        app_commands.Choice(
            name="Open",
            value="open",
        ),
        app_commands.Choice(
            name="Closed",
            value="closed",
        ),
    ]
)
async def setticketcategory(
    interaction: discord.Interaction,
    kind: app_commands.Choice[str],
    category: discord.CategoryChannel,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    DATA["ticket"][
        f"{kind.value}_category"
    ] = category.id

    save_data()

    await interaction.response.send_message(
        f"{kind.name} ticket category set to "
        f"{category.mention}. ✅",
        ephemeral=True,
    )


@bot.tree.command(
    name="setstaffrole",
    description="Set the ticket staff role.",
)
async def setstaffrole(
    interaction: discord.Interaction,
    role: discord.Role,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    DATA["ticket"]["staff_role"] = role.id
    save_data()

    await interaction.response.send_message(
        f"Ticket staff role set to {role.mention}. ✅",
        ephemeral=True,
    )


@bot.tree.command(
    name="close",
    description="Close the current ticket.",
)
async def close(interaction: discord.Interaction):
    if not ticket_record(interaction.channel.id):
        return await send_error(
            interaction,
            "This command can only be used inside a ticket.",
        )

    if not await ticket_is_manager(interaction):
        return await send_error(
            interaction,
            "You are not allowed to close this ticket.",
        )

    await close_ticket_channel(interaction)


@bot.tree.command(
    name="reopen",
    description="Reopen the current ticket.",
)
async def reopen(interaction: discord.Interaction):
    if not ticket_record(interaction.channel.id):
        return await send_error(
            interaction,
            "This command can only be used inside a ticket.",
        )

    if not await ticket_is_manager(interaction):
        return await send_error(
            interaction,
            "You are not allowed to reopen this ticket.",
        )

    await reopen_ticket_channel(interaction)


@bot.tree.command(
    name="delete",
    description="Delete the current ticket.",
)
async def delete_ticket(interaction: discord.Interaction):
    if not ticket_record(interaction.channel.id):
        return await send_error(
            interaction,
            "This is not a registered ticket.",
        )

    if not staff_can_manage_ticket(interaction):
        return await send_error(
            interaction,
            "Only ticket staff can delete tickets.",
        )

    await interaction.response.send_message(
        "Deleting ticket in 3 seconds...",
        ephemeral=True,
    )

    await asyncio.sleep(3)

    DATA["ticket"]["tickets"].pop(
        str(interaction.channel.id),
        None,
    )

    save_data()

    await interaction.channel.delete(
        reason=f"Ticket deleted by {interaction.user}"
    )


@bot.tree.command(
    name="purge",
    description="Delete all registered tickets in this server.",
)
async def purge(interaction: discord.Interaction):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    await interaction.response.defer(
        ephemeral=True
    )

    targets = [
        record
        for record in DATA["ticket"]["tickets"].values()
        if record.get("guild_id") == interaction.guild.id
    ]

    deleted = 0

    for record in targets:
        channel = interaction.guild.get_channel(
            int(record["channel_id"])
        )

        if channel:
            try:
                await channel.delete(
                    reason=f"Ticket purge by {interaction.user}"
                )
                deleted += 1
            except Exception:
                pass

    for record in targets:
        DATA["ticket"]["tickets"].pop(
            str(record["channel_id"]),
            None,
        )

    save_data()

    await interaction.followup.send(
        f"Ticket purge complete: **{deleted}** channels deleted. 🗑️",
        ephemeral=True,
    )


@bot.tree.command(
    name="add",
    description="Add a member to the current ticket.",
)
async def add(
    interaction: discord.Interaction,
    member: discord.Member,
):
    if not ticket_record(interaction.channel.id):
        return await send_error(
            interaction,
            "This is not a ticket.",
        )

    if not await ticket_is_manager(interaction):
        return await send_error(
            interaction,
            "You are not allowed to manage this ticket.",
        )

    try:
        await interaction.channel.set_permissions(
            member,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            reason=f"Added to ticket by {interaction.user}",
        )

        await interaction.response.send_message(
            f"Added {member.mention}. ✅",
            ephemeral=True,
        )

    except Exception as exc:
        await send_error(
            interaction,
            f"Failed: {exc}",
        )


@bot.tree.command(
    name="remove",
    description="Remove a member from the current ticket.",
)
async def remove(
    interaction: discord.Interaction,
    member: discord.Member,
):
    rec = ticket_record(interaction.channel.id)

    if not rec:
        return await send_error(
            interaction,
            "This is not a ticket.",
        )

    if not await ticket_is_manager(interaction):
        return await send_error(
            interaction,
            "You are not allowed to manage this ticket.",
        )

    if member.id == rec.get("owner_id"):
        return await send_error(
            interaction,
            "You cannot remove the ticket owner.",
        )

    try:
        await interaction.channel.set_permissions(
            member,
            overwrite=None,
            reason=f"Removed from ticket by {interaction.user}",
        )

        await interaction.response.send_message(
            f"Removed {member.mention}. ✅",
            ephemeral=True,
        )

    except Exception as exc:
        await send_error(
            interaction,
            f"Failed: {exc}",
        )


@bot.tree.command(
    name="ticketinfo",
    description="Show current ticket information.",
)
async def ticketinfo(interaction: discord.Interaction):
    rec = ticket_record(interaction.channel.id)

    if not rec:
        return await send_error(
            interaction,
            "This is not a registered ticket.",
        )

    owner = interaction.guild.get_member(
        rec["owner_id"]
    )

    embed = discord.Embed(
        title="Ticket Information",
        color=0x5865F2,
    )

    embed.add_field(
        name="Owner",
        value=(
            owner.mention
            if owner
            else f"<@{rec['owner_id']}>"
        ),
        inline=False,
    )

    embed.add_field(
        name="Reason",
        value=rec.get("reason", "Support"),
    )

    embed.add_field(
        name="Status",
        value=(
            "Open"
            if rec.get("open")
            else "Closed"
        ),
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# ============================================================
# AUTOMESSAGE / WELCOME
# ============================================================

def parse_time(value: str) -> int:
    value = value.lower().strip()

    matches = re.findall(
        r"(\d+)\s*([smhd])",
        value,
    )

    if not matches:
        return int(value) if value.isdigit() else 0

    units = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }

    return sum(
        int(number) * units[unit]
        for number, unit in matches
    )


@bot.tree.command(
    name="automessage",
    description="Send a repeating message.",
)
async def automessage(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str,
    time: str,
    attachment: discord.Attachment = None,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    seconds = parse_time(time)

    if seconds < 5:
        return await send_error(
            interaction,
            "Interval must be at least 5 seconds.",
        )

    old_task = AUTOMESSAGE_TASKS.get(
        channel.id
    )

    if old_task:
        old_task.cancel()

    async def loop_task():
        while True:
            try:
                files = (
                    [await attachment.to_file()]
                    if attachment
                    else []
                )

                if channel.id in LAST_AUTOMESSAGE_ID:
                    try:
                        old_message = await channel.fetch_message(
                            LAST_AUTOMESSAGE_ID[channel.id]
                        )
                        await old_message.delete()
                    except Exception:
                        pass

                msg = await channel.send(
                    content=message,
                    files=files or None,
                )

                LAST_AUTOMESSAGE_ID[channel.id] = msg.id

            except Exception as exc:
                print(
                    f"[AUTOMESSAGE] {exc}"
                )

            await asyncio.sleep(seconds)

    AUTOMESSAGE_TASKS[channel.id] = (
        asyncio.create_task(loop_task())
    )

    DATA["automessages"][
        str(channel.id)
    ] = {
        "message": message,
        "time": time,
        "attachment_url": (
            attachment.url
            if attachment
            else None
        ),
    }

    save_data()

    await interaction.response.send_message(
        f"Automessage started in {channel.mention}. 🔄",
        ephemeral=True,
    )


@bot.tree.command(
    name="stopautomessage",
    description="Stop an automessage.",
)
async def stopautomessage(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    task = AUTOMESSAGE_TASKS.pop(
        channel.id,
        None,
    )

    if task:
        task.cancel()

    DATA["automessages"].pop(
        str(channel.id),
        None,
    )

    save_data()

    await interaction.response.send_message(
        "Automessage stopped. 🛑",
        ephemeral=True,
    )


@bot.tree.command(
    name="senddm",
    description="DM all non-bot members.",
)
async def senddm(
    interaction: discord.Interaction,
    message: str,
    attachment: discord.Attachment = None,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    await interaction.response.defer(
        ephemeral=True
    )

    sent = 0
    failed = 0

    for member in interaction.guild.members:
        if member.bot:
            continue

        try:
            files = (
                [await attachment.to_file()]
                if attachment
                else []
            )

            await member.send(
                content=message,
                files=files or None,
            )

            sent += 1

        except Exception:
            failed += 1

        await asyncio.sleep(0.5)

    await interaction.followup.send(
        f"DM complete: {sent} sent, {failed} failed.",
        ephemeral=True,
    )


@bot.tree.command(
    name="setwelcomemessage",
    description="Set the welcome DM.",
)
async def setwelcomemessage(
    interaction: discord.Interaction,
    message: str,
    attachment: discord.Attachment = None,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    global CUSTOM_WELCOME_TEXT

    CUSTOM_WELCOME_TEXT = message

    DATA["welcome"]["text"] = message
    DATA["welcome"]["attachment_url"] = (
        attachment.url
        if attachment
        else None
    )
    DATA["welcome"]["attachment_name"] = (
        attachment.filename
        if attachment
        else None
    )

    save_data()

    await interaction.response.send_message(
        "Welcome message saved. ✅",
        ephemeral=True,
    )


# ============================================================
# SECURITY / SETPUNISHMENT
# ============================================================

SECURITY_ACTIONS = [
    "delete_message",
    "timeout",
    "ban",
    "delete_channel",
    "create_channel",
    "delete_role",
    "create_role",
    "change_server_settings",
    "kick_member",
]


def action_category_allowed(
    guild: discord.Guild,
    action: str,
    channel=None,
) -> bool:
    category_id = int(
        DATA.get(
            "punishment_categories",
            {},
        ).get(action, 0)
        or 0
    )

    # No category restriction configured.
    if category_id == 0:
        return True

    if channel is None:
        return False

    return getattr(
        channel,
        "category_id",
        None,
    ) == category_id


async def apply_message_punishment(
    message: discord.Message,
):
    if (
        not message.guild
        or not isinstance(
            message.author,
            discord.Member,
        )
    ):
        return

    punishment = DATA[
        "security_punishments"
    ].get(
        "delete_message"
    )

    if punishment == "delete_message":
        return

    action = (
        punishment
        if punishment in (
            "kick",
            "ban",
            "timeout",
        )
        else "timeout"
    )

    ok, _ = bot_can_act_on(
        message.guild,
        message.author,
        action,
    )

    if not ok:
        return

    try:
        if punishment == "timeout":
            await message.author.timeout(
                timedelta(minutes=10),
                reason="Security: deleted message",
            )

        elif punishment == "kick":
            await message.author.kick(
                reason="Security: deleted message"
            )

        elif punishment == "ban":
            await message.author.ban(
                reason="Security: deleted message"
            )

        await security_log(
            message.guild,
            "🛡️ Security Punishment",
            f"User: {message.author.mention}\n"
            f"Action: `delete_message`\n"
            f"Punishment: `{punishment}`\n"
            f"Channel: {message.channel.mention}",
        )

    except Exception as exc:
        print(
            f"[MESSAGE PUNISHMENT] {exc}"
        )


@bot.tree.command(
    name="setpunishment",
    description="Set punishment and category for a security action.",
)
@app_commands.choices(
    action=[
        app_commands.Choice(
            name=x.replace("_", " ").title(),
            value=x,
        )
        for x in SECURITY_ACTIONS
    ],
    punishment=[
        app_commands.Choice(
            name="Timeout",
            value="timeout",
        ),
        app_commands.Choice(
            name="Kick",
            value="kick",
        ),
        app_commands.Choice(
            name="Ban",
            value="ban",
        ),
        app_commands.Choice(
            name="Delete message",
            value="delete_message",
        ),
    ],
)
async def setpunishment(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    punishment: app_commands.Choice[str],
    category: discord.CategoryChannel,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    DATA["security_punishments"][
        action.value
    ] = punishment.value

    DATA["punishment_categories"][
        action.value
    ] = category.id

    save_data()

    await interaction.response.send_message(
        f"**{action.name}** → **{punishment.name}**\n"
        f"Category: {category.mention}\n"
        "Saved permanently. ✅",
        ephemeral=True,
    )


@bot.tree.command(
    name="setlogssecurity",
    description="Set the security logs channel.",
)
async def setlogssecurity(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    DATA["security_logs_channel"] = (
        channel.id
        if channel
        else 0
    )

    save_data()

    await interaction.response.send_message(
        "Security logs "
        + (
            f"set to {channel.mention}."
            if channel
            else "disabled."
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="antinuke",
    description="Enable or disable antinuke mode.",
)
async def antinuke(
    interaction: discord.Interaction,
    status: bool = None,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    DATA["antinuke"] = (
        not DATA["antinuke"]
        if status is None
        else status
    )

    save_data()

    await interaction.response.send_message(
        f"Antinuke: **{DATA['antinuke']}**",
        ephemeral=True,
    )


@bot.tree.command(
    name="antiraid",
    description="Enable or disable antiraid mode.",
)
async def antiraid(
    interaction: discord.Interaction,
    status: bool = None,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    DATA["antiraid"] = (
        not DATA["antiraid"]
        if status is None
        else status
    )

    save_data()

    await interaction.response.send_message(
        f"Antiraid: **{DATA['antiraid']}**",
        ephemeral=True,
    )


@bot.tree.command(
    name="rudewordadd",
    description="Add a word to the chat filter.",
)
async def rudewordadd(
    interaction: discord.Interaction,
    word: str,
    reply_message: str = "",
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    DATA["bad_words"][
        word.lower()
    ] = reply_message[:500]

    save_data()

    await interaction.response.send_message(
        f"Added `{word}` to the filter. ✅",
        ephemeral=True,
    )


@bot.tree.command(
    name="addblacklistserver",
    description="Add a server ID to the blacklist.",
)
async def addblacklistserver(
    interaction: discord.Interaction,
    server_id: str,
):
    err = check_command(
        interaction,
        "administrator",
    )

    if err:
        return await send_error(interaction, err)

    try:
        server_id_int = int(server_id)
    except ValueError:
        return await send_error(
            interaction,
            "Invalid server ID.",
        )

    if server_id_int not in DATA["blacklist_servers"]:
        DATA["blacklist_servers"].append(
            server_id_int
        )
        save_data()

    await interaction.response.send_message(
        "Server added to blacklist. ✅",
        ephemeral=True,
    )


# ============================================================
# BOOSTER EMBED
# ============================================================

@bot.tree.command(
    name="booster",
    description="Send a booster thank-you embed.",
)
async def booster(
    interaction: discord.Interaction,
    message: str,
    channel: discord.TextChannel = None,
    file: discord.Attachment = None,
):
    err = check_command(
        interaction,
        "manage_messages",
    )

    if err:
        return await send_error(interaction, err)

    target = channel or interaction.channel
    member = interaction.user

    embed = discord.Embed(
        title="🚀 New Server Boost!",
        description=(
            f"{message}\n\n"
            f"Thank you {member.mention} "
            "for boosting our server! 💜"
        ),
        color=0xFF4FD8,
    )

    # Username + profile picture.
    embed.set_author(
        name=member.display_name,
        icon_url=member.display_avatar.url,
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    embed.set_footer(
        text=f"Boosted by {member.name} • "
        f"{interaction.guild.name}"
    )

    discord_file = None

    if file:
        discord_file = await file.to_file()

        if (
            file.content_type
            and file.content_type.startswith("image/")
        ):
            embed.set_image(
                url=f"attachment://{file.filename}"
            )

    await target.send(
        embed=embed,
        file=discord_file,
    )

    await interaction.response.send_message(
        f"Booster message sent to {target.mention}. 🚀",
        ephemeral=True,
    )


# ============================================================
# BOT ERROR HANDLING
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(
        error,
        app_commands.CheckFailure,
    ):
        return await send_error(
            interaction,
            "You do not have permission to use this command.",
        )

    print(
        f"[COMMAND ERROR] "
        f"{type(error).__name__}: {error}"
    )

    await send_error(
        interaction,
        "An internal error occurred while executing the command.",
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    keep_alive()

    token = (
        os.environ.get("DISCORD_TOKEN")
        or os.environ.get("TOKEN")
    )

    if not token:
        raise RuntimeError(
            "Missing DISCORD_TOKEN/TOKEN environment variable."
        )

    bot.run(token)

