import os
import io
import ipaddress
import subprocess
import sys
import csv, io, json, re, sqlite3, threading, time, webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, redirect, render_template, request

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("QUAD_RADIO_DATA", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "radio_history.sqlite3"
CONFIG_PATH = BASE / "config.json"
app = Flask(__name__)
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) QuadRadioArchive/2.0"})

# Album artwork lookup cache: avoids repeating iTunes searches on every refresh.
artwork_cache = {}
artwork_cache_lock = threading.Lock()
ARTWORK_CACHE_SECONDS = 6 * 60 * 60

# Artist portrait cache: stores successful and unsuccessful lookups.
artist_photo_cache = {}
artist_photo_cache_lock = threading.Lock()
ARTIST_PHOTO_CACHE_SECONDS = 24 * 60 * 60



def artwork_tokens(value):
    return set(re.findall(r"[a-z0-9]+", str(value or "").lower()))


@app.get("/api/artwork")
def get_artwork():
    title = request.args.get("title", "").strip()
    artist = request.args.get("artist", "").strip()

    if not title or title.upper() in {"PST", "PDT", "UTC", "EST", "EDT", "CST", "CDT"}:
        return jsonify({"artwork": None})

    key = (artist.casefold(), title.casefold())
    now = time.time()

    with artwork_cache_lock:
        cached = artwork_cache.get(key)
        if cached and cached[0] > now:
            return jsonify({"artwork": cached[1]})

    artwork = None
    try:
        response = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": f"{artist} {title}".strip(),
                "entity": "song",
                "limit": 10,
                "country": "US"
            },
            timeout=8
        )
        response.raise_for_status()
        results = response.json().get("results", [])

        title_words = artwork_tokens(title)
        artist_words = artwork_tokens(artist)
        best_score = -1

        for item in results:
            candidate_title = artwork_tokens(item.get("trackName", ""))
            candidate_artist = artwork_tokens(item.get("artistName", ""))

            if not title_words or not candidate_title:
                continue

            title_score = len(title_words & candidate_title) / len(title_words)
            artist_score = (
                len(artist_words & candidate_artist) / len(artist_words)
                if artist_words else 0
            )

            if title_score < 0.60:
                continue
            if artist_words and artist_score < 0.25:
                continue

            score = title_score * 2 + artist_score
            if score > best_score:
                best_score = score
                artwork = item.get("artworkUrl100")

        if artwork:
            artwork = artwork.replace("100x100bb", "600x600bb")

    except (requests.RequestException, ValueError, TypeError):
        artwork = None

    with artwork_cache_lock:
        artwork_cache[key] = (now + ARTWORK_CACHE_SECONDS, artwork)

    return jsonify({"artwork": artwork})

collector_lock = threading.Lock()

def load_config():
    return json.loads(CONFIG_PATH.read_text())

def connect():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL,
            station_name TEXT NOT NULL,
            title TEXT NOT NULL,
            artist TEXT NOT NULL DEFAULT '',
            played_at TEXT NOT NULL,
            time_accuracy TEXT NOT NULL DEFAULT 'source',
            source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            UNIQUE(station_id,title,artist,played_at)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS source_status (
            station_id TEXT PRIMARY KEY, checked_at TEXT, status TEXT,
            message TEXT, imported_count INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS shared_notes (
            id INTEGER PRIMARY KEY CHECK(id=1),
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )""")
        c.execute(
            "INSERT OR IGNORE INTO shared_notes (id, content, updated_at) VALUES (1, '', ?)",
            (datetime.now().astimezone().isoformat(),)
        )
        c.execute("""CREATE TABLE IF NOT EXISTS notes_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'Untitled document',
            content TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS song_playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS playlist_songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            artist TEXT NOT NULL DEFAULT '',
            station_id TEXT NOT NULL DEFAULT '',
            station_name TEXT NOT NULL DEFAULT '',
            played_at TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL,
            UNIQUE(playlist_id, title, artist),
            FOREIGN KEY(playlist_id) REFERENCES song_playlists(id)
        )""")

        # Preserve the original shared note as the first document.
        if c.execute("SELECT COUNT(*) FROM notes_documents").fetchone()[0] == 0:
            old_note = c.execute(
                "SELECT content, updated_at FROM shared_notes WHERE id=1"
            ).fetchone()
            now = datetime.now().astimezone().isoformat()
            c.execute(
                """INSERT INTO notes_documents
                (title, content, created_at, updated_at)
                VALUES (?, ?, ?, ?)""",
                (
                    "My Notes",
                    old_note["content"] if old_note else "",
                    now,
                    old_note["updated_at"] if old_note else now
                )
            )
        # Upgrade databases made by the first prototype.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(plays)")}
        if "time_accuracy" not in cols:
            c.execute("ALTER TABLE plays ADD COLUMN time_accuracy TEXT NOT NULL DEFAULT 'source'")

def local_now(station):
    return datetime.now(ZoneInfo(station.get("timezone", "UTC")))


def parse_mix951(soup, station):
    """Read Mix 95.1's time-first recently-played listing."""
    lines = [
        re.sub(r"\s+", " ", x).strip()
        for x in soup.get_text("\n").splitlines()
        if x.strip()
    ]

    now = local_now(station)
    page_day = now.date()

    month_re = re.compile(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?",
        re.I
    )

    for line in lines[:120]:
        m = month_re.search(line)
        if m:
            try:
                page_day = datetime.strptime(
                    f"{m.group(1)} {m.group(2)} {m.group(3) or now.year}",
                    "%B %d %Y"
                ).date()
                if page_day > now.date():
                    page_day = page_day.replace(year=page_day.year - 1)
            except ValueError:
                pass
            break

    time_re = re.compile(
        r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", re.I
    )

    noise = re.compile(
        r"wikz\s*-|mix\s*95|recently played|"
        r"listen live|advertisement|chambersburg",
        re.I
    )

    output = []

    for i, line in enumerate(lines):
        match = time_re.match(line)
        if not match:
            continue

        hour = int(match.group(1))
        minute = int(match.group(2))

        if not 1 <= hour <= 12 or minute > 59:
            continue

        hour = hour % 12 + (
            12 if match.group(3).upper() == "PM" else 0
        )

        candidates = []
        for following in lines[i + 1:i + 4]:
            if time_re.match(following):
                break
            if noise.search(following):
                continue
            if len(following) < 2 or len(following) > 160:
                continue
            candidates.append(following)

        if not candidates:
            continue

        title = candidates[0]
        artist = ""

        if " - " in title:
            artist, title = title.split(" - ", 1)
        elif len(candidates) > 1:
            artist = candidates[1]

        title = title.strip()
        artist = artist.strip()

        if not title or title.upper() in {"PST", "PDT", "UTC"}:
            continue

        played = datetime(
            page_day.year,
            page_day.month,
            page_day.day,
            hour,
            minute,
            tzinfo=ZoneInfo(station["timezone"])
        )

        output.append((
            title,
            artist,
            played.isoformat(),
            "source"
        ))

    return dedupe(output)



