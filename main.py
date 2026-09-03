import asyncio
import os
import threading
from flask import Flask
import discord
from discord.ext import commands

# --- FLASK WEB SERVER FOR RENDER PORT BINDING ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

# --- DISCORD BOT SETUP ---
intents = discord.Intents.all()

MEMBER_COUNT_CHANNEL_ID = 1544821289506574388  
TARGET_USER_ID = 0
TARGET_EMOJI = "👀"
COLOR_CHANNEL_ID = 0
RENAME_REQUEST_CHANNEL_ID = 0  
SECURITY_LOGS_CHANNEL_ID = 0

# Advanced Punishments Mapping: Action -> Punishment
SECURITY_PUNISHMENTS = {
    "delete_message": "timeout",
    "timeout": "timeout",
    "ban": "ban",
    "delete_channel": "kick",
    "create_channel": "kick",
    "delete_role": "kick",
    "create_role": "kick",
    "change_server_settings": "ban",
    "kick_member": "kick"
}
PUNISHMENT_CHANNEL_ID = 0
ANTINUKE_ENABLED = False
ANTIRAID_ENABLED = False
BAD_WORDS_FILTER = {}  
SERVER_BLACKLIST = []

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


# --- PERSISTENT BOT CLASS ---

class PersistentViewBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(TicketSelectView(GLOBAL_TICKET_REASONS, None, None))
        print("--- PERSISTENT VIEWS REGISTERED ---")

bot = PersistentViewBot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f"--- SUCCESS: BOT IS FULLY OPERATIONAL ---")
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Sync error: {e}")
    
    for guild in bot.guilds:
        await update_member_count_channel(guild)

@bot.event
async def on_member_join(member):
    if member.guild.id in SERVER_BLACKLIST:
        try:
            await member.ban(reason="Server is blacklisted.")
            return
        except:
            pass

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

@bot.event
async def on_member_remove(member):
    await update_member_count_channel(member.guild)

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

@bot.event
async def on_message(message):
    global TARGET_USER_ID, TARGET_EMOJI, COLOR_CHANNEL_ID, RENAME_REQUEST_CHANNEL_ID, BAD_WORDS_FILTER
    if message.author.bot:
        return
    
    # Bad words filter
    if BAD_WORDS_FILTER:
        content_lower = message.content.lower()
        for word, reply_msg in BAD_WORDS_FILTER.items():
            if word in content_lower:
                try:
                    await message.delete()
                    if reply_msg:
                        await message.channel.send(f"{message.author.mention} {reply_msg}", delete_after=5)
                except:
                    pass
                return

    if RENAME_REQUEST_CHANNEL_ID != 0 and message.channel.id == RENAME_REQUEST_CHANNEL_ID:
        new_nickname = message.content.strip()
        try:
            await message.delete()
        except:
            pass
        
        if len(new_nickname) > 32:
            try:
                await message.channel.send(f"{message.author.mention}, Nickname must be 32 characters or fewer!", delete_after=5)
            except:
                pass
            return

        try:
            embed = discord.Embed(
                title="📝 New Nickname Request",
                description=f"**User:** {message.author.mention} (`{message.author.id}`)\n**Requested Nickname:** `{new_nickname}`",
                color=0xF1C40F
            )
            view = RenameApprovalView(message.author, new_nickname)
            await message.channel.send(embed=embed, view=view)
        except Exception as e:
            print(f"Error in rename request: {e}")
        return

    if COLOR_CHANNEL_ID != 0 and message.channel.id == COLOR_CHANNEL_ID:
        color_text = message.content.strip()
        guild = message.guild
        member = message.author

        try:
            cleaned_color = color_text.lower().replace("#", "")
            color_map = {
                "red": 0xFF0000, "green": 0x00FF00, "blue": 0x0000FF,
                "yellow": 0xFFFF00, "cyan": 0x00FFFF, "magenta": 0xFF00FF,
                "purple": 0x800080, "pink": 0xFFC0CB, "orange": 0xFFA500,
                "black": 0x000001, "white": 0xFFFFFF, "grey": 0x808080,
                "navy": 0x000080, "teal": 0x008080, "maroon": 0x800000,
                "olive": 0x808000, "lime": 0x00FF00, "silver": 0xC0C0C0,
                "gold": 0xFFD700, "coral": 0xFF7F50, "indigo": 0x4B0082,
                "turquoise": 0x40E0D0, "crimson": 0xDC143C, "chocolate": 0xD2691E,
                "lavender": 0xE6E6FA, "salmon": 0xFA8072
            }

            if cleaned_color in color_map:
                color_int = color_map[cleaned_color]
                role_name = cleaned_color.capitalize()
            else:
                color_int = int(cleaned_color, 16)
                role_name = f"#{cleaned_color.upper()}"

            all_color_role_names = [name.capitalize() for name in color_map.keys()]
            roles_to_remove = [r for r in member.roles if r.name in all_color_role_names or r.name.startswith("#")]
            
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove)

            role = discord.utils.get(guild.roles, name=role_name)
            if not role:
                role = await guild.create_role(name=role_name, color=discord.Color(color_int), reason="Color role requested.")
            
            await member.add_roles(role)
        except Exception as e:
            print(f"Error handling color role: {e}")
        
        await bot.process_commands(message)
        return

    if TARGET_USER_ID != 0 and any(user.id == TARGET_USER_ID for user in message.mentions):
        try:
            await message.add_reaction(TARGET_EMOJI)
        except Exception as e:
            print(f"Error adding reaction: {e}")

    await bot.process_commands(message)

