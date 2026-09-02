import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
from flask import Flask
from threading import Thread
import asyncio

# 1. Flask Server to keep the bot alive on hosting platforms
app = Flask('')
@app.route('/')
def home():
    return "Bot is running and alive!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# 2. Intents Setup with full coverage
intents = discord.Intents.all()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True
intents.moderation = True
intents.bans = True

# Secure JSON Storage File
DATA_FILE = "bot_data.json"

def load_data():
    if not os.path.exists(DATA_FILE):
        return {
            "punishments": {},
            "logs": {},
            "antinuke": {},
            "antiraid": {},
            "badwords": {},
            "blacklist_servers": [],
            "warns": {}
        }
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "punishments": {},
            "logs": {},
            "antinuke": {},
            "antiraid": {},
            "badwords": {},
            "blacklist_servers": [],
            "warns": {}
        }

def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving data: {e}")

db = load_data()

MEMBER_COUNT_CHANNEL_ID = 1530802174328701048  
TARGET_USER_ID = 0
TARGET_EMOJI = "👀"
COLOR_CHANNEL_ID = 0
RENAME_REQUEST_CHANNEL_ID = 0  

GLOBAL_TICKET_REASONS = ["buy vip:Buy VIP role here", "support:General assistance"]
VERIFY_EMOJI_DATA = {}

async def update_member_count_channel(guild):
    if MEMBER_COUNT_CHANNEL_ID == 0:
        return
    try:
        channel = guild.get_channel(MEMBER_COUNT_CHANNEL_ID)
        if channel and isinstance(channel, discord.VoiceChannel):
            member_count = guild.member_count
            new_name = f"members: {member_count}"
            if channel.name != new_name:
                await channel.edit(name=new_name, reason="Updating member count stats.")
    except Exception as e:
        print(f"Error updating member count channel: {e}")

# --- ADVANCED TICKET VIEWS ---

class TicketControlView(discord.ui.View):
    def __init__(self, closed_category_id=None):
        super().__init__(timeout=None)
        self.closed_category_id = closed_category_id

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="persistent_ticket_close_btn", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            channel = interaction.channel
            guild = interaction.guild
            closed_cat = guild.get_channel(self.closed_category_id) if self.closed_category_id else None
            
            await channel.edit(name=f"closed-{interaction.user.name}", category=closed_cat, reason="Ticket closed by user.")
            await channel.set_permissions(interaction.guild.default_role, view_channel=False)
            
            await interaction.followup.send("Ticket closed successfully! 🔒", ephemeral=True)
            
            open_view = TicketOpenView(self.closed_category_id)
            await channel.send("This ticket has been closed. Choose an action below:", view=open_view)
        except Exception as e:
            await interaction.followup.send(f"Error closing ticket: {e}", ephemeral=True)

class TicketOpenView(discord.ui.View):
    def __init__(self, closed_category_id=None):
        super().__init__(timeout=None)
        self.closed_category_id = closed_category_id

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.green, custom_id="persistent_ticket_reopen_btn", emoji="🔓")
    async def reopen_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            channel = interaction.channel
            await channel.edit(name=f"ticket-{interaction.user.name}", reason="Ticket reopened.")
            await interaction.followup.send("Ticket reopened successfully! 🔓", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error reopening ticket: {e}", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.grey, custom_id="persistent_ticket_delete_btn", emoji="🗑️")
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("You do not have permission to delete this ticket!", ephemeral=True)
            return

        await interaction.response.send_message("Deleting ticket channel in 3 seconds...", ephemeral=True)
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason="Ticket channel deleted.")
        except Exception as e:
            print(f"Error deleting channel: {e}")