def parse_live955(soup, station):
    """Parse Live 95.5's official dated recently-played history."""
    from zoneinfo import ZoneInfo

    output = []
    page_text = soup.get_text(" ", strip=True)

    date_match = re.search(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
        page_text,
        re.I
    )
    if not date_match:
        return output

    month, day, year = date_match.groups()
    date_value = datetime.strptime(
        f"{month} {day} {year}", "%B %d %Y"
    ).date()

    timezone = ZoneInfo(station.get("timezone", "America/Los_Angeles"))
    time_pattern = re.compile(r"^\d{1,2}:\d{2}\s*(?:am|pm)$", re.I)

    seen = set()

    for time_node in soup.find_all(string=time_pattern):
        time_text = str(time_node).strip()
        time_tag = time_node.parent
        title = artist = ""

        # Prefer explicitly named title and artist fields.
        ancestor = time_tag
        for _ in range(7):
            if ancestor is None:
                break

            title_node = ancestor.select_one(
                '[class*="song-title"], [class*="track-title"], '
                '[class*="title"]'
            )
            artist_node = ancestor.select_one(
                '[class*="artist"], [class*="performer"]'
            )

            if title_node and artist_node:
                title = title_node.get_text(" ", strip=True)
                artist = artist_node.get_text(" ", strip=True)
                break

            ancestor = ancestor.parent

        # Fallback: identify separate title/artist text elements
        # in the nearest track container.
        if not title or not artist:
            ancestor = time_tag
            for _ in range(7):
                if ancestor is None:
                    break

                pieces = []
                for node in ancestor.find_all(True):
                    if node.find(True):
                        continue
                    value = node.get_text(" ", strip=True)
                    if (
                        value
                        and value != time_text
                        and not time_pattern.match(value)
                        and value not in pieces
                    ):
                        pieces.append(value)

                if len(pieces) == 2:
                    title, artist = pieces
                    break

                ancestor = ancestor.parent

        title = re.sub(r"\s+", " ", title).strip()
        artist = re.sub(r"\s+", " ", artist).strip()

        if not title or not artist:
            continue

        try:
            played_time = datetime.strptime(
                time_text.replace(" ", "").lower(),
                "%I:%M%p"
            ).time()

            played = datetime.combine(
                date_value, played_time, tzinfo=timezone
            )
        except ValueError:
            continue

        key = (title.casefold(), artist.casefold(), played.isoformat())
        if key in seen:
            continue
        seen.add(key)

        output.append((
            title,
            artist,
            played.isoformat(),
            "source"
        ))

    return output


def parse_netplayer(soup, station):
    """Parse Netplayer song rows using their actual UTC play timestamps."""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    output = []

    for row in soup.select("div.my-3.row"):
        song_block = row.select_one(".d-flex.align-items-center")
        if not song_block:
            continue

        song_text = song_block.get_text(" ", strip=True)

        # Remove the track's release date, which is not its play date.
        song_text = re.sub(r"\s+\d{4}-\d{2}-\d{2}\s*$", "", song_text)

        if " - " not in song_text:
            continue

        artist, title = song_text.split(" - ", 1)
        artist = artist.strip()
        title = title.strip()

        # Normalize unusual spaces and encoding artifacts in timestamps.
        row_text = row.get_text(" ", strip=True)
        row_text = row_text.replace("Â", " ").replace("\xa0", " ")
        row_text = re.sub(r"\s+", " ", row_text)

        match = re.search(
            r"\b([A-Z][a-z]{2})\s+(\d{1,2}),\s*"
            r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*UTC\b",
            row_text,
            re.I
        )

        if not match:
            continue

        month, day, hour, minute, meridiem = match.groups()

        try:
            year = datetime.now(timezone.utc).year
            played_utc = datetime.strptime(
                f"{month} {day} {year} {hour}:{minute} {meridiem}",
                "%b %d %Y %I:%M %p"
            ).replace(tzinfo=timezone.utc)

            # Account for a playlist crossing New Year's Day.
            if played_utc > datetime.now(timezone.utc) + timedelta(days=1):
                played_utc = played_utc.replace(year=year - 1)

            played_local = played_utc.astimezone(
                ZoneInfo(station.get("timezone", "UTC"))
            )

            output.append((
                title,
                artist,
                played_local.isoformat(),
                "source"
            ))

        except (ValueError, OverflowError):
            continue

    return dedupe(output)