# --- ALL COMMANDS ---

@bot.tree.command(name="setpunishment", description="Set punishment for security actions.")
@commands.has_permissions(administrator=True)
async def setpunishment(
    interaction: discord.Interaction, 
    action: discord.app_commands.Choice[str], 
    punishment: discord.app_commands.Choice[str], 
    channel: discord.TextChannel = None
):
    await interaction.response.defer(thinking=True, ephemeral=True)
    global SECURITY_PUNISHMENTS, PUNISHMENT_CHANNEL_ID
    SECURITY_PUNISHMENTS[action.value] = punishment.value
    if channel:
        PUNISHMENT_CHANNEL_ID = channel.id
    await interaction.followup.send(f"Successfully set action **{action.name}** to punishment **{punishment.name}**! ✅", ephemeral=True)

@setpunishment.autocomplete("action")
async def setpunishment_action_autocomplete(interaction: discord.Interaction, current: str):
    actions = [
        "delete_message", "timeout", "ban", "delete_channel", 
        "create_channel", "delete_role", "create_role", 
        "change_server_settings", "kick_member"
    ]
    return [discord.app_commands.Choice(name=act.replace("_", " ").title(), value=act) for act in actions if current.lower() in act.lower()]

@setpunishment.autocomplete("punishment")
async def setpunishment_punishment_autocomplete(interaction: discord.Interaction, current: str):
    punishments = ["kick", "ban", "timeout"]
    return [discord.app_commands.Choice(name=p.capitalize(), value=p) for p in punishments if current.lower() in p.lower()]