class TicketSelect(discord.ui.Select):
    def __init__(self, reasons_list, open_category_id, closed_category_id):
        self.open_category_id = open_category_id
        self.closed_category_id = closed_category_id
        
        options = []
        for item in reasons_list:
            if ":" in item:
                label, desc = item.split(":", 1)
                label = label.strip()
                desc = desc.strip()
            else:
                label = item.strip()
                desc = f"Open a ticket for {label}"
            
            if label:
                options.append(discord.SelectOption(label=label, description=desc[:100], emoji="🎫"))
        
        if not options:
            options.append(discord.SelectOption(label="Support", description="General Support", emoji="🎫"))

        max_val = min(len(options), 25)
        super().__init__(placeholder="Select reasons to open a ticket...", min_values=1, max_values=max_val, options=options, custom_id="persistent_ticket_select_menu")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        member = interaction.user

        try:
            category = guild.get_channel(self.open_category_id) if self.open_category_id else None
            
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
            }

            reasons_joined = ", ".join(self.values)
            ticket_channel = await guild.create_text_channel(
                name=f"ticket-{member.name}",
                category=category,
                overwrites=overwrites,
                reason=f"Ticket opened for: {reasons_joined}"
            )

            control_view = TicketControlView(self.closed_category_id)
            embed = discord.Embed(
                title="Support Ticket",
                description=f"Welcome {member.mention}!\nReasons selected: **{reasons_joined}**\nSupport team will assist you shortly.",
                color=0x5865F2
            )
            await ticket_channel.send(embed=embed, view=control_view)
            await interaction.followup.send(f"Your ticket has been created: {ticket_channel.mention} ✅", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to create ticket: {e}", ephemeral=True)

class TicketSelectView(discord.ui.View):
    def __init__(self, reasons_list, open_category_id=None, closed_category_id=None):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(reasons_list, open_category_id, closed_category_id))

class RenameApprovalView(discord.ui.View):
    def __init__(self, member: discord.Member, new_nickname: str):
        super().__init__(timeout=None)
        self.member = member
        self.new_nickname = new_nickname

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, custom_id="rename_accept_btn", emoji="✅")
    async def accept_rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_nicknames:
            await interaction.response.send_message("You do not have permission to use this button!", ephemeral=True)
            return
            
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            await self.member.edit(nick=self.new_nickname)
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            await interaction.followup.send(f"Nickname change approved for {self.member.mention}! ✅", ephemeral=True)
            try:
                await self.member.send(f"Your request to change your nickname to **{self.new_nickname}** has been **Accepted**! 🎉")
            except:
                pass
        except Exception as e:
            await interaction.followup.send(f"Error applying nickname: {e}", ephemeral=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, custom_id="rename_reject_btn", emoji="❌")
    async def reject_rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_nicknames:
            await interaction.response.send_message("You do not have permission to use this button!", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
            await interaction.followup.send(f"Nickname change rejected for {self.member.mention}. ❌", ephemeral=True)
            try:
                await self.member.send(f"Your request to change your nickname to **{self.new_nickname}** has been **Rejected**.")
            except:
                pass
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

# --- PERSISTENT BOT CLASS ---

class PersistentViewBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(TicketSelectView(GLOBAL_TICKET_REASONS, None, None))
        print("--- PERSISTENT VIEWS REGISTERED ---")

bot = PersistentViewBot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f"--- SUCCESS: BOT IS FULLY OPERATIONAL ---")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s) successfully.")
    except Exception as e:
        print(f"Sync error: {e}")
    
    for guild in bot.guilds:
        await update_member_count_channel(guild)

@bot.event
async def on_member_join(member):
    try:
        welcome_message = (
            f"welcome {member.mention} to hell of tunisia server!\n"
            f"invite your friends and enjoy\n"
            f"https://discord.gg/WMWgkFuxA"
        )
        await member.send(welcome_message)
    except Exception as e:
        print(f"Could not send welcome DM to {member.name}: {e}")
    
    await update_member_count_channel(member.guild)

    if member.bot:
        await asyncio.sleep(1.5)
        try:
            async for entry in member.guild.audit_logs(limit=3, action=discord.AuditLogAction.bot_add):
                if entry.target and entry.target.id == member.id:
                    if entry.user and not entry.user.bot:
                        await apply_punishment(member.guild, entry.user, "add_bot")
                        return
        except Exception as e:
            print(f"Error in on_member_join (bot check): {e}")

@bot.event
async def on_member_remove(member):
    await update_member_count_channel(member.guild)

# ==================== Security & Settings Commands ====================

@bot.tree.command(name="setpunishment", description="Set punishment for security actions with optional specific channel")
@app_commands.describe(
    action_type="Select the security event type",
    punishment="Select the punishment",
    channel="Optional: Target specific channel for delete message punishment"
)
@app_commands.choices(action_type=[
    app_commands.Choice(name="Delete Message in Channel", value="del_msg"),
    app_commands.Choice(name="Ban Member", value="ban_member"),
    app_commands.Choice(name="Creating Channel", value="create_chan"),
    app_commands.Choice(name="Creating Role", value="create_role"),
    app_commands.Choice(name="Giving Administrator Role", value="give_admin"),
    app_commands.Choice(name="Add Bot", value="add_bot"),
    app_commands.Choice(name="Kick Member", value="kick_member"),
    app_commands.Choice(name="Deleting Channel", value="del_chan")
], punishment=[
    app_commands.Choice(name="Kick", value="kick"),
    app_commands.Choice(name="Ban", value="ban")
])
@app_commands.checks.has_permissions(administrator=True)
async def setpunishment(interaction: discord.Interaction, action_type: str, punishment: str, channel: discord.TextChannel = None):
    guild_id = str(interaction.guild_id)
    if guild_id not in db["punishments"]:
        db["punishments"][guild_id] = {}
    
    channel_id = channel.id if channel else "all"
    db["punishments"][guild_id][action_type] = {
        "punishment": punishment,
        "channel_id": channel_id
    }
    save_data(db)
    
    chan_text = channel.mention if channel else "All Channels (Global)"
    await interaction.response.send_message(f"✅ Punishment for **{action_type}** set to **{punishment}** in **{chan_text}**", ephemeral=True)