def parse_fulldial(soup, station):
    """Parse Live 95.5's title / artist / play-time playlist layout."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in soup.get_text("\n").splitlines()
        if line.strip()
    ]

    now = local_now(station)
    current_day = now.date()
    output = []
    pending = []

    date_re = re.compile(
        r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"([A-Z][a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4})$",
        re.I
    )
    hour_re = re.compile(r"^\d{1,2}\s*(?:am|pm)$", re.I)
    time_re = re.compile(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", re.I)

    for line in lines:
        dm = date_re.match(line)
        if dm:
            try:
                current_day = datetime.strptime(
                    f"{dm.group(1)} {dm.group(2)} {dm.group(3)}",
                    "%B %d %Y"
                ).date()
            except ValueError:
                pass
            pending.clear()
            continue

        if hour_re.match(line):
            pending.clear()
            continue

        tm = time_re.match(line)
        if tm:
            if len(pending) >= 2:
                title, artist = pending[-2], pending[-1]

                hour = int(tm.group(1)) % 12
                if tm.group(3).lower() == "pm":
                    hour += 12

                minute = int(tm.group(2))
                if minute <= 59:
                    played = datetime(
                        current_day.year,
                        current_day.month,
                        current_day.day,
                        hour,
                        minute,
                        tzinfo=ZoneInfo(station["timezone"])
                    )
                    output.append((
                        title,
                        artist,
                        played.isoformat(),
                        "source"
                    ))

            pending.clear()
            continue

        pending.append(line)

    return dedupe(output)


def parse_kkez_fulldial(soup, station):
    """Parse KKEZ FullDial's time -> title -> artist playlist layout."""
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in soup.get_text("\n").splitlines()
        if line.strip()
    ]

    marker = "Recently played on Mix 94.5"
    try:
        start = lines.index(marker)
    except ValueError:
        raise ValueError("KKEZ playlist heading was not found.")

    lines = lines[start + 1:]
    now = local_now(station)
    current_day = now.date()
    output = []

    date_re = re.compile(
        r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+"
        r"([A-Z][a-z]{2})\s+(\d{1,2})$",
        re.I
    )
    time_re = re.compile(
        r"^(\d{1,2}):(\d{2})\s*(AM|PM)$",
        re.I
    )

    i = 0
    while i < len(lines):
        line = lines[i]

        dm = date_re.match(line)
        if dm:
            try:
                parsed = datetime.strptime(
                    f"{dm.group(1)} {dm.group(2)} {now.year}",
                    "%b %d %Y"
                ).date()

                if parsed > now.date() + timedelta(days=1):
                    parsed = parsed.replace(year=parsed.year - 1)

                current_day = parsed
            except ValueError:
                pass

            i += 1
            continue

        tm = time_re.match(line)
        if tm and i + 2 < len(lines):
            title = lines[i + 1].strip()
            artist = lines[i + 2].strip()

            # Do not accept another time/date/header as a song field.
            if (
                title
                and artist
                and not time_re.match(title)
                and not date_re.match(title)
                and not time_re.match(artist)
                and not date_re.match(artist)
            ):
                hour = int(tm.group(1)) % 12
                if tm.group(3).upper() == "PM":
                    hour += 12

                minute = int(tm.group(2))
                if minute <= 59:
                    played = datetime(
                        current_day.year,
                        current_day.month,
                        current_day.day,
                        hour,
                        minute,
                        tzinfo=ZoneInfo(station["timezone"])
                    )
                    output.append((
                        title,
                        artist,
                        played.isoformat(),
                        "source"
                    ))

                i += 3
                continue

        i += 1

    return dedupe(output)

def dedupe(rows):
    seen, result = set(), []
    for row in rows:
        key = (row[0].casefold(), row[1].casefold(), row[2])
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result

def save_status(sid, checked, status, message, count=0):
    with connect() as c:
        c.execute("""INSERT INTO source_status VALUES(?,?,?,?,?)
        ON CONFLICT(station_id) DO UPDATE SET checked_at=excluded.checked_at,
        status=excluded.status,message=excluded.message,imported_count=excluded.imported_count""",
        (sid, checked, status, message, count))



def fetch_mix951(station):
    """Fetch Mix 95.1 history with bounded network waits."""
    api_url = (
        "https://us.production.audio.one/api/v1.3/app/20070/"
        "playlist/168/live-stream-history/"
    )

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.mix95.com/",
        "Origin": "https://www.mix95.com",
    }

    timezone = ZoneInfo(station.get("timezone", "America/New_York"))
    page_url = api_url
    rows = []
    pages = 0

    while page_url and pages < 5:
        response = HTTP.get(
            page_url,
            headers=headers,
            params={"page_size": 100} if pages == 0 else None,
            timeout=(5, 12),
        )
        response.raise_for_status()
        payload = response.json()

        for item in payload.get("results", []):
            title = str(item.get("current_title") or "").strip()
            artist = str(item.get("current_artist_name") or "").strip()
            streamed_time = item.get("streamed_time")

            if not title or not streamed_time:
                continue

            try:
                played = datetime.fromisoformat(
                    streamed_time.replace("Z", "+00:00")
                )
                if played.tzinfo is None:
                    continue
                played = played.astimezone(timezone)
            except (ValueError, TypeError):
                continue

            rows.append((title, artist, played.isoformat(), "source"))

        page_url = payload.get("next")
        pages += 1

    return dedupe(rows), api_url


def fetch_html_bounded(
    url,
    total_timeout=25,
    stop_after_songs=5,
    station=None
):
    """Fetch Netplayer efficiently, stopping once enough recent songs arrive."""
    started = time.monotonic()
    response = HTTP.get(
        url,
        timeout=(5, 3),
        stream=True
    )
    body = bytearray()

    try:
        response.raise_for_status()

        try:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    body.extend(chunk)

                elapsed = time.monotonic() - started
                if elapsed >= total_timeout:
                    break

                if station and stop_after_songs:
                    html = body.decode(
                        response.encoding or "utf-8",
                        errors="replace"
                    )
                    rows = parse_netplayer(
                        BeautifulSoup(html, "html.parser"),
                        station
                    )
                    if len(rows) >= stop_after_songs:
                        break

        except requests.RequestException:
            # Preserve usable HTML if Netplayer stops sending data.
            if not body:
                raise

        return body.decode(
            response.encoding or "utf-8",
            errors="replace"
        )
    finally:
        response.close()


def fetch_kkez_spincounts(station):
    """Fetch KKEZ's timestamped recently-played records from SpinCounts."""
    url = station.get("url") or "https://spincounts.com/station/mix-94-5-2/"
    response = HTTP.get(url, timeout=(5, 20))
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    timezone = ZoneInfo(station.get("timezone", "America/Chicago"))
    rows = []

    for when in soup.select("td.sc-list__when"):
        tr = when.find_parent("tr")
        if not tr:
            continue

        cells = tr.find_all("td")
        song_cells = [cell for cell in cells if cell is not when]

        if len(song_cells) < 2:
            continue

        title = song_cells[0].get_text(" ", strip=True)
        artist = song_cells[1].get_text(" ", strip=True)
        timestamp = when.get_text(" ", strip=True)

        if not title or not artist:
            continue

        played = datetime.strptime(
            timestamp, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone)

        rows.append((
            title,
            artist,
            played.isoformat(),
            "source"
        ))

    rows = dedupe(rows)

    if not rows:
        raise ValueError("SpinCounts returned no timestamped plays.")

    return rows, url


