import asyncio
import io
import json
import os
import re
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
import requests
from bs4 import BeautifulSoup
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()  # reads TOKEN from .env

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not found. Put it in your .env file (see .env.example).")

intents = discord.Intents.default()
intents.message_content = True  # required for .prefix commands to be read

bot = commands.Bot(command_prefix=".", intents=intents)

@bot.tree.command(name="test", description="A simple test command")
async def test_slash(interaction: discord.Interaction):
    await interaction.response.send_message("cwel")
@bot.command(name="test")
async def test_prefix(ctx: commands.Context):
    await ctx.send(f" Masz {round(bot.latency * 1000)} ms pingu, cwelu")
@bot.command(name="cwel")
async def cwel_command(ctx: commands.Context):
    await ctx.send("sam jestes cwel")


STARBASE_URL = "https://www.starbase.texas.gov/beach-road-access"

# Starbase publishes closure windows in local Texas (Boca Chica) time.
STARBASE_TZ = ZoneInfo("America/Chicago")

DATE_RANGE_RE = re.compile(r"Date:\s*(.+?)\s+to\s+(.+)$", re.IGNORECASE)
DATE_PART_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2})\s+(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)

def _parse_starbase_datetime(raw: str, year: int) -> datetime | None:
    match = DATE_PART_RE.search(raw)
    if not match:
        return None
    month, day, time_part = match.groups()
    try:
        naive = datetime.strptime(f"{month} {day} {year} {time_part}", "%B %d %Y %I:%M %p")
    except ValueError:
        return None
    return naive.replace(tzinfo=STARBASE_TZ)

def _parse_starbase_date_range(detail_text: str) -> tuple[datetime, datetime] | None:
    match = DATE_RANGE_RE.search(detail_text)
    if not match:
        return None
    start_raw, end_raw = match.group(1).strip(), match.group(2).strip()

    now_year = datetime.now(STARBASE_TZ).year
    start_dt = _parse_starbase_datetime(start_raw, now_year)
    end_dt = _parse_starbase_datetime(end_raw, now_year)
    if start_dt is None or end_dt is None:
        return None
    if end_dt < start_dt:
        end_dt = end_dt.replace(year=end_dt.year + 1)
    return start_dt, end_dt

def _hammer(dt: datetime, style: str = "f") -> str:
    """Discord timestamp markup (a.k.a. "hammertime") — Discord renders this
    client-side in each reader's own local timezone and language, so Polish
    readers see Polish local time automatically without us converting."""
    return f"<t:{int(dt.timestamp())}:{style}>"

def _format_road_status(status: str, detail_text: str) -> str:
    if not detail_text:
        return status
    date_range = _parse_starbase_date_range(detail_text)
    if date_range:
        start_dt, end_dt = date_range
        hammer_range = f"{_hammer(start_dt, 'f')} - {_hammer(end_dt, 't')}"
        detail_text = DATE_RANGE_RE.sub(f"Date: {hammer_range}", detail_text)
    return f"{status}\n{detail_text}"