@bot.tree.command(name="setlogssecurity", description="Set the security logs channel for actions")
@app_commands.describe(channel="Select the logs channel")
@app_commands.checks.has_permissions(administrator=True)
async def setlogssecurity(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    db["logs"][guild_id] = channel.id
    save_data(db)
    await interaction.response.send_message(f"✅ Security logs channel successfully set to: {channel.mention}", ephemeral=True)

@bot.tree.command(name="antinuke", description="Toggle the Antinuke security system")
@app_commands.checks.has_permissions(administrator=True)
async def antinuke(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    current = db["antinuke"].get(guild_id, False)
    db["antinuke"][guild_id] = not current
    save_data(db)
    status = "Enabled ✅" if db["antinuke"][guild_id] else "Disabled ❌"
    await interaction.response.send_message(f"🔒 Antinuke status is now: **{status}**", ephemeral=True)

@bot.tree.command(name="antiraid", description="Toggle the Anti-Raid security system")
@app_commands.checks.has_permissions(administrator=True)
async def antiraid(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    current = db["antiraid"].get(guild_id, False)
    db["antiraid"][guild_id] = not current
    save_data(db)
    status = "Enabled ✅" if db["antiraid"][guild_id] else "Disabled ❌"
    await interaction.response.send_message(f"🛡️ Anti-Raid status is now: **{status}**", ephemeral=True)

@bot.tree.command(name="rudewordadd", description="Add a bad word to the filter with an auto-reply message")
@app_commands.describe(word="The word to filter", delete_message="Delete the message?", reply_message="Bot reply message (e.g. Shut up @user!)")
@app_commands.choices(delete_message=[
    app_commands.Choice(name="Yes", value="yes"),
    app_commands.Choice(name="No", value="no")
])
@app_commands.checks.has_permissions(administrator=True)
async def rudewordadd(interaction: discord.Interaction, word: str, delete_message: str, reply_message: str):
    guild_id = str(interaction.guild_id)
    if guild_id not in db["badwords"]:
        db["badwords"][guild_id] = {}
    db["badwords"][guild_id][word.lower()] = {
        "delete": delete_message == "yes",
        "reply": reply_message
    }
    save_data(db)
    await interaction.response.send_message(f"✅ Word `{word}` has been added to the bad words filter.", ephemeral=True)

@bot.tree.command(name="addblacklistserver", description="Blacklist a server ID from letting users join")
@app_commands.describe(server_id="The Server ID")
@app_commands.checks.has_permissions(administrator=True)
async def addblacklistserver(interaction: discord.Interaction, server_id: str):
    if server_id not in db["blacklist_servers"]:
        db["blacklist_servers"].append(server_id)
        save_data(db)
        await interaction.response.send_message(f"✅ Server `{server_id}` added to blacklist.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ Server is already blacklisted.", ephemeral=True)

# ==================== Voice Channel Commands ====================

@bot.tree.command(name="joinvc", description="Make the bot join a voice channel")
@app_commands.describe(channel_id="The Voice Channel ID")
async def joinvc(interaction: discord.Interaction, channel_id: str):
    try:
        channel = bot.get_channel(int(channel_id))
        if isinstance(channel, discord.VoiceChannel):
            await channel.connect()
            await interaction.response.send_message(f"🔊 Successfully joined voice channel: `{channel.name}`", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Invalid or unavailable voice channel ID.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ An error occurred: {e}", ephemeral=True)

@bot.tree.command(name="leavevc", description="Make the bot leave the voice channel")
async def leavevc(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("🔇 Successfully left the voice channel.", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ The bot is not connected to any voice channel.", ephemeral=True)

@bot.tree.command(name="voiceinfo", description="Show current voice connection info")
async def voiceinfo(interaction: discord.Interaction):
    if interaction.guild.voice_client and interaction.guild.voice_client.channel:
        vc = interaction.guild.voice_client
        await interaction.response.send_message(f"📊 Voice Info:\n- Channel: {vc.channel.name}\n- Connected Members: {len(vc.channel.members)}", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ The bot is not connected to voice currently.", ephemeral=True)

# ==================== Moderation & Warnings Commands ====================

@bot.tree.command(name="warn", description="Warn a member with a specific reason")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    guild_id = str(interaction.guild_id)
    user_id = str(member.id)
    if guild_id not in db["warns"]:
        db["warns"][guild_id] = {}
    if user_id not in db["warns"][guild_id]:
        db["warns"][guild_id][user_id] = []
    
    db["warns"][guild_id][user_id].append(reason)
    save_data(db)
    await interaction.response.send_message(f"⚠️ Member {member.mention} has been warned. Reason: {reason}", ephemeral=True)

@bot.tree.command(name="check_warns", description="Check warnings for a member")
async def check_warns(interaction: discord.Interaction, member: discord.Member):
    guild_id = str(interaction.guild_id)
    user_id = str(member.id)
    warns = db.get("warns", {}).get(guild_id, {}).get(user_id, [])
    if not warns:
        await interaction.response.send_message(f"✅ Member {member.mention} has no warnings.", ephemeral=True)
    else:
        warns_list = "\n".join([f"- {w}" for w in warns])
        await interaction.response.send_message(f"📌 Warnings for {member.mention}:\n{warns_list}", ephemeral=True)

@bot.tree.command(name="clear_warns", description="Clear all warnings for a member")
@app_commands.checks.has_permissions(administrator=True)
async def clear_warns(interaction: discord.Interaction, member: discord.Member):
    guild_id = str(interaction.guild_id)
    user_id = str(member.id)
    if guild_id in db["warns"] and user_id in db["warns"][guild_id]:
        db["warns"][guild_id][user_id] = []
        save_data(db)
        await interaction.response.send_message(f"✅ Cleared all warnings for {member.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ Member has no warnings recorded.", ephemeral=True)

@bot.tree.command(name="mute", description="Timeout a member for a duration")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
    await interaction.response.send_message(f"⚠️ Member {member.mention} has been muted for {duration}. Reason: {reason}", ephemeral=True)

@bot.tree.command(name="unmute", description="Remove timeout from a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    await interaction.response.send_message(f"✅ Timeout removed from {member.mention}.", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a member from the server")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.kick(reason=reason)
    await interaction.response.send_message(f"👢 Kicked {member.mention}. Reason: {reason}", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a member from the server")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.ban(reason=reason)
    await interaction.response.send_message(f"🔨 Banned {member.mention}. Reason: {reason}", ephemeral=True)

@bot.tree.command(name="unban", description="Unban a member by name or type 'all' to unban everyone")
@app_commands.describe(member_name="Member username or type 'all'")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, member_name: str):
    await interaction.response.defer(ephemeral=True)
    ban_entries = [entry async for entry in interaction.guild.bans()]
    
    if member_name.lower() == "all":
        if not ban_entries:
            await interaction.followup.send("⚠️ There are no banned members in this server.", ephemeral=True)
            return
        
        count = 0
        for entry in ban_entries:
            try:
                await interaction.guild.unban(entry.user)
                count += 1
            except Exception:
                pass
        await interaction.followup.send(f"✅ Successfully unbanned all banned members (Total: {count}).", ephemeral=True)
        return

    for entry in ban_entries:
        if entry.user.name.lower() == member_name.lower():
            await interaction.guild.unban(entry.user)
            await interaction.followup.send(f"✅ Successfully unbanned `{member_name}`.", ephemeral=True)
            return
            
    await interaction.followup.send(f"❌ Member `{member_name}` not found in bans list.", ephemeral=True)

@bot.tree.command(name="stats", description="Show server and bot statistics")
async def stats(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=f"📊 Statistics for {guild.name}", color=discord.Color.blue())
    embed.add_field(name="👥 Members", value=str(guild.member_count), inline=True)
    embed.add_field(name="💬 Channels", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="🛡️ Roles", value=str(len(guild.roles)), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="purge", description="Delete a specified amount of messages")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount + 1)
    await interaction.followup.send(f"🧹 Successfully deleted `{len(deleted) - 1}` messages.", ephemeral=True)

# ==================== Punishment Executor Helper ====================
async def apply_punishment(guild, member, action_type, channel_id=None):
    guild_id = str(guild.id)
    action_data = db.get("punishments", {}).get(guild_id, {}).get(action_type)
    
    if not action_data or not member:
        return
    
    if isinstance(action_data, str):
        punishment = action_data
        target_channel = "all"
    else:
        punishment = action_data.get("punishment")
        target_channel = action_data.get("channel_id", "all")

    if action_type == "del_msg" and target_channel != "all":
        if channel_id and str(channel_id) != str(target_channel):
            return

    print(f"🔥 SECURE TRIGGER: Action '{action_type}' | Target Member: {member} | Punishment: {punishment}")
    
    if member.bot or member.id == guild.owner_id:
        return

    try:
        if punishment == "kick":
            await member.kick(reason=f"Security Guard: {action_type}")
        elif punishment == "ban":
            await member.ban(reason=f"Security Guard: {action_type}")
        
        log_channel_id = db.get("logs", {}).get(guild_id)
        if log_channel_id:
            log_chan = guild.get_channel(log_channel_id)
            if log_chan:
                await log_chan.send(f"🛡️ **Security Triggered:** Member {member.mention} was **{punishment}ned** for: `{action_type}`")
    except Exception as e:
        print(f"❌ CRITICAL ERROR executing punishment: {e}")

# ==================== Event Listeners ====================

@bot.event
async def on_guild_channel_create(channel):
    await asyncio.sleep(1.5)
    try:
        async for entry in channel.guild.audit_logs(limit=3, action=discord.AuditLogAction.channel_create):
            if entry.target and entry.target.id == channel.id:
                if entry.user and not entry.user.bot:
                    await apply_punishment(channel.guild, entry.user, "create_chan")
                    return
        async for entry in channel.guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
            if entry.user and not entry.user.bot:
                await apply_punishment(channel.guild, entry.user, "create_chan")
                return
    except Exception as e:
        print(f"Error in on_guild_channel_create: {e}")

@bot.event
async def on_guild_channel_delete(channel):
    await asyncio.sleep(1.5)
    try:
        async for entry in channel.guild.audit_logs(limit=3, action=discord.AuditLogAction.channel_delete):
            if entry.user and not entry.user.bot:
                await apply_punishment(channel.guild, entry.user, "del_chan")
                return
    except Exception as e:
        print(f"Error in on_guild_channel_delete: {e}")

@bot.event
async def on_guild_role_create(role):
    await asyncio.sleep(1.5)
    try:
        async for entry in role.guild.audit_logs(limit=3, action=discord.AuditLogAction.role_create):
            if entry.user and not entry.user.bot:
                await apply_punishment(role.guild, entry.user, "create_role")
                return
    except Exception as e:
        print(f"Error in on_guild_role_create: {e}")

@bot.event
async def on_member_update(before, after):
    await asyncio.sleep(1.5)
    try:
        if not any(r.permissions.administrator for r in before.roles) and any(r.permissions.administrator for r in after.roles):
            async for entry in after.guild.audit_logs(limit=3, action=discord.AuditLogAction.member_role_update):
                if entry.target and entry.target.id == after.id:
                    if entry.user and not entry.user.bot:
                        await apply_punishment(after.guild, entry.user, "give_admin")
                        return
    except Exception as e:
        print(f"Error in on_member_update: {e}")

@bot.event
async def on_raw_message_delete(payload):
    await asyncio.sleep(1.5)
    try:
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            return
        
        async for entry in guild.audit_logs(limit=3, action=discord.AuditLogAction.message_delete):
            if entry.user and not entry.user.bot:
                await apply_punishment(guild, entry.user, "del_msg", channel_id=payload.channel_id)
                return
    except Exception as e:
        print(f"Error in on_raw_message_delete: {e}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    guild_id = str(message.guild.id) if message.guild else None
    if guild_id:
        badwords = db.get("badwords", {}).get(guild_id, {})
        content_lower = message.content.lower()
        for word, settings in badwords.items():
            if word in content_lower:
                if settings.get("delete", False):
                    try:
                        await message.delete()
                        print(f"🗑️ Bad word detected and deleted from {message.author}")
                        await apply_punishment(message.guild, message.author, "del_msg", channel_id=message.channel.id)
                    except Exception as e:
                        print(f"Error handling badword delete: {e}")
                
                reply_text = settings.get("reply", "").replace("@user", message.author.mention).replace("@username", message.author.name)
                if reply_text:
                    try:
                        await message.channel.send(reply_text)
                    except Exception:
                        pass
                break

    await bot.process_commands(message)

# ==================== Main Execution ====================
if __name__ == "__main__":
    keep_alive()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ Error: DISCORD_TOKEN environment variable is not set!")