def collect_station(station):
    checked = datetime.now().astimezone().isoformat()
    url = station.get("url", "").strip()
    sid = station["id"]

    if sid == "live955":
        url = "https://www.live955.com/recently-played/"

    # Make progress visible immediately, before contacting the source.
    save_status(sid, checked, "checking", "Checking station source...")

    if not url or station.get("source_type") == "unconfigured":
        save_status(
            sid, checked, "needs_source",
            station.get("note", "No playlist source configured.")
        )
        return

    try:
        kind = "live955" if sid == "live955" else station.get("source_type")

        if kind == "mix951":
            rows, source_url = fetch_mix951(station)
        elif kind in ("kkez_spincounts", "spincounts"):
            rows, source_url = fetch_kkez_spincounts(station)
        else:
            if sid == "live993":
                html = fetch_html_bounded(url, total_timeout=25, stop_after_songs=5, station=station)
            elif kind == "kkez_fulldial":
                response = HTTP.get(url, timeout=(3, 8))
                response.raise_for_status()
                html = response.text
            else:
                response = HTTP.get(url, timeout=(5, 15))
                response.raise_for_status()
                html = response.text

            soup = BeautifulSoup(html, "html.parser")

            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()

            if kind == "live955":
                rows = parse_live955(soup, station)
            elif kind == "netplayer":
                rows = parse_netplayer(soup, station)
            elif kind == "fulldial":
                rows = parse_fulldial(soup, station)
            elif kind == "kkez_fulldial":
                rows = parse_kkez_fulldial(soup, station)
            else:
                rows = []

            source_url = url

        if not rows:
            save_status(
                sid, checked, "no_records",
                "The source returned no recognizable song records."
            )
            return

        inserted = 0
        with connect() as db:
            for title, artist, played_at, accuracy in rows:
                cur = db.execute(
                    """INSERT OR IGNORE INTO plays
                    (station_id,station_name,title,artist,played_at,
                     time_accuracy,source_url,collected_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        sid, station["name"], title, artist,
                        played_at, accuracy, source_url, checked
                    )
                )
                inserted += cur.rowcount

        save_status(
            sid, checked, "ok",
            f"Source checked successfully. {inserted} new records saved "
            f"({len(rows)} records found).",
            inserted
        )

    except Exception as error:
        save_status(
            sid, checked, "error",
            f"{type(error).__name__}: {str(error)[:240]}"
        )


def collect_stations_locked():
    """Run one collection while the caller owns collector_lock."""
    refresh_state.update({
        "running": True,
        "started_at": datetime.now().astimezone().isoformat(),
        "finished_at": None,
        "error": None
    })

    try:
        for station in load_config()["stations"]:
            collect_station(station)

    except Exception as error:
        refresh_state["error"] = str(error)

    finally:
        refresh_state["finished_at"] = datetime.now().astimezone().isoformat()
        refresh_state["running"] = False
        collector_lock.release()


def collect_all():
    if not collector_lock.acquire(blocking=False):
        return False

    collect_stations_locked()
    return True


def poller():
    while True:
        try:
            collect_all()
        except Exception as error:
            print("Quad Radio poller error:", repr(error), flush=True)

        time.sleep(max(60, int(load_config().get("poll_seconds", 60))))


# Fixed, verified radio stream endpoints. Never accept arbitrary stream URLs.
RADIO_STREAMS = {
    "live993": "https://live.amperwave.net/direct/alphacorporate-wvbxfmaac-ibc4",
    "mix951": "https://live.amperwave.net/direct/alphacorporate-wikzfmaac-ibc4",
    "live955": "https://live.amperwave.net/direct/alphacorporate-kbfffmaac-ibc4",
    "kelly953": "https://ice7.securenetsystems.net/KLLY",
    "alt997": "https://live.amperwave.net/direct/alphacorporate-warqhd2aac-ibc4",
    "now997": "https://playerservices.streamtheworld.com/api/livestream-redirect/KMVQFMAAC.aac",
    "movin925": "https://playerservices.streamtheworld.com/api/livestream-redirect/KQMVFMAAC.aac",
    "djx997": "https://live.amperwave.net/direct/alphacorporate-wdjxfmaac-ibc2",
    "mix941": "https://live.amperwave.net/direct/alphacorporate-whbcfmaac-ibc4",
    "z100pdx": "https://stream.revma.ihrhls.com/zc1961",
    "z100nyc": "https://stream.revma.ihrhls.com/zc1469",
    "live967": "https://live.amperwave.net/direct/alphacorporate-wdldfmaac-ibc4",
    "mix1065": "https://live.amperwave.net/direct/alphacorporate-kezrfmmp3-ibc4",
    "kpsi1005": "https://live.amperwave.net/direct/alphacorporate-kpsifmaac-ibc4",
    "kfrx1063": "https://live.amperwave.net/direct/alphacorporate-kfrxfmaac-ibc4",
}
KKEZ_PLAYLIST = "http://stream.abacast.net/playlist/alphamidwest-kkezfmaac-ibc2.m3u"


@app.get("/stream/<station_id>")
def radio_stream(station_id):
    if station_id in RADIO_STREAMS:
        return redirect(RADIO_STREAMS[station_id], code=302)

    if station_id == "kkez945":
        try:
            response = requests.get(
                KKEZ_PLAYLIST,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )
            response.raise_for_status()

            for line in response.text.splitlines():
                candidate = line.strip()
                if candidate.startswith(("http://", "https://")):
                    return redirect(candidate, code=302)

            return jsonify({"error": "The KKEZ playlist contained no playable stream."}), 502
        except requests.RequestException as error:
            app.logger.warning("KKEZ stream resolution failed: %s", error)
            return jsonify({"error": "Could not resolve the KKEZ stream."}), 502

    return jsonify({"error": "Unknown radio station."}), 404


@app.get("/player")
def radio_player_page():
    return render_template("radio_player.html", stations=load_config()["stations"])


@app.get("/")
def home():
    return render_template("index.html")

@app.get("/notes")
def notes_page():
    return render_template("notes.html")



@app.get("/my-songs")
def my_songs_page():
    return render_template("my_songs.html")


# Statistics and discovery use the complete saved history, not the
# dashboard's 3,000-row display limit.
VALID_PLAY_FILTER = """
    time_accuracy != 'unverified'
    AND TRIM(title) != ''
    AND UPPER(TRIM(title)) NOT IN
        ('PST','PDT','UTC','EST','EDT','CST','CDT')
    AND LOWER(TRIM(title)) NOT LIKE 'time (%'
"""


@app.get("/api/artist-photo")
def get_artist_photo():
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"photo": None})

    key = artist.casefold()
    now = time.time()

    with artist_photo_cache_lock:
        cached = artist_photo_cache.get(key)
        if cached and cached[0] > now:
            return jsonify({"photo": cached[1]})

    photo = None
    try:
        response = requests.get(
            "https://api.deezer.com/search/artist",
            params={"q": artist, "limit": 10},
            timeout=8
        )
        response.raise_for_status()
        results = response.json().get("data", [])

        exact = next(
            (item for item in results
             if item.get("name", "").strip().casefold() == key),
            None
        )

        if exact:
            photo = (
                exact.get("picture_xl")
                or exact.get("picture_big")
                or exact.get("picture_medium")
            )

    except Exception as exc:
        print(f"Artist photo lookup failed for {artist}: {exc}")

    with artist_photo_cache_lock:
        artist_photo_cache[key] = (
            now + ARTIST_PHOTO_CACHE_SECONDS,
            photo
        )

    return jsonify({"photo": photo})



@app.get("/station/<station_id>")
def station_page(station_id):
    cfg = load_config()
    station = next(
        (s for s in cfg["stations"] if s["id"] == station_id),
        None
    )
    if station is None:
        return "Radio station not found.", 404

    with connect() as db:
        stats = db.execute(f"""
            SELECT
                COUNT(*) AS total_plays,
                COUNT(DISTINCT LOWER(TRIM(title))) AS unique_songs,
                COUNT(DISTINCT LOWER(TRIM(artist))) AS unique_artists,
                MIN(played_at) AS first_seen,
                MAX(played_at) AS last_seen
            FROM plays
            WHERE {VALID_PLAY_FILTER} AND station_id = ?
        """, (station_id,)).fetchone()

        top_songs = [dict(r) for r in db.execute(f"""
            SELECT
                MIN(id) AS id,
                MIN(TRIM(title)) AS title,
                MIN(TRIM(artist)) AS artist,
                COUNT(*) AS detections,
                MIN(played_at) AS first_seen,
                MAX(played_at) AS last_seen
            FROM plays
            WHERE {VALID_PLAY_FILTER} AND station_id = ?
            GROUP BY LOWER(TRIM(title)), LOWER(TRIM(artist))
            ORDER BY detections DESC, title COLLATE NOCASE
            LIMIT 15
        """, (station_id,))]

        top_artists = [dict(r) for r in db.execute(f"""
            SELECT
                MIN(TRIM(artist)) AS artist,
                COUNT(*) AS detections,
                COUNT(DISTINCT LOWER(TRIM(title))) AS songs
            FROM plays
            WHERE {VALID_PLAY_FILTER}
              AND station_id = ?
              AND TRIM(artist) != ''
            GROUP BY LOWER(TRIM(artist))
            ORDER BY detections DESC, artist COLLATE NOCASE
            LIMIT 10
        """, (station_id,))]

        recent = [dict(r) for r in db.execute(f"""
            SELECT title, artist, played_at, station_name
            FROM plays
            WHERE {VALID_PLAY_FILTER} AND station_id = ?
            ORDER BY played_at DESC, id DESC
            LIMIT 100
        """, (station_id,))]

    return render_template(
        "station.html",
        station=station,
        stats=dict(stats),
        top_songs=top_songs,
        top_artists=top_artists,
        recent=recent
    )


@app.get("/statistics")
def statistics_page():
    with connect() as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM plays WHERE {VALID_PLAY_FILTER}"
        ).fetchone()[0]

        unique_songs = db.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT LOWER(TRIM(title)), LOWER(TRIM(artist))
                FROM plays
                WHERE {VALID_PLAY_FILTER}
                GROUP BY LOWER(TRIM(title)), LOWER(TRIM(artist))
            )
        """).fetchone()[0]

        songs = [dict(r) for r in db.execute(f"""
            SELECT
                MIN(id) AS id,

                MIN(TRIM(title)) AS title,
                MIN(TRIM(artist)) AS artist,
                COUNT(*) AS plays,
                COUNT(DISTINCT station_id) AS station_count,
                MIN(played_at) AS first_played,
                MAX(played_at) AS last_played
            FROM plays
            WHERE {VALID_PLAY_FILTER}
            GROUP BY LOWER(TRIM(title)), LOWER(TRIM(artist))
            ORDER BY plays DESC, title COLLATE NOCASE
            LIMIT 30
        """)]

        artists = [dict(r) for r in db.execute(f"""
            SELECT
                MIN(TRIM(artist)) AS artist,
                COUNT(*) AS plays,
                COUNT(DISTINCT LOWER(TRIM(title))) AS songs,
                COUNT(DISTINCT station_id) AS stations
            FROM plays
            WHERE {VALID_PLAY_FILTER}
              AND TRIM(artist) != ''
            GROUP BY LOWER(TRIM(artist))
            ORDER BY plays DESC, artist COLLATE NOCASE
            LIMIT 20
        """)]

        stations = [dict(r) for r in db.execute(f"""
            SELECT station_name, COUNT(*) AS plays,
                   COUNT(DISTINCT LOWER(TRIM(title))) AS songs
            FROM plays
            WHERE {VALID_PLAY_FILTER}
            GROUP BY station_id, station_name
            ORDER BY plays DESC
        """)]

    return render_template(
        "statistics.html",
        total=total,
        unique_songs=unique_songs,
        songs=songs,
        artists=artists,
        stations=stations
    )




