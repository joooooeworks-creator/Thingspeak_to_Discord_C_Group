import asyncio
import json
import logging
import math
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
    config = read_config()
    return config[section][key]


def read_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


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


def get_ntp_time(config=None):
    config = config or read_config()
    ntp_server = config["ntp"]["ntp_server"]
    timezone_offset = config["ntp"]["timezone_offset"]
    local_timezone = timezone(timedelta(hours=int(timezone_offset)))

    try:
        response = ntplib.NTPClient().request(ntp_server, version=3, timeout=5)
        return datetime.fromtimestamp(response.tx_time, tz=timezone.utc).astimezone(local_timezone)
    except (ntplib.NTPException, OSError) as error:
        logger.warning("NTP time unavailable; using system time: %s", error)
        return datetime.now(local_timezone)


def get_thingspeak_data(config=None) -> list[int | float]:
    config = config or read_config()
    channel_id = config["Thingspeak"]["ThingSpeak_channel_id"]
    read_api_key = config["Thingspeak"]["read_api_key"]
    if not channel_id or channel_id.startswith("YOUR_") or not read_api_key or read_api_key.startswith("YOUR_"):
        logger.error("ThingSpeak channel ID or read API key is not configured.")
        return []

    url = f"https://api.thingspeak.com/channels/{channel_id}/feeds.json"
    try:
        response = requests.get(
            url,
            params={"api_key": read_api_key, "results": 1},
            timeout=10,
        )
        response.raise_for_status()
        feeds = response.json().get("feeds", [])
        if not feeds:
            logger.warning("No ThingSpeak feed data found.")
            return []

        feed = feeds[0]
        field_names = sorted(
            (
                key for key in feed
                if key.startswith("field") and key[5:].isdigit()
            ),
            key=lambda key: int(key[5:]),
        )
        values = []
        for field_name in field_names:
            raw_value = feed[field_name]
            if raw_value is None:
                continue
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric ThingSpeak value for %s: %r", field_name, raw_value)
                continue
            if not math.isfinite(numeric_value):
                logger.warning("Ignoring non-finite ThingSpeak value for %s: %r", field_name, raw_value)
                continue
            values.append(
                int(numeric_value) if numeric_value.is_integer() else numeric_value
            )
        return values
    except (requests.RequestException, ValueError) as error:
        logger.error("Could not get ThingSpeak data: %s", error)
        return []


async def send_discord_msg(bot, message, config=None):
    config = config or read_config()
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


async def process_data(bot, data, config):
    """Apply optional processing rules before sending the data."""
    if not data:
        logger.warning("No ThingSpeak readings to process this time.")
        return

    send_data = data
    await send_discord_msg(bot, str(send_data), config)


def next_schedule_time(now):
    next_minute = next((minute for minute in SCHEDULE_MINUTES if minute > now.minute), None)
    if next_minute is None:
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return now.replace(minute=next_minute, second=0, microsecond=0)


def Discord_init(config=None):
    config = config or read_config()
    bot_token = config["Discord"]["bot_token"]
    command_prefix = config["Discord"]["command_prefix"]
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

    return bot


def main():
    ensure_config()
    config = read_config()
    bot = Discord_init(config)
    if bot is not None:
        async def loop():
            logger.info("Logged in as %s", bot.user)
            while not bot.is_closed():
                config = read_config()
                current_time = await asyncio.to_thread(get_ntp_time, config)
                scheduled_time = next_schedule_time(current_time)
                await asyncio.sleep(max(0, (scheduled_time - current_time).total_seconds()))

                config = read_config()
                data = await asyncio.to_thread(get_thingspeak_data, config)
                await process_data(bot, data, config)

        async def setup_hook():
            bot.loop_task = asyncio.create_task(loop())

        bot.setup_hook = setup_hook
        bot.run(config["Discord"]["bot_token"])


if __name__ == "__main__":
    main()
