import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import requests
import os
import threading
import math
import random
import time
from flask import Flask
from collections import defaultdict
from dotenv import load_dotenv
import certifi
from pymongo import MongoClient
from datetime import datetime, timedelta
import pytz
from langdetect import detect, LangDetectException
from discord.ui import Button, View

# Set timezone to Philippines (GMT+8)
PH_TIMEZONE = pytz.timezone("Asia/Manila")
load_dotenv()

# ===========================
# Bot Setup
# ===========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Rate limiting data
bot.ask_rate_limit = defaultdict(list)
bot.conversations = defaultdict(list)  # In-memory cache for AI conversation
bot.last_message_id = {}  # Store last message IDs for threaded replies

# ===========================
# Flask Web Server (Keep Alive)
# ===========================
app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is alive!"

def run_server():
    app.run(host='0.0.0.0', port=5000)

server_thread = threading.Thread(target=run_server)
server_thread.start()

# Optional: Add another threaded task
def check_for_updates():
    while True:
        print("[Background] Checking for updates...")
        time.sleep(300)  # Every 5 minutes

update_thread = threading.Thread(target=check_for_updates)
update_thread.daemon = True
update_thread.start()

# ===========================
# MongoDB Setup (with SSL Fix)
# ===========================
try:
    client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
    db = client.ai_bot
    conversations_collection = db.conversations
    reminders_collection = db.reminders

    # Create TTL indexes
    conversations_collection.create_index("timestamp", expireAfterSeconds=604800)  # 7 days
    reminders_collection.create_index("reminder_time", expireAfterSeconds=2592000)  # 30 days
except Exception as e:
    print(f"[!] Failed to connect to MongoDB: {e}")
    client = None
    conversations_collection = None
    reminders_collection = None

# ===========================
# AI Commands
# ===========================

# /ask - Chat with Llama 3 via Together AI with threaded replies
@bot.tree.command(name="ask", description="Chat with an AI assistant using Llama 3")
@app_commands.describe(prompt="What would you like to ask?")
async def ask(interaction: discord.Interaction, prompt: str):
    user_id = interaction.user.id
    channel_id = interaction.channel.id
    await interaction.response.defer()

    # Rate limit: 5 messages/user/minute
    current_time = asyncio.get_event_loop().time()
    timestamps = bot.ask_rate_limit[user_id]
    timestamps.append(current_time)
    bot.ask_rate_limit[user_id] = [t for t in timestamps if current_time - t <= 60]

    if len(timestamps) > 5:
        await interaction.followup.send("⏳ You're being rate-limited. Please wait.")
        return

    async with interaction.channel.typing():
        try:
            # Custom filter for creator questions
            normalized_prompt = prompt.strip().lower()
            if normalized_prompt in ["who made you", "who created you", "who created this bot", "who made this bot"]:
                embed = discord.Embed(description="I was created by **Neroniel**.", color=discord.Color.blue())
                embed.set_footer(text="Neroniel AI")
                embed.timestamp = datetime.now(PH_TIMEZONE)
                msg = await interaction.followup.send(embed=embed)
                bot.last_message_id[(user_id, channel_id)] = msg.id
                return

            # Language Detection
            try:
                detected_lang = detect(prompt)
            except LangDetectException:
                detected_lang = "en"  # default to English

            lang_instruction = {
                "tl": "Please respond in Tagalog.",
                "es": "Por favor responde en español.",
                "fr": "Veuillez répondre en français.",
                "ja": "日本語で答えてください。",
                "ko": "한국어로 답변해 주세요.",
                "zh": "请用中文回答。",
                "ru": "Пожалуйста, отвечайте на русском языке.",
                "ar": "من فضلك أجب بالعربية.",
                "vi": "Vui lòng trả lời bằng tiếng Việt.",
                "th": "กรุณาตอบเป็นภาษาไทย",
                "id": "Silakan jawab dalam bahasa Indonesia"
            }.get(detected_lang, "")

            # Load conversation history from MongoDB (if available)
            history = []
            if conversations_collection:
                if not bot.conversations[user_id]:
                    history_docs = conversations_collection.find({"user_id": user_id}).sort("timestamp", -1).limit(5)
                    for doc in history_docs:
                        bot.conversations[user_id].append({
                            "user": doc["prompt"],
                            "assistant": doc["response"]
                        })
                    bot.conversations[user_id].reverse()  # Maintain order
                history = bot.conversations[user_id][-5:]

            # Build full prompt with language instruction
            system_prompt = f"You are a helpful and friendly AI assistant named Neroniel AI. {lang_instruction}"
            full_prompt = system_prompt
            for msg in history:
                full_prompt += f"User: {msg['user']}\nAssistant: {msg['assistant']}\n"
            full_prompt += f"User: {prompt}\nAssistant:"

            # Call Together AI
            headers = {
                "Authorization": f"Bearer {os.getenv('TOGETHER_API_KEY')}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "meta-llama/Llama-3-70b-chat-hf",
                "prompt": full_prompt,
                "max_tokens": 2048,
                "temperature": 0.7
            }

            response = requests.post(
                "https://api.together.xyz/v1/completions ",
                headers=headers,
                json=payload
            )
            data = response.json()

            if 'error' in data:
                await interaction.followup.send(f"❌ Error from AI API: {data['error']['message']}")
                return

            ai_response = data["choices"][0]["text"].strip()

            # Determine if we should reply to a previous message
            target_message_id = bot.last_message_id.get((user_id, channel_id))

            # Send the AI response
            embed = discord.Embed(description=ai_response, color=discord.Color.blue())
            embed.set_footer(text="Neroniel AI")
            embed.timestamp = datetime.now(PH_TIMEZONE)

            if target_message_id:
                try:
                    msg = await interaction.channel.fetch_message(target_message_id)
                    reply = await msg.reply(embed=embed)
                except discord.NotFound:
                    msg = await interaction.followup.send(embed=embed)
                    reply = msg
            else:
                msg = await interaction.followup.send(embed=embed)
                reply = msg

            # Update the last message ID for future replies
            bot.last_message_id[(user_id, channel_id)] = reply.id

            # Store in memory and MongoDB
            bot.conversations[user_id].append({
                "user": prompt,
                "assistant": ai_response
            })

            if conversations_collection:
                conversations_collection.insert_one({
                    "user_id": user_id,
                    "prompt": prompt,
                    "response": ai_response,
                    "timestamp": datetime.now(PH_TIMEZONE)
                })

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}")