@app.get("/song/<int:play_id>")
def song_profile(play_id):
    with connect() as db:
        seed = db.execute(f"""
            SELECT TRIM(title) AS title,
                   TRIM(COALESCE(artist, '')) AS artist
            FROM plays
            WHERE id = ? AND {VALID_PLAY_FILTER}
        """, (play_id,)).fetchone()

        if not seed or not seed["title"]:
            return "Song not found in verified radio history.", 404

        title_key = seed["title"].strip().casefold()
        artist_key = seed["artist"].strip().casefold()

        matching = f"""
            {VALID_PLAY_FILTER}
            AND LOWER(TRIM(title)) = ?
            AND LOWER(TRIM(COALESCE(artist, ''))) = ?
        """
        params = (title_key, artist_key)

        stats_row = db.execute(f"""
            SELECT
                MIN(TRIM(title)) AS title,
                MIN(TRIM(COALESCE(artist, ''))) AS artist,
                COUNT(*) AS total_detections,
                COUNT(DISTINCT station_id) AS station_count,
                MIN(played_at) AS first_seen,
                MAX(played_at) AS last_seen
            FROM plays
            WHERE {matching}
        """, params).fetchone()
        stats = dict(stats_row)

        stations = [dict(r) for r in db.execute(f"""
            SELECT
                station_id,
                station_name,
                COUNT(*) AS detections,
                MIN(played_at) AS first_seen,
                MAX(played_at) AS last_seen
            FROM plays
            WHERE {matching}
            GROUP BY station_id, station_name
            ORDER BY detections DESC, station_name COLLATE NOCASE
        """, params)]

        timeline = [dict(r) for r in db.execute(f"""
            SELECT
                SUBSTR(played_at, 1, 10) AS day,
                COUNT(*) AS detections
            FROM plays
            WHERE {matching}
            GROUP BY SUBSTR(played_at, 1, 10)
            ORDER BY day
        """, params)]

        history = [dict(r) for r in db.execute(f"""
            SELECT
                id, station_id, station_name, played_at,
                collected_at, time_accuracy, source_url
            FROM plays
            WHERE {matching}
            ORDER BY played_at DESC, id DESC
        """, params)]

    return render_template(
        "song_profile.html",
        song=stats,
        stations=stations,
        timeline=timeline,
        history=history
    )