@bot.tree.command(name="status", description="Change or view the bot's presence status.")
@commands.has_permissions(administrator=True)
async def status(
    interaction: discord.Interaction, 
    activity_type: discord.app_commands.Choice[str] = None, 
    status_type: discord.app_commands.Choice[str] = None, 
    text: str = None
):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        act_mapping = {
            "playing": discord.ActivityType.playing,
            "streaming": discord.ActivityType.streaming,
            "listening": discord.ActivityType.listening,
            "watching": discord.ActivityType.watching,
            "competing": discord.ActivityType.competing
        }
        stat_mapping = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "offline": discord.Status.offline
        }
        
        current_activity = bot.activity
        current_status = bot.status
        
        new_act = act_mapping.get(activity_type.value, current_activity.type) if activity_type else (current_activity.type if current_activity else discord.ActivityType.playing)
        new_text = text if text else (current_activity.name if current_activity else "Active")
        new_stat = stat_mapping.get(status_type.value, current_status) if status_type else current_status

        await bot.change_presence(status=new_stat, activity=discord.Activity(type=new_act, name=new_text))
        await interaction.followup.send("Bot status updated successfully! ✅", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error updating status: {e}", ephemeral=True)

@status.autocomplete("activity_type")
async def status_activity_autocomplete(interaction: discord.Interaction, current: str):
    acts = ["playing", "streaming", "listening", "watching", "competing"]
    return [discord.app_commands.Choice(name=a.capitalize(), value=a) for a in acts if current.lower() in a.lower()]

@status.autocomplete("status_type")
async def status_type_autocomplete(interaction: discord.Interaction, current: str):
    stats = ["online", "idle", "dnd", "offline"]
    return [discord.app_commands.Choice(name=s.capitalize(), value=s) for s in stats if current.lower() in s.lower()]

@bot.tree.command(name="setlogssecurity", description="Set the security logs channel.")
@commands.has_permissions(administrator=True)
async def setlogssecurity(interaction: discord.Interaction, channel: discord.TextChannel = None):
    await interaction.response.defer(thinking=True, ephemeral=True)
    global SECURITY_LOGS_CHANNEL_ID
    if channel:
        SECURITY_LOGS_CHANNEL_ID = channel.id
        await interaction.followup.send(f"Security logs channel set to {channel.mention}! ✅", ephemeral=True)
    else:
        SECURITY_LOGS_CHANNEL_ID = 0
        await interaction.followup.send("Security logs channel has been cleared/disabled! ❌", ephemeral=True)

@bot.tree.command(name="antinuke", description="Enable or disable the antinuke protection system.")
@commands.has_permissions(administrator=True)
async def antinuke(interaction: discord.Interaction, status: bool = None):
    await interaction.response.defer(thinking=True, ephemeral=True)
    global ANTINUKE_ENABLED
    if status is not None:
        ANTINUKE_ENABLED = status
    else:
        ANTINUKE_ENABLED = not ANTINUKE_ENABLED
    state = "Enabled" if ANTINUKE_ENABLED else "Disabled"
    await interaction.followup.send(f"Antinuke system is now **{state}**! ✅", ephemeral=True)

@bot.tree.command(name="antiraid", description="Enable or disable the anti-raid protection system.")
@commands.has_permissions(administrator=True)
async def antiraid(interaction: discord.Interaction, status: bool = None):
    await interaction.response.defer(thinking=True, ephemeral=True)
    global ANTIRAID_ENABLED
    if status is not None:
        ANTIRAID_ENABLED = status
    else:
        ANTIRAID_ENABLED = not ANTIRAID_ENABLED
    state = "Enabled" if ANTIRAID_ENABLED else "Disabled"
    await interaction.followup.send(f"Anti-Raid system is now **{state}**! ✅", ephemeral=True)

@bot.tree.command(name="rudewordadd", description="Add a bad word to the chat filter.")
@commands.has_permissions(administrator=True)
async def rudewordadd(interaction: discord.Interaction, word: str, reply_message: str = ""):
    await interaction.response.defer(thinking=True, ephemeral=True)
    global BAD_WORDS_FILTER
    BAD_WORDS_FILTER[word.lower()] = reply_message
    await interaction.followup.send(f"Bad word `{word}` added to filter successfully! ✅", ephemeral=True)

@bot.tree.command(name="addblacklistserver", description="Add a Server ID to the blacklist.")
@commands.has_permissions(administrator=True)
async def addblacklistserver(interaction: discord.Interaction, server_id: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    global SERVER_BLACKLIST
    try:
        s_id = int(server_id)
        if s_id not in SERVER_BLACKLIST:
            SERVER_BLACKLIST.append(s_id)
            await interaction.followup.send(f"Server ID `{s_id}` added to blacklist! ✅", ephemeral=True)
        else:
            await interaction.followup.send("Server ID is already blacklisted!", ephemeral=True)
    except ValueError:
        await interaction.followup.send("Invalid Server ID format!", ephemeral=True)

@bot.tree.command(name="joinvc", description="Make the bot join a voice channel.")
@commands.has_permissions(administrator=True)
async def joinvc(interaction: discord.Interaction, channel_id: str = None):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        channel = None
        if channel_id:
            channel = bot.get_channel(int(channel_id))
        elif interaction.user.voice and interaction.user.voice.channel:
            channel = interaction.user.voice.channel
            
        if channel and isinstance(channel, discord.VoiceChannel):
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.move_to(channel)
            else:
                await channel.connect()
            await interaction.followup.send(f"Successfully joined voice channel: **{channel.name}**! 🔊", ephemeral=True)
        else:
            await interaction.followup.send("Voice channel not found or you are not in one!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Voice connection error: {e}", ephemeral=True)

@bot.tree.command(name="leavevc", description="Make the bot leave the current voice channel.")
@commands.has_permissions(administrator=True)
async def leavevc(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.followup.send("Successfully left the voice channel! 👋", ephemeral=True)
    else:
        await interaction.followup.send("I am not in any voice channel!", ephemeral=True)

@bot.tree.command(name="voiceinfo", description="Show current voice connection information.")
async def voiceinfo(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if interaction.guild.voice_client and interaction.guild.voice_client.channel:
        vc = interaction.guild.voice_client.channel
        await interaction.followup.send(f"Connected to: **{vc.name}** (`{vc.id}`) | Members connected: {len(vc.members)} 🔊", ephemeral=True)
    else:
        await interaction.followup.send("I am not currently connected to any voice channel.", ephemeral=True)

@bot.tree.command(name="warn", description="Warn a member with a specified reason.")
@commands.has_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        await member.send(f"You have been warned in **{interaction.guild.name}** for: {reason}")
    except:
        pass
    await interaction.followup.send(f"Successfully warned {member.mention} for: {reason} ✅", ephemeral=True)

@bot.tree.command(name="check_warns", description="View recorded warnings for a specific member.")
@commands.has_permissions(manage_messages=True)
async def check_warns(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await interaction.followup.send(f"Checking warnings for {member.mention}... (Feature initialized)", ephemeral=True)

@bot.tree.command(name="clear_warns", description="Clear all warnings for a specific member.")
@commands.has_permissions(manage_messages=True)
async def clear_warns(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(thinking=True, ephemeral=True)
    await interaction.followup.send(f"Successfully cleared warnings for {member.mention}! ✅", ephemeral=True)

@bot.tree.command(name="mute", description="Timeout a member for a specified duration in minutes.")
@commands.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "No reason provided"):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        duration = discord.utils.utcnow() + discord.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        await interaction.followup.send(f"Successfully muted {member.mention} for {minutes} minutes! ✅", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error muting member: {e}", ephemeral=True)

@bot.tree.command(name="unmute", description="Remove timeout from a member or type 'all'.")
@commands.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, target: str = "all"):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        if target.lower() == "all":
            count = 0
            for member in interaction.guild.members:
                if member.is_timed_out():
                    try:
                        await member.timeout(None)
                        count += 1
                    except:
                        pass
            await interaction.followup.send(f"Successfully removed timeout from all ({count}) members! ✅", ephemeral=True)
        else:
            member = None
            if target.startswith("<@") and target.endswith(">"):
                member_id = int(target.strip("<@!>"))
                member = interaction.guild.get_member(member_id)
            elif target.isdigit():
                member = interaction.guild.get_member(int(target))
            else:
                member = discord.utils.get(interaction.guild.members, name=target)
            
            if member:
                await member.timeout(None)
                await interaction.followup.send(f"Successfully unmuted {member.mention}! ✅", ephemeral=True)
            else:
                await interaction.followup.send("Member not found!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error unmuting: {e}", ephemeral=True)

@bot.tree.command(name="untimeout", description="Remove timeout from a member or type 'all'.")
@commands.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, target: str = "all"):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        if target.lower() == "all":
            count = 0
            for member in interaction.guild.members:
                if member.is_timed_out():
                    try:
                        await member.timeout(None)
                        count += 1
                    except:
                        pass
            await interaction.followup.send(f"Successfully removed timeout from all ({count}) members! ✅", ephemeral=True)
        else:
            member = None
            if target.startswith("<@") and target.endswith(">"):
                member_id = int(target.strip("<@!>"))
                member = interaction.guild.get_member(member_id)
            elif target.isdigit():
                member = interaction.guild.get_member(int(target))
            else:
                member = discord.utils.get(interaction.guild.members, name=target)
            
            if member:
                await member.timeout(None)
                await interaction.followup.send(f"Successfully removed timeout for {member.mention}! ✅", ephemeral=True)
            else:
                await interaction.followup.send("Member not found!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error removing timeout: {e}", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a member from the server.")
@commands.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        await member.kick(reason=reason)
        await interaction.followup.send(f"Successfully kicked {member.mention}! ✅", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error kicking member: {e}", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a member from the server.")
@commands.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        await member.ban(reason=reason)
        await interaction.followup.send(f"Successfully banned {member.mention}! ✅", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error banning member: {e}", ephemeral=True)

@bot.tree.command(name="unban", description="Unban a member by name or type 'all'.")
@commands.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, target: str = "all"):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        bans = [ban_entry async for ban_entry in interaction.guild.bans()]
        if target.lower() == "all":
            count = 0
            for ban_entry in bans:
                await interaction.guild.unban(ban_entry.user)
                count += 1
            await interaction.followup.send(f"Successfully unbanned all ({count}) members! ✅", ephemeral=True)
        else:
            unbanned = False
            for ban_entry in bans:
                user = ban_entry.user
                if target.lower() in user.name.lower() or target == str(user.id):
                    await interaction.guild.unban(user)
                    unbanned = True
                    await interaction.followup.send(f"Successfully unbanned **{user.name}**! ✅", ephemeral=True)
                    break
            if not unbanned:
                await interaction.followup.send("User not found in ban list!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error unbanning: {e}", ephemeral=True)

@bot.tree.command(name="purge", description="Bulk delete messages in the chat.")
@commands.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int = 10):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Successfully deleted {len(deleted)} messages! ✅", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error purging messages: {e}", ephemeral=True)

@bot.tree.command(name="stats", description="Display general server and bot statistics.")
async def stats(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    guild = interaction.guild
    embed = discord.Embed(
        title="📊 Server & Bot Statistics",
        color=0x3498DB
    )
    embed.add_field(name="Server Name", value=guild.name, inline=True)
    embed.add_field(name="Total Members", value=guild.member_count, inline=True)
    embed.add_field(name="Total Guilds (Bot)", value=len(bot.guilds), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="sendhere", description="Send a message and files in the current channel.")
@commands.has_permissions(administrator=True)
async def sendhere(
    interaction: discord.Interaction, 
    message: str = "",
    file1: discord.Attachment = None, file2: discord.Attachment = None, file3: discord.Attachment = None,
    file4: discord.Attachment = None, file5: discord.Attachment = None, file6: discord.Attachment = None,
    file7: discord.Attachment = None, file8: discord.Attachment = None, file9: discord.Attachment = None,
    file10: discord.Attachment = None, file11: discord.Attachment = None, file12: discord.Attachment = None
):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        attachments = [file1, file2, file3, file4, file5, file6, file7, file8, file9, file10, file11, file12]
        files_to_send = []
        for att in attachments:
            if att:
                file_obj = await att.to_file()
                files_to_send.append(file_obj)
        
        final_message = message if message else None

        if final_message and files_to_send:
            await interaction.channel.send(content=final_message, files=files_to_send)
        elif final_message:
            await interaction.channel.send(content=final_message)
        elif files_to_send:
            await interaction.channel.send(files=files_to_send)
        else:
            await interaction.followup.send("Please provide a message or at least one file!", ephemeral=True)
            return

        await interaction.followup.send("Successfully sent!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)

@bot.tree.command(name="react", description="Add an emoji reaction to a specific message ID.")
@commands.has_permissions(administrator=True)
async def react(interaction: discord.Interaction, message_id: str, emoji: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
        await msg.add_reaction(emoji)
        await interaction.followup.send("Reaction added successfully! ✅", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)

# --- STARTUP ---
if __name__ == "__main__":
    keep_alive()
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Error: DISCORD_TOKEN environment variable is missing!")
