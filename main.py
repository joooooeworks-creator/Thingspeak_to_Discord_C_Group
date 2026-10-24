import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import ntplib
import requests
from discord.ext import commands


CONFIG_PATH = Path(__file__).with_name("config.json")
SCHEDULE_MINUTES = tuple(range(0, 60, 5))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def makeconfig():
    config = {
        "Thingspeak": {
            "ThingSpeak_channel_id": "YOUR_THINGSPEAK_CHANNEL_ID",
            "read_api_key": "YOUR_THINGSPEAK_READ_API_KEY",
            "field_numbers": [1],
        },
        "Discord": {
            "bot_token": "YOUR_BOT_TOKEN",
            "command_prefix": "!",
            "Discord_channel_id": [],
        },
        "ntp": {
            "ntp_server": "ntp.nict.jp",
            "timezone_offset": 9,
        },
    }
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    logger.info("config.json was created. Set your ThingSpeak and Discord credentials.")


def ensure_config():
    if not CONFIG_PATH.exists():
        makeconfig()


def getconfig(section, key):
    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)
    return config[section][key]


def save_config(config):
    temporary_path = CONFIG_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    temporary_path.replace(CONFIG_PATH)


def read_discord_channel_ids(config):
    configured_ids = config["Discord"]["Discord_channel_id"]
    if not isinstance(configured_ids, list):
        raise TypeError("Discord.Discord_channel_id must be a list.")

    channel_ids = []
    for channel_id in configured_ids:
        try:
            channel_ids.append(int(channel_id))
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid Discord channel ID: %r", channel_id)
    return channel_ids


def get_ntp_time():
    ntp_server = getconfig("ntp", "ntp_server")
    timezone_offset = getconfig("ntp", "timezone_offset")
    local_timezone = timezone(timedelta(hours=int(timezone_offset)))

    try:
        response = ntplib.NTPClient().request(ntp_server, version=3, timeout=5)
        return datetime.fromtimestamp(response.tx_time, tz=timezone.utc).astimezone(local_timezone)
    except (ntplib.NTPException, OSError) as error:
        logger.warning("NTP time unavailable; using system time: %s", error)
        return datetime.now(local_timezone)


def get_thingspeak_data(field_num: int) -> int | None:
    channel_id = getconfig("Thingspeak", "ThingSpeak_channel_id")
    read_api_key = getconfig("Thingspeak", "read_api_key")
    if not channel_id or channel_id.startswith("YOUR_") or not read_api_key or read_api_key.startswith("YOUR_"):
        logger.error("ThingSpeak channel ID or read API key is not configured.")
        return None

    url = (
        f"https://api.thingspeak.com/channels/{channel_id}/fields/"
        f"{field_num}.json"
    )
    try:
        response = requests.get(
            url,
            params={"api_key": read_api_key, "results": 1},
            timeout=10,
        )
        response.raise_for_status()
        feeds = response.json().get("feeds", [])
        if not feeds or feeds[0].get(f"field{field_num}") is None:
            logger.warning("No data found for ThingSpeak field %s.", field_num)
            return None
        return int(feeds[0][f"field{field_num}"])
    except (requests.RequestException, ValueError) as error:
        logger.error("Could not get integer data for ThingSpeak field %s: %s", field_num, error)
        return None


async def send_discord_msg(bot, message):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    channel_ids = read_discord_channel_ids(config)
    for channel_id in channel_ids:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(channel_id))
            except discord.HTTPException:
                logger.exception("Could not access Discord channel %s.", channel_id)
                continue
        try:
            await channel.send(message)
        except discord.HTTPException:
            logger.exception("Could not send a message to Discord channel %s.", channel_id)


async def scheduled_task(bot):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    field_numbers = config.get("Thingspeak", {}).get("field_numbers", [1])
    readings = []
    for field_num in field_numbers:
        value = await asyncio.to_thread(get_thingspeak_data, int(field_num))
        if value is not None:
            readings.append(f"field{field_num}: {value}")

    if readings:
        await send_discord_msg(bot, "\n".join(readings))
    else:
        logger.warning("No ThingSpeak readings to send this time.")


def next_schedule_time(now):
    next_minute = next((minute for minute in SCHEDULE_MINUTES if minute > now.minute), None)
    if next_minute is None:
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return now.replace(minute=next_minute, second=0, microsecond=0)


async def schedule_loop(bot):
    while not bot.is_closed():
        current_time = await asyncio.to_thread(get_ntp_time)
        scheduled_time = next_schedule_time(current_time)
        await asyncio.sleep(max(0, (scheduled_time - current_time).total_seconds()))
        await scheduled_task(bot)


def Discord_init():
    bot_token = getconfig("Discord", "bot_token")
    command_prefix = getconfig("Discord", "command_prefix")
    if not bot_token or bot_token.startswith("YOUR_"):
        logger.error("Set a valid Discord bot token in config.json before starting.")
        return None

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix=command_prefix, intents=intents)
    channel_lock = asyncio.Lock()

    @bot.command(name="add")
    async def add_channel(ctx):
        async with channel_lock:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            channel_ids = read_discord_channel_ids(config)
            if ctx.channel.id in channel_ids:
                await ctx.send("このチャンネルはすでに登録されています。")
                return
            channel_ids.append(ctx.channel.id)
            config["Discord"]["Discord_channel_id"] = channel_ids
            save_config(config)
        await ctx.send("このチャンネルを送信先に登録しました。")

    @bot.command(name="remove")
    async def remove_channel(ctx):
        async with channel_lock:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            channel_ids = read_discord_channel_ids(config)
            if ctx.channel.id not in channel_ids:
                await ctx.send("このチャンネルは登録されていません。")
                return
            config["Discord"]["Discord_channel_id"] = [
                channel_id for channel_id in channel_ids if channel_id != ctx.channel.id
            ]
            save_config(config)
        await ctx.send("このチャンネルの登録を解除しました。")

    @bot.event
    async def on_ready():
        logger.info("Logged in as %s", bot.user)
        if not hasattr(bot, "schedule_task") or bot.schedule_task.done():
            bot.schedule_task = asyncio.create_task(schedule_loop(bot))

    return bot


def main():
    ensure_config()
    bot = Discord_init()
    if bot is not None:
        bot.run(getconfig("Discord", "bot_token"))


if __name__ == "__main__":
    main()