@app.get("/artist/<path:artist_name>")
def artist_profile(artist_name):
    with connect() as db:
        profile = db.execute(f"""
            SELECT
                MIN(TRIM(artist)) AS artist,
                COUNT(*) AS total_detections,
                COUNT(DISTINCT LOWER(TRIM(title))) AS unique_songs,
                COUNT(DISTINCT station_id) AS station_count
            FROM plays
            WHERE {VALID_PLAY_FILTER}
              AND TRIM(artist) != ''
              AND LOWER(TRIM(artist)) = LOWER(TRIM(?))
        """, (artist_name,)).fetchone()

        if not profile or not profile["artist"]:
            return "Artist not found in verified radio history.", 404

        artist = dict(profile)

        songs = [dict(r) for r in db.execute(f"""
            SELECT
                MIN(TRIM(title)) AS title,
                COUNT(*) AS detections,
                MIN(played_at) AS first_seen,
                MAX(played_at) AS last_seen,
                COUNT(DISTINCT station_id) AS station_count,
                GROUP_CONCAT(DISTINCT station_name) AS stations
            FROM plays
            WHERE {VALID_PLAY_FILTER}
              AND LOWER(TRIM(artist)) = LOWER(TRIM(?))
            GROUP BY LOWER(TRIM(title))
            ORDER BY detections DESC, title COLLATE NOCASE
        """, (artist["artist"],))]

        stations = [dict(r) for r in db.execute(f"""
            SELECT
                station_name,
                COUNT(*) AS detections,
                COUNT(DISTINCT LOWER(TRIM(title))) AS songs
            FROM plays
            WHERE {VALID_PLAY_FILTER}
              AND LOWER(TRIM(artist)) = LOWER(TRIM(?))
            GROUP BY station_id, station_name
            ORDER BY detections DESC
        """, (artist["artist"],))]

    return render_template(
        "artist_profile.html",
        artist=artist,
        songs=songs,
        stations=stations
    )


@app.get("/discoveries")
def discoveries_page():
    with connect() as db:
        discoveries = [dict(r) for r in db.execute(f"""
            WITH grouped AS (
                SELECT
                    LOWER(TRIM(title)) AS title_key,
                    LOWER(TRIM(artist)) AS artist_key,
                    MIN(TRIM(title)) AS title,
                    MIN(TRIM(artist)) AS artist,
                    MIN(played_at) AS first_seen,
                    COUNT(*) AS total_detections
                FROM plays
                WHERE {VALID_PLAY_FILTER}
                GROUP BY LOWER(TRIM(title)), LOWER(TRIM(artist))
            )
            SELECT
                g.title,
                g.artist,
                g.first_seen,
                (
                    SELECT p.station_name
                    FROM plays p
                    WHERE LOWER(TRIM(p.title)) = g.title_key
                      AND LOWER(TRIM(p.artist)) = g.artist_key
                      AND p.played_at = g.first_seen
                      AND p.time_accuracy != 'unverified'
                    ORDER BY p.id
                    LIMIT 1
                ) AS station_name,
                g.total_detections
            FROM grouped g
            ORDER BY g.first_seen DESC
            LIMIT 200
        """)]
    return render_template("discoveries.html", discoveries=discoveries)


@app.get("/api/playlists")
def get_playlists():
    with connect() as c:
        playlists = c.execute(
            "SELECT id, name, created_at FROM song_playlists ORDER BY name COLLATE NOCASE"
        ).fetchall()
        result = []
        for playlist in playlists:
            songs = c.execute(
                """SELECT id, title, artist, station_id, station_name, played_at, added_at
                   FROM playlist_songs WHERE playlist_id=?
                   ORDER BY id DESC""",
                (playlist["id"],)
            ).fetchall()
            result.append({
                "id": playlist["id"],
                "name": playlist["name"],
                "created_at": playlist["created_at"],
                "songs": [dict(song) for song in songs]
            })
    return jsonify({"playlists": result})


@app.post("/api/playlists")
def create_playlist():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Enter a playlist name."}), 400
    if len(name) > 100:
        return jsonify({"error": "Playlist names must be 100 characters or fewer."}), 400

    now = datetime.now().astimezone().isoformat()
    try:
        with connect() as c:
            cur = c.execute(
                "INSERT INTO song_playlists (name, created_at) VALUES (?, ?)",
                (name, now)
            )
            playlist_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({"error": "A playlist with that name already exists."}), 409
    return jsonify({"id": playlist_id, "name": name}), 201


