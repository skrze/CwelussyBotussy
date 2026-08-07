import asyncio
import os
import traceback

import discord
import requests
from bs4 import BeautifulSoup
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # reads TOKEN from .env

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not found. Put it in your .env file (see .env.example).")

intents = discord.Intents.default()
intents.message_content = True  # required for ?prefix commands to be read

bot = commands.Bot(command_prefix="?", intents=intents)


@bot.tree.command(name="test", description="A simple test command")
async def test_slash(interaction: discord.Interaction):
    await interaction.response.send_message("cwel")


@bot.command(name="test")
async def test_prefix(ctx: commands.Context):
    await ctx.send("chuj ci w dupe cwelu")

@bot.command(name= "cwel")
async def test_prefix(ctx: commands.context):
    await ctx.send("sam jestes cwel")


STARBASE_URL = "https://www.starbase.texas.gov/beach-road-access"


def fetch_starbase_status() -> tuple[str, str]:
    """Blocking scrape of the Starbase beach/road status page. Run via asyncio.to_thread."""
    resp = requests.get(STARBASE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    beach_container = soup.find(id="beach-closure")
    beach_text = (
        beach_container.find("div", class_="cms-big-text").get_text(strip=True)
        if beach_container
        else "Brak danych o statusie plaży."
    
    )

    road_container = soup.find(id="road-closure")
    road_text = "Brak danych o drodze."
    if road_container:
        visible = [
            el.get_text(strip=True)
            for el in road_container.find_all("div", class_="cms-big-text")
            if "w-condition-invisible" not in (el.get("class") or [])
        ]
        if visible:
            road_text = " ".join(visible)
    return beach_text, road_text


async def build_starbase_embed() -> discord.Embed:
    beach_text, road_text = await asyncio.to_thread(fetch_starbase_status)

    beach_open = "open" in beach_text.lower() and "closed" not in beach_text.lower()
    road_clear = "no road delays" in road_text.lower()
    color = discord.Color.green() if beach_open and road_clear else discord.Color.orange()

    embed = discord.Embed(
        title="Starbase — status plaży i drogi (Hwy 4)",
        url=STARBASE_URL,
        color=color,
    )
    embed.add_field(name="🏖️ Plaża (Boca Chica)", value=beach_text, inline=False)
    embed.add_field(name="🛣️ Droga (Highway 4)", value=road_text, inline=False)
    embed.set_footer(text="Źródło: starbase.texas.gov")
    return embed


@bot.tree.command(name="zamkniecia", description="Sprawdź aktualny status plaży i drogi HWY4 w Starbase")
async def zamkniecia_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        embed = await build_starbase_embed()
        await interaction.followup.send(embed=embed)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Nie udało się pobrać danych ze strony starbase.texas.gov.")


@bot.command(name="zamkniecia")
async def zamkniecia_prefix(ctx: commands.Context):
    async with ctx.typing():
        try:
            embed = await build_starbase_embed()
            await ctx.send(embed=embed)
        except Exception:
            traceback.print_exc()
            await ctx.send("Nie udało się pobrać danych ze strony starbase.texas.gov.")


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands synced. Try /test or ?test in your server.")


bot.run(TOKEN)