def fetch_starbase_page() -> tuple[str, str | None, str]:
    """Returns (beach_text, road_status, road_detail_text). road_status/detail
    are the raw scraped strings (Texas-local dates, not yet hammer-formatted)
    so callers can either format them for display or parse the date range."""
    resp = requests.get(
        STARBASE_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    beach_container = soup.find(id="beach-closure")
    beach_text = "Brak danych o statusie plaży."

    #tymczasowa poprawka dla jacusia
    if beach_container:
        beach_text = beach_container.get_text(" ", strip=True)

    road_container = soup.find(id="road-closure")
    road_status, road_detail_text = None, ""
    if road_container:
        notice = road_container.find("div", class_="notice-container-no-hover")
        if notice:
            road_status = next(
                (
                    el.get_text(strip=True)
                    for el in notice.find_all("div", class_="cms-big-text")
                    if "w-condition-invisible" not in (el.get("class") or [])
                ),
                None,
            )
            if road_status:
                detail_el = notice.find(id="rich-notification")
                road_detail_text = detail_el.get_text("\n", strip=True) if detail_el else ""
    return beach_text, road_status, road_detail_text

def fetch_starbase_status() -> tuple[str, str]:
    beach_text, road_status, road_detail_text = fetch_starbase_page()
    road_text = (
        _format_road_status(road_status, road_detail_text)
        if road_status else "Brak danych o drodze."
    )
    return beach_text, road_text

async def build_starbase_embed() -> discord.Embed:
    beach_text, road_text = await asyncio.to_thread(
        fetch_starbase_status
    )
    beach_open = (
        "open" in beach_text.lower()
        and "closed" not in beach_text.lower()
    )
    road_clear = (
        "no road delays"
        in road_text.lower()
    )
    color = (
        discord.Color.green()
        if beach_open and road_clear
        else discord.Color.orange()
    )
    embed = discord.Embed(
        title="Starbase — status plaży i drogi (Hwy 4)",
        url=STARBASE_URL,
        color=color,
    )
    embed.add_field(
        name="🏖️ Plaża (Boca Chica)",
        value=beach_text,
        inline=False
    )
    embed.add_field(
        name="🛣️ Droga (Highway 4)",
        value=road_text,
        inline=False
    )
    embed.set_footer(text="Źródło: starbase.texas.gov")
    return embed

@bot.tree.command(
    name="zamkniecia",
    description="Sprawdź aktualny status plaży i drogi HWY4 w Starbase"
)

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
        except Exception as e:
            traceback.print_exc()
            await ctx.send(f"wyjebka: `{type(e).__name__}: {e}`")

async def build_tcd_message() -> str:
    """Time-to-closure: how long until the next HWY4 closure starts, or (if
    one is already underway) how long until it ends."""
    _beach_text, road_status, road_detail_text = await asyncio.to_thread(fetch_starbase_page)
    date_range = _parse_starbase_date_range(road_detail_text) if road_status else None
    if date_range is None:
        if road_status and "no road delays" in road_status.lower():
            return "✅ Brak zaplanowanego zamknięcia drogi HWY4."
        return "Nie udało się odczytać godzin zamknięcia ze strony starbase.texas.gov."

    start_dt, end_dt = date_range
    now = datetime.now(STARBASE_TZ)
    if now < start_dt:
        return f"🚧 Zamknięcie HWY4 zacznie się {_hammer(start_dt, 'R')} ({_hammer(start_dt, 'f')})"
    if now <= end_dt:
        return f"🚧 Zamknięcie HWY4 trwa, koniec {_hammer(end_dt, 'R')} ({_hammer(end_dt, 'f')})"
    return f"✅ Ostatnie znane zamknięcie HWY4 zakończyło się {_hammer(end_dt, 'R')} ({_hammer(end_dt, 'f')})"

@bot.tree.command(
    name="tcd",
    description="Czas do najbliższego zamknięcia HWY4 w Starbase (lub do jego końca, jeśli już trwa)"
)
async def tcd_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        await interaction.followup.send(await build_tcd_message())
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Nie udało się pobrać danych ze strony starbase.texas.gov.")
@bot.command(name="tcd")
async def tcd_prefix(ctx: commands.Context):
    async with ctx.typing():
        try:
            await ctx.send(await build_tcd_message())
        except Exception as e:
            traceback.print_exc()
            await ctx.send(f"wyjebka: `{type(e).__name__}: {e}`")

TFR_API_URL = "https://tfr.faa.gov/tfrapi/getTfrList"
TFR_INFO_URL = "https://tfr.faa.gov/tfr3/?tfrid={notam_id}"
TFR_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tfr_seen.json")
STARBASE_TFR_KEYWORDS = ("brownsville", "boca chica", "south padre island")

def fetch_starbase_tfrs() -> list[dict]:
    resp = requests.get(
        TFR_API_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        tfr for tfr in data
        if tfr.get("state") == "TX"
        and any(kw in tfr.get("description", "").lower() for kw in STARBASE_TFR_KEYWORDS)
    ]

def load_seen_tfr_ids() -> set[str] | None:
    """None means "no baseline recorded yet" (e.g. first ever run, or a
    fresh deploy where tfr_seen.json doesn't exist) — distinct from an empty
    set, which means we've checked before and genuinely saw nothing."""
    try:
        with open(TFR_SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_seen_tfr_ids(ids: set[str]) -> None:
    with open(TFR_SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)

def _consume_new_tfr_ids(tfrs: list[dict]) -> tuple[set[str], set[str]]:
    """Diffs the current TFR ids against what was last persisted, then
    persists the current set. Called once per check (manual or background)
    so a new TFR is only ever flagged "new" the first time it's seen. On the
    very first check ever (no baseline yet) nothing is flagged "new" — it
    just establishes the baseline, instead of announcing every currently
    active TFR as if it just appeared."""
    seen_ids = load_seen_tfr_ids()
    current_ids = {tfr["notam_id"] for tfr in tfrs}
    new_ids = set() if seen_ids is None else current_ids - seen_ids
    save_seen_tfr_ids(current_ids)
    return current_ids, new_ids

SPACE_NOTICES_HOME_URL = "https://space-notices.com/"
SPACE_NOTICES_IMAGE_URL = "https://space-notices.com/og/entry/{slug}/{index}"
SPACE_NOTICES_MAX_IMAGES = 9  # Discord caps a message at 10 embeds; 1 is reserved for the text embed
SPACE_NOTICES_ENTRY_LINK = "https://space-notices.com/entry/{slug}"
STARBASE_TESTING_COLLECTION_SLUG = "collection-starbase-testing"

def _extract_rsc_json(html: str, key: str):
    """Next.js streams page data as escaped JSON inside self.__next_f.push(...) chunks.
    This pulls out the value for a top-level "key": by walking brackets (string-aware,
    since polygon/name fields can themselves contain [ ] characters)."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    payload = "".join(chunks).encode("utf-8").decode("unicode_escape")

    marker = f'"{key}":'
    idx = payload.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    opening = payload[start]
    if opening not in "[{":
        return None
    closing = "]" if opening == "[" else "}"

    depth, in_string, escape = 0, False, False
    i = start
    while i < len(payload):
        c = payload[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == opening:
            depth += 1
        elif c == closing:
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    return json.loads(payload[start:i])

def fetch_entry_notices(slug: str) -> list[dict]:
    resp = requests.get(
        SPACE_NOTICES_ENTRY_LINK.format(slug=slug),
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15
    )
    resp.raise_for_status()
    groups = _extract_rsc_json(resp.text, "noticeGroups") or []
    notices = []
    for group in groups:
        notices.extend(group.get("notices", []))
    return notices

def fetch_active_entry_slugs() -> list[str]:
    """Every entry currently listed on the space-notices.com homepage
    (collections and individual launches, active right now)."""
    resp = requests.get(
        SPACE_NOTICES_HOME_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15
    )
    resp.raise_for_status()
    slugs, seen = [], set()
    for slug in re.findall(r'"/entry/([a-z0-9][a-z0-9-]*)"', resp.text):
        if slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs

_ENTRY_SLUG_PREFIXES = ("launch-", "collection-", "other-")

def _prettify_slug(slug: str) -> str:
    name = slug
    for prefix in _ENTRY_SLUG_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("-", " ").title()

def fetch_active_entries() -> list[tuple[str, str]]:
    """Returns (slug, display_name) pairs for the dropdown menu."""
    return [(slug, _prettify_slug(slug)) for slug in fetch_active_entry_slugs()]

def _fetch_entry_images(slug: str) -> list[tuple[bytes, str, str, int]]:
    """Each leg of an entry's trajectory (e.g. Gulf/Caribbean ascent, Indian
    Ocean re-entry) gets its own hazard-map image at /og/entry/<slug>/<index>.
    Probes sequential indices until the server 404s."""
    images = []
    for index in range(SPACE_NOTICES_MAX_IMAGES):
        img_resp = requests.get(
            SPACE_NOTICES_IMAGE_URL.format(slug=slug, index=index),
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        if img_resp.status_code != 200 or not img_resp.content:
            break
        ext = "webp" if "webp" in img_resp.headers.get("Content-Type", "") else "png"
        images.append((img_resp.content, f"tfr_{slug}_{index}.{ext}", slug, index))
    return images

def fetch_starbase_launch_images() -> list[tuple[bytes, str, str, int]]:
    """space-notices.com only renders real hazard-map images for an active
    Starship launch entry (not for the static-fire/WDR testing notices)."""
    resp = requests.get(
        SPACE_NOTICES_HOME_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15
    )
    resp.raise_for_status()
    match = re.search(r'"(launch-starship-flight-\d+)"', resp.text)
    if not match:
        return []
    return _fetch_entry_images(match.group(1))

def _extract_notam_number(name: str) -> str | None:
    """space-notices.com notice names look like 'FDC 6/2751'; FAA's notam_id
    for the same NOTAM is just '6/2751'."""
    match = re.search(r"\d+/\d+", name)
    return match.group(0) if match else None

def _format_space_notice_lines(notices: list[dict]) -> str:
    lines = []
    for n in notices:
        label = f"`{n.get('type', '?')}` {n.get('name', n.get('id', '?'))}"
        lines.append(f"~~{label}~~" if n.get("cancelled") else label)
    return "\n".join(lines)

def _build_image_embeds(
    images: list[tuple[bytes, str, str, int]], link_url: str
) -> tuple[list[discord.Embed], list[discord.File]]:
    """One embed per image, all sharing the same url so Discord groups them
    into a single gallery instead of showing unrelated separate cards."""
    embeds, files = [], []
    for content, filename, _slug, _index in images:
        files.append(discord.File(io.BytesIO(content), filename=filename))
        img_embed = discord.Embed(url=link_url)
        img_embed.set_image(url=f"attachment://{filename}")
        embeds.append(img_embed)
    return embeds, files

async def _build_tfr_content(tfrs: list[dict], new_ids: set[str]) -> tuple[list[discord.Embed], list[discord.File]]:
    if not tfrs:
        return [discord.Embed(
            title="TFR — Starbase / Boca Chica",
            description="Brak aktywnych TFR w rejonie Starbase.",
            color=discord.Color.green(),
        )], []

    try:
        space_notices = await asyncio.to_thread(fetch_entry_notices, STARBASE_TESTING_COLLECTION_SLUG)
    except Exception:
        traceback.print_exc()
        space_notices = []
    confirmed_numbers = {
        num for n in space_notices
        if not n.get("cancelled") and (num := _extract_notam_number(n.get("name", "")))
    }

    embed = discord.Embed(
        title="TFR — Starbase / Boca Chica",
        description=(
            f"🆕 Wykryto {len(new_ids)} nowy(ch) TFR!" if new_ids
            else "Brak nowych TFR od ostatniego sprawdzenia."
        ),
        color=discord.Color.red() if new_ids else discord.Color.orange(),
    )
    for tfr in tfrs:
        mark = "🆕 " if tfr["notam_id"] in new_ids else ""
        confirmation = (
            "✅ potwierdzone na space-notices.com" if tfr["notam_id"] in confirmed_numbers
            else "⚠️ brak potwierdzenia na space-notices.com"
        )
        embed.add_field(
            name=f"{mark}NOTAM {tfr['notam_id']} ({tfr.get('type', '?')})",
            value=f"{tfr.get('description', 'brak opisu')}\n"
                  f"{confirmation}\n"
                  f"[Szczegóły]({TFR_INFO_URL.format(notam_id=tfr['notam_id'])})",
            inline=False,
        )

    embeds = [embed]
    files = []
    try:
        images = await asyncio.to_thread(fetch_starbase_launch_images)
    except Exception:
        traceback.print_exc()
        images = []
    if images:
        slug = images[0][2]
        embed.add_field(
            name="🗺️ Mapy zagrożenia",
            value=f"[{slug}]({SPACE_NOTICES_ENTRY_LINK.format(slug=slug)}) — {len(images)} zdjęcie(a)",
            inline=False,
        )
        extra_embeds, files = _build_image_embeds(images, SPACE_NOTICES_ENTRY_LINK.format(slug=slug))
        embeds.extend(extra_embeds)

    embed.set_footer(text="Źródło: tfr.faa.gov + space-notices.com")
    return embeds, files

async def build_tfr_embed() -> tuple[list[discord.Embed], list[discord.File]]:
    tfrs = await asyncio.to_thread(fetch_starbase_tfrs)
    _, new_ids = _consume_new_tfr_ids(tfrs)
    return await _build_tfr_content(tfrs, new_ids)

NOTIFY_CHANNEL_IDS = (1535271197304946688, 1535293117941555210)
TFR_CHECK_INTERVAL_MINUTES = 1

async def _get_notify_channels() -> list[discord.abc.Messageable]:
    channels = []
    for channel_id in NOTIFY_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                traceback.print_exc()
                continue
        channels.append(channel)
    return channels

@tasks.loop(minutes=TFR_CHECK_INTERVAL_MINUTES)
async def watch_for_new_tfrs():
    try:
        tfrs = await asyncio.to_thread(fetch_starbase_tfrs)
    except Exception:
        traceback.print_exc()
        return

    _, new_ids = _consume_new_tfr_ids(tfrs)
    if not new_ids:
        return

    channels = await _get_notify_channels()
    if not channels:
        return

    try:
        embeds, files = await _build_tfr_content(tfrs, new_ids)
    except Exception:
        traceback.print_exc()
        return

    for channel in channels:
        for f in files:
            f.reset()
        try:
            await channel.send(
                content=f"🚨 Wykryto {len(new_ids)} nowy(ch) TFR nad Starbase!",
                embeds=embeds,
                files=files or discord.utils.MISSING,
            )
        except Exception:
            traceback.print_exc()

@watch_for_new_tfrs.before_loop
async def before_watch_for_new_tfrs():
    await bot.wait_until_ready()

ROAD_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "road_state.json")
ROAD_CHECK_INTERVAL_MINUTES = 1

def load_road_closed_state() -> bool | None:
    """None means "no baseline recorded yet" (first ever run, or a fresh
    deploy where road_state.json doesn't exist) — distinct from False,
    which means we've checked before and it was genuinely open."""
    try:
        with open(ROAD_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("closed")
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_road_closed_state(closed: bool) -> None:
    with open(ROAD_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"closed": closed}, f)

def _classify_road_status(road_text: str) -> bool | None:
    """True = confirmed closed, False = confirmed clear, None = the scrape
    didn't get a real answer this cycle. Starbase's CMS list for road status
    occasionally renders with zero items even though the container element
    exists, which falls back to fetch_starbase_status's placeholder text —
    that must never be read as "closed", or every empty scrape looks like
    a brand new closure."""
    lowered = road_text.lower()
    if "no road delays" in lowered:
        return False
    if not road_text or road_text == "Brak danych o drodze.":
        return None
    return True

@tasks.loop(minutes=ROAD_CHECK_INTERVAL_MINUTES)
async def watch_for_road_closures():
    try:
        _beach_text, road_text = await asyncio.to_thread(fetch_starbase_status)
    except Exception:
        traceback.print_exc()
        return

    is_closed = _classify_road_status(road_text)
    if is_closed is None:
        return  # couldn't read the page this cycle; leave state alone and retry next interval

    was_closed = load_road_closed_state()
    save_road_closed_state(is_closed)
    # On the very first check ever (was_closed is None) just establish the
    # baseline — don't announce an already-ongoing closure as if it just happened.
    if not is_closed or was_closed is None or was_closed:
        return

    channels = await _get_notify_channels()
    if not channels:
        return

    embed = discord.Embed(
        title="🚧 Zamknięcia HWY4",
        description=road_text,
        url=STARBASE_URL,
        color=discord.Color.orange(),
    )
    embed.set_footer(text="Źródło: starbase.texas.gov")
    for channel in channels:
        try:
            await channel.send(content="🚧 Pojawiło się zamknięcie drogi w Starbase!", embed=embed)
        except Exception:
            traceback.print_exc()

@watch_for_road_closures.before_loop
async def before_watch_for_road_closures():
    await bot.wait_until_ready()

SPACEX_TWITTER_HANDLE = "SpaceX"
# Public Nitter-compatible mirrors — these come and go (many now sit behind
# an "Anubis" bot-check that returns 200/403 with an HTML challenge page
# instead of RSS), so we try a short list in order and use whichever one
# responds with real XML. nitter.net first since it's currently the most
# reliable of the bunch; the rest are kept as fallbacks in case it goes down.
SPACEX_NITTER_INSTANCES = [
    "https://nitter.net",
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.privacyredirect.com",
]
SPACEX_TWEET_LINK_RE = re.compile(r"/status/(\d+)")
SPACEX_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spacex_tweets_seen.json")
SPACEX_CHECK_INTERVAL_MINUTES = 2

def fetch_spacex_tweets() -> list[dict]:
    """Returns SpaceX's recent tweets as {id, url, text, published}, newest
    first. Tries each Nitter mirror in turn until one returns a usable feed."""
    for base in SPACEX_NITTER_INSTANCES:
        try:
            resp = requests.get(
                f"{base}/{SPACEX_TWITTER_HANDLE}/rss",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as e:
            # A mirror being down/blocked is routine, not a bug — log one
            # short line instead of a full traceback so this doesn't spam
            # the console every SPACEX_CHECK_INTERVAL_MINUTES.
            print(f"[spacex] {base} niedostępne ({type(e).__name__}: {e}), próbuję kolejny mirror...")
            continue

        tweets = []
        for item in root.findall("./channel/item"):
            link = (item.findtext("link") or "").strip()
            match = SPACEX_TWEET_LINK_RE.search(link)
            if not match:
                continue
            tweets.append({
                "id": match.group(1),
                "url": f"https://twitter.com/{SPACEX_TWITTER_HANDLE}/status/{match.group(1)}",
                "text": (item.findtext("title") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
            })
        if tweets:
            return tweets
    return []

def load_seen_tweet_ids() -> set[str] | None:
    """None means "no baseline recorded yet" — distinct from an empty set,
    which means we've checked before and genuinely saw nothing new."""
    try:
        with open(SPACEX_SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_seen_tweet_ids(ids: set[str]) -> None:
    with open(SPACEX_SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)

@tasks.loop(minutes=SPACEX_CHECK_INTERVAL_MINUTES)
async def watch_for_spacex_tweets():
    try:
        tweets = await asyncio.to_thread(fetch_spacex_tweets)
    except Exception:
        traceback.print_exc()
        return
    if not tweets:
        return  # all Nitter mirrors failed this cycle; leave state alone and retry later

    seen_ids = load_seen_tweet_ids()
    current_ids = {t["id"] for t in tweets}
    # On the very first check ever (seen_ids is None) just establish the
    # baseline — don't announce every tweet currently in the feed as "new".
    new_ids = set() if seen_ids is None else current_ids - seen_ids
    save_seen_tweet_ids(current_ids)
    if not new_ids:
        return

    channels = await _get_notify_channels()
    if not channels:
        return

    new_tweets = sorted((t for t in tweets if t["id"] in new_ids), key=lambda t: int(t["id"]))
    for tweet in new_tweets:
        for channel in channels:
            try:
                await channel.send(f"🚀 Nowy tweet SpaceX!\n{tweet['url']}")
            except Exception:
                traceback.print_exc()

@watch_for_spacex_tweets.before_loop
async def before_watch_for_spacex_tweets():
    await bot.wait_until_ready()

async def build_entry_embed(slug: str) -> tuple[list[discord.Embed], list[discord.File]]:
    try:
        notices = await asyncio.to_thread(fetch_entry_notices, slug)
    except Exception:
        traceback.print_exc()
        notices = []
    try:
        images = await asyncio.to_thread(_fetch_entry_images, slug)
    except Exception:
        traceback.print_exc()
        images = []

    embed = discord.Embed(
        title=_prettify_slug(slug),
        url=SPACE_NOTICES_ENTRY_LINK.format(slug=slug),
        color=discord.Color.blurple(),
    )
    embed.description = (
        _format_space_notice_lines(notices) if notices
        else "Brak notek dla tego wpisu."
    )

    embeds = [embed]
    files = []
    if images:
        embed.add_field(name="🗺️ Zdjęcia", value=f"{len(images)} zdjęcie(a)", inline=False)
        extra_embeds, files = _build_image_embeds(images, SPACE_NOTICES_ENTRY_LINK.format(slug=slug))
        embeds.extend(extra_embeds)
    else:
        embed.add_field(name="🗺️ Zdjęcia", value="Brak zdjęć dla tego wpisu.", inline=False)

    embed.set_footer(text="Źródło: space-notices.com")
    return embeds, files

class EntrySelect(discord.ui.Select):
    def __init__(self, entries: list[tuple[str, str]]):
        options = [
            discord.SelectOption(label=name[:100], value=slug)
            for slug, name in entries[:25]
        ]
        super().__init__(placeholder="Wybierz start / wpis...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            embeds, files = await build_entry_embed(self.values[0])
            await interaction.edit_original_response(embeds=embeds, attachments=files, view=self.view)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("Nie udało się pobrać danych dla tego wpisu.", ephemeral=True)

class SpaceNoticesView(discord.ui.View):
    def __init__(self, entries: list[tuple[str, str]]):
        super().__init__(timeout=300)
        self.add_item(EntrySelect(entries))

@bot.tree.command(
    name="spacenotices",
    description="Wybierz start i zobacz jego notki oraz zdjęcia ze space-notices.com"
)
async def space_notices_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        entries = await asyncio.to_thread(fetch_active_entries)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Nie udało się pobrać listy wpisów ze space-notices.com.")
        return
    if not entries:
        await interaction.followup.send("Brak aktywnych wpisów na space-notices.com.")
        return
    embed = discord.Embed(
        title="Space Notices",
        description="Wybierz start z listy, żeby zobaczyć jego notki i zdjęcia.",
        color=discord.Color.blurple(),
    )
    await interaction.followup.send(embed=embed, view=SpaceNoticesView(entries))
@bot.command(name="spacenotices")
async def space_notices_prefix(ctx: commands.Context):
    async with ctx.typing():
        try:
            entries = await asyncio.to_thread(fetch_active_entries)
        except Exception as e:
            traceback.print_exc()
            await ctx.send(f"wyjebka: `{type(e).__name__}: {e}`")
            return
    if not entries:
        await ctx.send("Brak aktywnych wpisów na space-notices.com.")
        return
    embed = discord.Embed(
        title="Space Notices",
        description="Wybierz start z listy, żeby zobaczyć jego notki i zdjęcia.",
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed, view=SpaceNoticesView(entries))

@bot.tree.command(
    name="tfr",
    description="Sprawdź TFR-y nad Starbase: dane z FAA, potwierdzenie i zdjęcia ze space-notices.com"
)
async def tfr_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        embeds, files = await build_tfr_embed()
        await interaction.followup.send(embeds=embeds, files=files or discord.utils.MISSING)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Nie udało się pobrać danych o TFR z tfr.faa.gov / space-notices.com.")
@bot.command(name="tfr")
async def tfr_prefix(ctx: commands.Context):
    async with ctx.typing():
        try:
            embeds, files = await build_tfr_embed()
            await ctx.send(embeds=embeds, files=files or discord.utils.MISSING)
        except Exception as e:
            traceback.print_exc()
            await ctx.send(f"wyjebka: `{type(e).__name__}: {e}`")
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not watch_for_new_tfrs.is_running():
        watch_for_new_tfrs.start()
    if not watch_for_road_closures.is_running():
        watch_for_road_closures.start()
    if not watch_for_spacex_tweets.is_running():
        watch_for_spacex_tweets.start()
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands synced. Try /test or .test in your server.")
    print(f"Watching for new Starbase TFRs every {TFR_CHECK_INTERVAL_MINUTES} min -> channels {NOTIFY_CHANNEL_IDS}")
    print(f"Watching for Starbase road closures every {ROAD_CHECK_INTERVAL_MINUTES} min -> channels {NOTIFY_CHANNEL_IDS}")
    print(f"Watching for new SpaceX tweets every {SPACEX_CHECK_INTERVAL_MINUTES} min -> channels {NOTIFY_CHANNEL_IDS}")

if __name__ == "__main__":
    bot.run(TOKEN)