@app.post("/api/playlists/<int:playlist_id>/songs")
def add_playlist_song(playlist_id):
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    artist = str(data.get("artist", "")).strip()
    if not title:
        return jsonify({"error": "A song title is required."}), 400
    if len(title) > 500 or len(artist) > 500:
        return jsonify({"error": "Song information is too long."}), 400

    now = datetime.now().astimezone().isoformat()
    with connect() as c:
        exists = c.execute(
            "SELECT id FROM song_playlists WHERE id=?", (playlist_id,)
        ).fetchone()
        if not exists:
            return jsonify({"error": "Playlist not found."}), 404
        try:
            c.execute(
                """INSERT INTO playlist_songs
                   (playlist_id, title, artist, station_id, station_name, played_at, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    playlist_id, title, artist,
                    str(data.get("station_id", ""))[:100],
                    str(data.get("station_name", ""))[:200],
                    str(data.get("played_at", ""))[:100],
                    now
                )
            )
        except sqlite3.IntegrityError:
            return jsonify({"error": "That song is already in this playlist."}), 409
    return jsonify({"success": True}), 201


@app.delete("/api/playlists/<int:playlist_id>/songs/<int:song_id>")
def remove_playlist_song(playlist_id, song_id):
    with connect() as c:
        cur = c.execute(
            "DELETE FROM playlist_songs WHERE id=? AND playlist_id=?",
            (song_id, playlist_id)
        )
    if cur.rowcount == 0:
        return jsonify({"error": "Song not found."}), 404
    return jsonify({"success": True})


@app.delete("/api/playlists/<int:playlist_id>")
def delete_playlist(playlist_id):
    with connect() as c:
        c.execute("DELETE FROM playlist_songs WHERE playlist_id=?", (playlist_id,))
        cur = c.execute("DELETE FROM song_playlists WHERE id=?", (playlist_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "Playlist not found."}), 404
    return jsonify({"success": True})


@app.get("/api/documents")
def list_documents():
    with connect() as c:
        rows = c.execute(
            "SELECT id, title, content, created_at, updated_at "
            "FROM notes_documents ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return jsonify({"documents": [dict(row) for row in rows]})


@app.post("/api/documents")
def create_document():
    payload = request.get_json(silent=True) or {}
    title = payload.get("title", "Untitled document")
    content = payload.get("content", "")

    if not isinstance(title, str) or not isinstance(content, str):
        return jsonify({"ok": False, "message": "Invalid document data."}), 400
    if len(title) > 200 or len(content) > 5_000_000:
        return jsonify({"ok": False, "message": "Document is too large."}), 413

    now = datetime.now().astimezone().isoformat()
    with connect() as c:
        cur = c.execute(
            """INSERT INTO notes_documents
            (title, content, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            (title.strip() or "Untitled document", content, now, now)
        )
        doc_id = cur.lastrowid

    return jsonify({"ok": True, "id": doc_id, "title": title, "content": content,
                    "created_at": now, "updated_at": now})


@app.put("/api/documents/<int:doc_id>")
def update_document(doc_id):
    payload = request.get_json(silent=True) or {}
    title = payload.get("title")
    content = payload.get("content")

    if not isinstance(title, str) or not isinstance(content, str):
        return jsonify({"ok": False, "message": "Invalid document data."}), 400
    if len(title) > 200 or len(content) > 5_000_000:
        return jsonify({"ok": False, "message": "Document is too large."}), 413

    updated = datetime.now().astimezone().isoformat()
    with connect() as c:
        cur = c.execute(
            """UPDATE notes_documents
            SET title=?, content=?, updated_at=? WHERE id=?""",
            (title.strip() or "Untitled document", content, updated, doc_id)
        )
        if cur.rowcount == 0:
            return jsonify({"ok": False, "message": "Document not found."}), 404

    return jsonify({"ok": True, "updated_at": updated})


@app.delete("/api/documents/<int:doc_id>")
def delete_document(doc_id):
    with connect() as c:
        cur = c.execute(
            "DELETE FROM notes_documents WHERE id=?", (doc_id,)
        )
        if cur.rowcount == 0:
            return jsonify({"ok": False, "message": "Document not found."}), 404

    return jsonify({"ok": True})



def get_wifi_network_name():
    """Best-effort SSID detection; ChromeOS containers may hide host Wi-Fi."""
    commands = [
        ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
        ["iwgetid", "-r"],
        ["networksetup", "-getairportnetwork", "en0"],
    ]
    for command in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0:
                continue
            output = result.stdout.strip()
            if command[0] == "nmcli":
                for line in output.splitlines():
                    if line.startswith("yes:"):
                        return line[4:].strip() or None
            elif command[0] == "iwgetid":
                if output:
                    return output
            elif "Current Wi-Fi Network:" in output:
                name = output.split("Current Wi-Fi Network:", 1)[1].strip()
                if name and name.lower() != "you are not associated":
                    return name
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


@app.get("/api/network-status")
def network_status():
    internet = False
    try:
        response = requests.get(
            "https://www.gstatic.com/generate_204",
            timeout=4,
            allow_redirects=False,
            stream=True,
        )
        internet = response.status_code == 204
        response.close()
    except requests.RequestException:
        internet = False

    return jsonify({
        "internet": internet,
        "ssid": get_wifi_network_name(),
        "checked_at": datetime.now().astimezone().isoformat(),
    })


@app.get("/api/data")
def data():
    cfg = load_config()
    with connect() as c:
        all_rows = [dict(r) for r in c.execute("SELECT * FROM plays ORDER BY CASE WHEN time_accuracy='unverified' THEN 1 ELSE 0 END, played_at DESC, id DESC")]
        statuses = {r["station_id"]: dict(r) for r in c.execute("SELECT * FROM source_status")}
    active_ids = {s["id"] for s in cfg["stations"]}
    all_rows = [
        r for r in all_rows
        if r["station_id"] in active_ids
    ]

    stations = []
    for s in cfg["stations"]:
        rows = [r for r in all_rows if r["station_id"] == s["id"]]
        stations.append({**s, "plays": rows[:10], "count": len(rows), "status": statuses.get(s["id"], {})})
    return jsonify({"stations": stations, "plays": all_rows})

refresh_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "error": None
}

@app.post("/api/refresh")
def refresh():
    if not collector_lock.acquire(blocking=False):
        return jsonify({
            "ok": False,
            "busy": True,
            "message": "A station refresh is already running."
        }), 409

    try:
        threading.Thread(
            target=collect_stations_locked,
            daemon=True
        ).start()
    except Exception:
        collector_lock.release()
        raise

    return jsonify({"ok": True, "message": "Refresh started."})