# /clearhistory - Clear stored conversation history
@bot.tree.command(name="clearhistory", description="Clear your AI conversation history")
async def clearhistory(interaction: discord.Interaction):
    user_id = interaction.user.id
    # Clear local memory
    if user_id in bot.conversations:
        bot.conversations[user_id].clear()
    # Clear MongoDB history
    if conversations_collection:
        conversations_collection.delete_many({"user_id": user_id})
    await interaction.response.send_message("✅ Your AI conversation history has been cleared!", ephemeral=True)

# ===========================
# Background Tasks
# ===========================
@tasks.loop(seconds=60)
async def check_reminders():
    if not reminders_collection:
        return
    try:
        now = datetime.now(PH_TIMEZONE)
        expired = reminders_collection.find({"reminder_time": {"$lte": now}})
        for reminder in expired:
            user_id = reminder["user_id"]
            guild_id = reminder["guild_id"]
            channel_id = reminder["channel_id"]
            note = reminder["note"]
            user = bot.get_user(user_id)
            if not user:
                user = await bot.fetch_user(user_id)
            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            try:
                await channel.send(f"🔔 {user.mention}, reminder: {note}")
            except discord.Forbidden:
                print(f"[!] Cannot send reminder to {user} in #{channel.name}")
            reminders_collection.delete_one({"_id": reminder["_id"]})
    except Exception as e:
        print(f"[!] Error checking reminders: {e}")

@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()

if reminders_collection:
    check_reminders.start()