@app.get("/api/refresh-status")
def refresh_status():
    return jsonify(refresh_state)


@app.get("/api/export")
def export():
    with connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM plays ORDER BY "
            "CASE WHEN time_accuracy='unverified' THEN 1 ELSE 0 END, "
            "played_at DESC, id DESC"
        )]
    return jsonify(rows)


@app.get("/api/export.csv")
def export_csv():
    output = io.StringIO()
    with connect() as c:
        cursor = c.execute(
            "SELECT * FROM plays ORDER BY "
            "CASE WHEN time_accuracy='unverified' THEN 1 ELSE 0 END, "
            "played_at DESC, id DESC"
        )
        fieldnames = [column[0] for column in cursor.description]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(dict(row) for row in cursor)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=quad-radio-history.csv"}
    )

@app.post("/api/ask")
def ask():
    question = (request.json or {}).get("question", "").strip()
    if not question:
        return jsonify({"answer": "Enter a question."}), 400
    cfg = load_config()
    # Provide a substantial recent archive sample; Ollama is instructed not to invent missing records.
    with connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM plays ORDER BY CASE WHEN time_accuracy='unverified' THEN 1 ELSE 0 END, played_at DESC LIMIT 500")]
    if not rows:
        return jsonify({"answer": "The archive is empty. Run a refresh and confirm the station sources are returning records."})
    # Basic deterministic filtering before asking the model.
    q = question.lower()
    filtered = rows
    for s in cfg["stations"]:
        if s["name"].lower() in q or s["id"].lower() in q:
            filtered = [r for r in filtered if r["station_id"] == s["id"]]
            break
    if "yesterday" in q:
        filtered = [r for r in filtered if r.get("time_accuracy") != "unverified" and datetime.fromisoformat(r["played_at"]).date() == (datetime.now().date()-timedelta(days=1))]
    elif "today" in q:
        filtered = [r for r in filtered if r.get("time_accuracy") != "unverified" and datetime.fromisoformat(r["played_at"]).date() == datetime.now().date()]
    # If query includes a song/artist term, retain matching rows if possible.
    terms = [t for t in re.findall(r"[a-zA-Z0-9']{3,}", q)
             if t not in {"what","when","which","played","play","songs","song","station","yesterday","today","last","night","show","tell","me","were","was","the","on","at","for","did","have","from"}]
    if terms:
        matched = [r for r in filtered if any(t in (r["title"]+" "+r["artist"]+" "+r["station_name"]).lower() for t in terms)]
        if matched:
            filtered = matched
    context = "\n".join(f'{r["played_at"]} | {r["station_name"]} | {r["title"]} | {r["artist"]} | time={r["time_accuracy"]}' for r in filtered[:250])
    prompt = ("Use only the radio archive records below. Never invent songs or timestamps. "
              "If the requested period or station is not represented, say the archive has no matching records. "
              "Explain that approximate timestamps are marked approximate.\n"
              f"Question: {question}\nRecords:\n{context}")
    try:
        result = requests.post(cfg["ollama_url"].rstrip("/")+"/api/generate",
            json={"model":cfg["ollama_model"],"prompt":prompt,"stream":False},timeout=120)
        result.raise_for_status()
        return jsonify({"answer":result.json().get("response","").strip()})
    except Exception as e:
        return jsonify({"answer":f"Could not reach Ollama ({e}). Check `ollama list` and the model name in config.json."})



# ---- Secure local update controls ----

def _update_request_is_local():
    """Only allow update operations from the local Chromebook browser."""
    try:
        remote = ipaddress.ip_address(request.remote_addr or "")
        if not remote.is_loopback:
            return False

        from urllib.parse import urlsplit

        host = (urlsplit("//" + request.host).hostname or "").lower()
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return False

        origin = request.headers.get("Origin")
        if origin:
            origin_host = (urlsplit(origin).hostname or "").lower()
            if origin_host not in {"localhost", "127.0.0.1", "::1"}:
                return False

        return True
    except Exception:
        return False


@app.get("/api/update/check")
def update_check():
    if not _update_request_is_local():
        return jsonify({"error": "Local access only."}), 403

    try:
        from updater import latest_release

        latest, _, _ = latest_release()
        version_file = BASE / "VERSION"
        current = version_file.read_text().strip() if version_file.exists() else "0.0.0"

        def version_tuple(value):
            parts = tuple(int(part) for part in value.split("."))
            return parts + (0,) * (3 - len(parts))

        available = version_tuple(latest) > version_tuple(current)

        return jsonify({
            "current_version": current,
            "latest_version": latest,
            "update_available": available,
            "message": (
                f"Version {latest} is available."
                if available else
                f"Quad Radio is up to date ({current})."
            )
        })
    except Exception as error:
        return jsonify({
            "error": f"Could not check GitHub releases: {error}"
        }), 502


@app.post("/api/update/install")
def update_install():
    if not _update_request_is_local():
        return jsonify({"error": "Local access only."}), 403

    try:
        from updater import latest_release

        latest, _, _ = latest_release()
        version_file = BASE / "VERSION"
        current = version_file.read_text().strip() if version_file.exists() else "0.0.0"

        def version_tuple(value):
            parts = value.split(".")
            if len(parts) != 3 or not all(part.isdigit() for part in parts):
                raise ValueError("Invalid application version.")
            return tuple(int(part) for part in parts)

        if version_tuple(latest) <= version_tuple(current):
            return jsonify({
                "error": "This version is already installed or is older."
            }), 409

        log_path = DATA_DIR / "updater.log"
        log_file = open(log_path, "a", buffering=1)

        subprocess.Popen(
            [
                sys.executable,
                str(BASE / "updater.py"),
                "--install",
                "--app", str(BASE),
                "--data", str(DATA_DIR),
                "--pid", str(os.getpid()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

        return jsonify({
            "started": True,
            "version": latest,
            "message": "Update downloaded and validated; installation is starting."
        }), 202

    except Exception as error:
        return jsonify({"error": str(error)}), 500


if __name__ == "__main__":
    init_db()
    threading.Thread(target=poller, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5055")).start()
    app.run(host="0.0.0.0", port=5055, debug=False)