# ========== Currency Conversion Commands ==========
# Payout
@bot.tree.command(name="payout", description="Convert Robux to PHP based on Payout rate (₱320 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def payout(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (320 / 1000)
    await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")

# Gift
@bot.tree.command(name="gift", description="Convert Robux to PHP based on Gift rate (₱250 for 1000 Robux)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def gift(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (250 / 1000)
    await interaction.response.send_message(f"🎁 {robux} Robux = **₱{php:.2f} PHP**")

# NCT
@bot.tree.command(name="nct", description="Convert Robux to PHP based on NCT rate (₱240/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def nct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Invalid input.")
        return
    php = robux * (240 / 1000)
    await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")

# CT
@bot.tree.command(name="ct", description="Convert Robux to PHP based on CT rate (₱340/1k)")
@app_commands.describe(robux="How much Robux do you want to convert?")
async def ct(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    php = robux * (340 / 1000)
    await interaction.response.send_message(f"💵 {robux} Robux = **₱{php:.2f} PHP**")

# All Rates
@bot.tree.command(name="allrates", description="See PHP equivalent across all rates for given Robux")
@app_commands.describe(robux="How much Robux do you want to compare?")
async def allrates(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    rates = {
        "Not Covered Tax (₱240)": 240,
        "Covered Tax (₱340)": 340,
        "Group Payout (₱320)": 320,
        "Gift (₱250)": 250
    }
    result = "\n".join([f"**{label}** → ₱{(value / 1000) * robux:.2f}" for label, value in rates.items()])
    await interaction.response.send_message(f"📊 **{robux} Robux Conversion:**\n{result}")

# Before Tax
@bot.tree.command(name="beforetax", description="Calculate how much Robux you'll receive after 30% tax")
@app_commands.describe(robux="How much Robux is being sent?")
async def beforetax(interaction: discord.Interaction, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.")
        return
    received = math.floor(robux * 0.7)
    await interaction.response.send_message(f"📤 Sending **{robux} Robux** → You will receive **{received} Robux** after tax.")

# After Tax
@bot.tree.command(name="aftertax", description="Calculate how much Robux to send to receive desired amount after 30% tax")
@app_commands.describe(target="How much Robux do you want to receive *after* tax?")
async def aftertax(interaction: discord.Interaction, target: int):
    if target <= 0:
        await interaction.response.send_message("❗ Target Robux must be greater than zero.")
        return
    sent = math.ceil(target / 0.7)
    await interaction.response.send_message(f"📬 To receive **{target} Robux**, send **{sent} Robux** (30% tax).")

# ConvertCurrency
@bot.tree.command(name="convertcurrency", description="Convert between two currencies")
@app_commands.describe(
    amount="Amount to convert",
    from_currency="Currency to convert from (e.g., USD)",
    to_currency="Currency to convert to (e.g., PHP)"
)
async def convertcurrency(interaction: discord.Interaction, amount: float, from_currency: str, to_currency: str):
    api_key = os.getenv("CURRENCY_API_KEY")
    if not api_key:
        await interaction.response.send_message("❌ `CURRENCY_API_KEY` is missing in environment variables.")
        return
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    url = f"https://api.currencyapi.com/v3/latest?apikey= {api_key}&currencies={to_currency}&base_currency={from_currency}"
    try:
        response = requests.get(url)
        data = response.json()
        if 'error' in data:
            await interaction.response.send_message(f"❌ API Error: {data['error']['message']}")
            return
        if "data" not in data or to_currency not in data["data"]:
            await interaction.response.send_message("❌ Invalid currency code or no data found.")
            return
        rate = data["data"][to_currency]["value"]
        result = amount * rate
        embed = discord.Embed(color=discord.Color.gold())
        embed.title = f"💱 Currency Conversion from {from_currency}"
        embed.add_field(name="📥 Input", value=f"`{amount} {from_currency}`", inline=False)
        embed.add_field(name="📉 Rate", value=f"`1 {from_currency} = {rate:.4f} {to_currency}`", inline=False)
        embed.add_field(name="📤 Result", value=f"≈ **{result:.2f} {to_currency}**", inline=False)
        embed.set_footer(text="Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error during conversion: {str(e)}")

# ========== Weather Command ==========
PHILIPPINE_CITIES = [
    "Manila", "Quezon City", "Caloocan", "Las Piñas", "Makati",
    "Malabon", "Navotas", "Paranaque", "Pasay", "Muntinlupa",
    "Taguig", "Valenzuela", "Marikina", "Pasig", "San Juan",
    "Cavite", "Cebu", "Davao", "Iloilo", "Baguio", "Zamboanga",
    "Angeles", "Bacolod", "Batangas", "Cagayan de Oro", "Cebu City",
    "Davao City", "General Santos", "Iligan", "Kalibo", "Lapu-Lapu City",
    "Lucena", "Mandaue", "Olongapo", "Ormoc", "Oroquieta", "Ozamiz",
    "Palawan", "Puerto Princesa", "Roxas City", "San Pablo", "Silay"
]

GLOBAL_CAPITAL_CITIES = [
    "Washington D.C.", "London", "Paris", "Berlin", "Rome",
    "Moscow", "Beijing", "Tokyo", "Seoul", "New Delhi", "Islamabad",
    "Canberra", "Ottawa", "Brasilia", "Ottawa", "Cairo", "Nairobi",
    "Pretoria", "Kuala Lumpur", "Jakarta", "Bangkok", "Hanoi", "Athens",
    "Vienna", "Stockholm", "Oslo", "Copenhagen", "Helsinki", "Dublin",
    "Warsaw", "Prague", "Madrid", "Amsterdam", "Brussels", "Bern",
    "Wellington", "Santiago", "Buenos Aires", "Brasilia", "Abu Dhabi",
    "Doha", "Riyadh", "Kuwait City", "Muscat", "Manama", "Doha",
    "Beijing", "Shanghai", "Tokyo", "Seoul", "Sydney", "Melbourne"
]

@bot.tree.command(name="weather", description="Get weather information for a city")
@app_commands.describe(city="City name", unit="Temperature unit (default is Celsius)")
@app_commands.choices(unit=[
    app_commands.Choice(name="Celsius (°C)", value="c"),
    app_commands.Choice(name="Fahrenheit (°F)", value="f")
])
async def weather(interaction: discord.Interaction, city: str, unit: str = "c"):
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        await interaction.response.send_message("❌ Weather API key is missing.", ephemeral=True)
        return
    url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={city}"
    try:
        response = requests.get(url)
        data = response.json()
        if "error" in data:
            await interaction.response.send_message("❌ City not found or invalid input.", ephemeral=True)
            return
        current = data["current"]
        location = data["location"]["name"]
        region = data["location"]["region"]
        country = data["location"]["country"]
        if unit == "c":
            temperature = current["temp_c"]
            feels_like = current["feelslike_c"]
            unit_label = "°C"
        else:
            temperature = current["temp_f"]
            feels_like = current["feelslike_f"]
            unit_label = "°F"
        humidity = current["humidity"]
        wind_kph = current["wind_kph"]
        condition = current["condition"]["text"]
        icon_url = f"https:{current['condition']['icon']}"
        embed = discord.Embed(
            title=f"🌤️ Weather in {location}, {region}, {country}",
            color=discord.Color.blue()
        )
        embed.add_field(name="🌡️ Temperature", value=f"{temperature}{unit_label}", inline=True)
        embed.add_field(name="🧯 Feels Like", value=f"{feels_like}{unit_label}", inline=True)
        embed.add_field(name="💧 Humidity", value=f"{humidity}%", inline=True)
        embed.add_field(name="🌬️ Wind Speed", value=f"{wind_kph} km/h", inline=True)
        embed.add_field(name="📝 Condition", value=condition, inline=False)
        embed.set_thumbnail(url=icon_url)
        embed.set_footer(text="Powered by WeatherAPI • Neroniel")
        embed.timestamp = datetime.now(PH_TIMEZONE)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error fetching weather data: {str(e)}", ephemeral=True)

@weather.autocomplete('city')
async def city_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    # Combine Philippine and global capitals
    all_cities = PHILIPPINE_CITIES + GLOBAL_CAPITAL_CITIES
    # Filter based on user input
    filtered = [c for c in all_cities if current.lower() in c.lower()]
    return [
        app_commands.Choice(name=c, value=c)
        for c in filtered[:25]  # Max 25 choices
    ]

# ========== Utility Commands ==========
# User Info
@bot.tree.command(name="userinfo", description="Display detailed info about a user")
@app_commands.describe(member="Optional, defaults to you")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    if member is None:
        member = interaction.user
    created_at = member.created_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8")
    joined_at = member.joined_at.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.joined_at else "Unknown"
    roles = [role.mention for role in member.roles if not role.is_default()]
    roles_str = ", ".join(roles) if roles else "No Roles"
    boost_since = member.premium_since.astimezone(PH_TIMEZONE).strftime("%B %d, %Y • %I:%M %p GMT+8") if member.premium_since else "Not Boosting"
    embed = discord.Embed(title=f"👤 User Info for {member}", color=discord.Color.green())
    embed.add_field(name="Username", value=f"{member.mention}", inline=False)
    embed.add_field(name="Display Name", value=f"`{member.display_name}`", inline=True)
    embed.add_field(name="User ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Created Account", value=f"`{created_at}`", inline=False)
    embed.add_field(name="Joined Server", value=f"`{joined_at}`", inline=False)
    embed.add_field(name="Roles", value=roles_str, inline=False)
    embed.add_field(name="Server Booster Since", value=f"`{boost_since}`", inline=False)
    if member.bot:
        embed.add_field(name="Bot Account", value="✅ Yes", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    await interaction.response.send_message(embed=embed)

# Purge
@bot.tree.command(name="purge", description="Delete a specified number of messages")
@app_commands.describe(amount="How many messages would you like to delete?")
async def purge(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❗ Please specify a positive number of messages.", ephemeral=True)
        return
    BOT_OWNER_ID = 1163771452403761193
    has_permission = interaction.user.guild_permissions.manage_messages or interaction.user.id == BOT_OWNER_ID
    if not has_permission:
        await interaction.response.send_message("❗ You don't have permission to use this command.", ephemeral=True)
        return
    if not interaction.guild.me.guild_permissions.manage_messages:
        await interaction.response.send_message("❗ I don't have permission to delete messages.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"✅ Deleted **{len(deleted)}** messages.", ephemeral=True)

# Group Info
@bot.tree.command(name="group", description="Display information about the 1cy Roblox group")
async def groupinfo(interaction: discord.Interaction):
    group_id = 5838002
    try:
        response = requests.get(f"https://groups.roblox.com/v1/groups/ {group_id}")
        data = response.json()
        formatted_members = "{:,}".format(data['memberCount'])
        embed = discord.Embed(color=discord.Color.blue())
        embed.add_field(name="Group Name", value=f"[{data['name']}](https://www.roblox.com/groups/ {group_id})", inline=False)
        embed.add_field(name="Description", value=f"```\n{data['description'] or 'No description'}\n```", inline=False)
        embed.add_field(name="Group ID", value=str(data['id']), inline=True)
        owner = data['owner']
        owner_link = f"[{owner['username']}](https://www.roblox.com/users/ {owner['userId']}/profile)" if owner else "No owner"
        embed.add_field(name="Owner", value=owner_link, inline=True)
        embed.add_field(name="Members", value=formatted_members, inline=True)
        embed.set_footer(text="Neroniel")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error fetching group info: {e}", ephemeral=True)

# ===========================
# Developer Tools
# ===========================

# Owner-only check using BOT_OWNER_ID from .env
def is_owner():
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == int(os.getenv("BOT_OWNER_ID"))
    return app_commands.check(predicate)

@bot.tree.command(name="sync", description="Sync slash commands globally (Owner Only)")
@is_owner()
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    await interaction.response.send_message("✅ Slash commands have been synced globally.", ephemeral=True)

@bot.tree.command(name="reload", description="Reload cogs (Owner Only)")
@is_owner()
async def reload(interaction: discord.Interaction):
    # Placeholder for future cog reloading
    await interaction.response.send_message("ℹ️ No cogs to reload yet. Add cogs for full functionality.", ephemeral=True)

# ========== Fun Commands ==========
# Poll
@bot.tree.command(name="poll", description="Create a poll with up/down votes")
@app_commands.describe(question="Poll question", amount="Duration amount", unit="Time unit (seconds, minutes, hours)")
@app_commands.choices(unit=[
    app_commands.Choice(name="Seconds", value="seconds"),
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours", value="hours")
])
async def poll(interaction: discord.Interaction, question: str, amount: int, unit: app_commands.Choice[str]):
    total_seconds = {"seconds": amount, "minutes": amount * 60, "hours": amount * 3600}.get(unit.value, 0)
    if total_seconds <= 0:
        await interaction.response.send_message("❗ Invalid time unit.", ephemeral=True)
        return
    if total_seconds > 86400:
        await interaction.response.send_message("❗ Duration cannot exceed 24 hours.", ephemeral=True)
        return
    embed = discord.Embed(title="📊 Poll", description=question, color=discord.Color.orange())
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)
    message = await interaction.channel.send(embed=embed)
    await message.add_reaction("👍")
    await message.add_reaction("👎")
    await interaction.response.send_message("✅ Poll created!", ephemeral=True)
    await asyncio.sleep(total_seconds)
    message = await interaction.channel.fetch_message(message.id)
    reactions = message.reactions
    up_count = next((r.count for r in reactions if str(r.emoji) == "👍"), 0)
    down_count = next((r.count for r in reactions if str(r.emoji) == "👎"), 0)
    if up_count > down_count:
        result = "👍 Upvotes win!"
    elif down_count > up_count:
        result = "👎 Downvotes win!"
    else:
        result = "⚖️ It's a tie!"
    result_embed = discord.Embed(title="📊 Poll Results", description=question, color=discord.Color.green())
    result_embed.add_field(name="👍 Upvotes", value=str(up_count), inline=True)
    result_embed.add_field(name="👎 Downvotes", value=str(down_count), inline=True)
    result_embed.add_field(name="Result", value=result, inline=False)
    result_embed.set_footer(text="Poll has ended")
    result_embed.timestamp = datetime.now(PH_TIMEZONE)
    await message.edit(embed=result_embed)

# Remind Me
@bot.tree.command(name="remindme", description="Set a reminder after X minutes (will ping you in this channel)")
@app_commands.describe(minutes="How many minutes until I remind you?", note="Your reminder message")
async def remindme(interaction: discord.Interaction, minutes: int, note: str):
    if minutes <= 0:
        await interaction.response.send_message("❗ Please enter a positive number of minutes.", ephemeral=True)
        return
    reminder_time = datetime.utcnow() + timedelta(minutes=minutes)
    if reminders_collection:
        reminders_collection.insert_one({
            "user_id": interaction.user.id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "note": note,
            "reminder_time": reminder_time
        })
    await interaction.response.send_message(f"⏰ I'll remind you in `{minutes}` minutes: `{note}`", ephemeral=True)

# Donate
@bot.tree.command(name="donate", description="Donate Robux to a Discord user. (Only for fun!)")
@app_commands.describe(user="The user to donate to.", robux="The amount of Robux to donate.")
async def donate(interaction: discord.Interaction, user: discord.Member, robux: int):
    if robux <= 0:
        await interaction.response.send_message("❗ Robux amount must be greater than zero.", ephemeral=True)
        return
    await interaction.response.send_message(f"`{interaction.user.name}` just donated **{robux:,} Robux** to {user.mention}!")

# Say
@bot.tree.command(name="say", description="Make the bot say something in chat (no @everyone/@here allowed)")
@app_commands.describe(message="Message for the bot to say")
async def say(interaction: discord.Interaction, message: str):
    if "@everyone" in message or "@here" in message:
        await interaction.response.send_message("❌ You cannot use `@everyone` or `@here`.", ephemeral=True)
        return
    await interaction.response.send_message(message)

# Calculator
@bot.tree.command(name="calculator", description="Perform basic math operations (+, -, *, /)")
@app_commands.describe(num1="First number", operation="Operation", num2="Second number")
@app_commands.choices(operation=[
    app_commands.Choice(name="Addition (+)", value="add"),
    app_commands.Choice(name="Subtraction (-)", value="subtract"),
    app_commands.Choice(name="Multiplication (*)", value="multiply"),
    app_commands.Choice(name="Division (/)", value="divide")
])
async def calculator(interaction: discord.Interaction, num1: float, operation: app_commands.Choice[str], num2: float):
    if operation.value == "divide" and num2 == 0:
        await interaction.response.send_message("❌ Cannot divide by zero.", ephemeral=True)
        return
    try:
        if operation.value == "add":
            result = num1 + num2
            symbol = "+"
        elif operation.value == "subtract":
            result = num1 - num2
            symbol = "-"
        elif operation.value == "multiply":
            result = num1 * num2
            symbol = "*"
        elif operation.value == "divide":
            result = num1 / num2
            symbol = "/"
        await interaction.response.send_message(f"Result: `{num1} {symbol} {num2} = {result}`")
    except Exception as e:
        await interaction.response.send_message(f"⚠️ An error occurred: {str(e)}")

# List All Commands
@bot.tree.command(name="listallcommands", description="List all available slash commands")
async def listallcommands(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 All Available Commands",
        description="A categorized list of all commands for easy navigation.",
        color=discord.Color.blue()
    )

    # 🤖 AI Assistant
    embed.add_field(
        name="🤖 AI Assistant",
        value="""
        `/ask <prompt>` - Chat with Llama 3 AI  
        `/clearhistory` - Clear your AI conversation history
        """,
        inline=False
    )

    # 💰 Currency Conversion
    embed.add_field(
        name="💰 Currency & Robux Conversion",
        value="""
        `/payout <robux>` - Convert Robux to PHP (Payout rate)  
        `/gift <robux>` - Convert Robux to PHP (Gift rate)  
        `/nct <robux>` - Convert Robux to PHP (NCT rate)  
        `/ct <robux>` - Convert Robux to PHP (CT rate)  
        `/allrates <robux>` - Compare PHP values across all rates  
        `/beforetax <robux>` - How much you'll receive after tax  
        `/aftertax <target>` - How much to send to get desired amount
        """,
        inline=False
    )

    # 🛠️ Utility Tools
    embed.add_field(
        name="🛠️ Utility Tools",
        value="""
        `/userinfo [user]` - View detailed info about a user  
        `/purge <amount>` - Delete messages (requires mod permissions)  
        `/calculator <num1> <operation> <num2>` - Perform math operations  
        `/group` - Show info about the 1cy Roblox group  
        `/convertcurrency <amount> <from> <to>` - Convert between currencies  
        `/weather <city> [unit]` - Get weather in a city (supports autocomplete)
        """,
        inline=False
    )

    # 🕒 Reminders & Polls
    embed.add_field(
        name="⏰ Reminders & Polls",
        value="""
        `/remindme <minutes> <note>` - Set a personal reminder  
        `/poll <question> <time> <unit>` - Create a timed poll  
        """,
        inline=False
    )

    # 🎁 Fun Commands
    embed.add_field(
        name="🎉 Fun",
        value="""
        `/donate <user> <amount>` - Donate Robux to someone (for fun)  
        `/say <message>` - Make the bot say something (no @everyone/@here)
        """,
        inline=False
    )

    # 🔧 Developer Tools
    embed.add_field(
        name="🔧 Developer Tools",
        value="""
        `/sync` - Sync slash commands globally (owner only)  
        `/reload` - Reload cogs (owner only)
        """,
        inline=False
    )

    # Footer
    embed.set_footer(text="Neroniel")
    embed.timestamp = datetime.now(PH_TIMEZONE)

    await interaction.response.send_message(embed=embed)

# ===========================
# Start the Bot
# ===========================
@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    await bot.tree.sync()
    print("All commands synced!")
    group_id = 5838002
    while True:
        try:
            response = requests.get(f"https://groups.roblox.com/v1/groups/ {group_id}")
            data = response.json()
            member_count = "{:,}".format(data['memberCount'])
            await bot.change_presence(status=discord.Status.dnd, activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"1cy | {member_count} Members"
            ))
        except Exception as e:
            print(f"Error fetching group info: {str(e)}")
            await bot.change_presence(status=discord.Status.dnd, activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="1cy"
            ))
        await asyncio.sleep(60)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    content = message.content.lower()
    if content == "hobie":
        await message.reply("mapanghe")
    elif content == "neroniel":
        await message.reply("masarap")
    elif content == "hi":
        reply = (
            "hi tapos ano? magiging friends tayo? lagi tayong mag-uusap mula umaga hanggang madaling araw? "
            "tas magiging close tayo? sa sobrang close natin nahuhulog na tayo sa isa't isa, tapos ano? "
            "liligawan mo ko ako? sasagutin naman kita. paplanuhin natin yung pangarap natin sa isa't isa "
            "tapos ano? may makikita kang iba. magsasawa ka na, iiwan mo ako. tapos ano? magmamakaawa ako sayo "
            "kasi mahal kita pero ano? wala kang gagawin, hahayaan mo lang akong umiiyak while begging you to stay. kaya wag na lang. thanks nalang sa hi mo"
        )
        await message.reply(reply)
    auto_react_channels = [
        1225294057371074760,
        1107600826664501258,
        1107591404877791242,
        1368123462077513738
    ]
    if message.channel.id in auto_react_channels:
        await message.add_reaction("🎀")
    if message.channel.id == 1107281584337461321:
        await message.add_reaction("<:1cy_heart:1258694384346468362>")
    await bot.process_commands(message)

# Run the bot
bot.run(os.getenv("DISCORD_TOKEN"))
