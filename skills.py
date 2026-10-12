"""Jarvis's skills: tested things it can do, which the model only has to pick.

Each skill takes string arguments and returns a short plain-text result for
the model to put into words. None of them deletes or installs anything, so
they run straight away, without the confirmation that free-form commands need.
"""
import ctypes
import datetime
import difflib
import html
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from owner import enabled

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"  # see config.example.json
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"}

finished: "queue.Queue[dict]" = queue.Queue()  # things to say aloud when Jarvis is next free

# name -> what Windows should start; URIs and bare program names both work with os.startfile
APPS = {
    "calculator": "calc.exe", "notepad": "notepad.exe", "file explorer": "explorer.exe", "paint": "mspaint.exe",
    "settings": "ms-settings:", "bluetooth settings": "ms-settings:bluetooth", "wifi settings": "ms-settings:network-wifi",
    "sound settings": "ms-settings:sound", "display settings": "ms-settings:display", "task manager": "taskmgr.exe", "command prompt": "cmd.exe", "terminal": "wt.exe",
    "spotify": "spotify:", "discord": "discord:", "steam": "steam://open/main", "xbox": "xbox:",
    "epic games": "com.epicgames.launcher:", "chrome": "chrome.exe", "edge": "msedge.exe",
    "visual studio code": "code", "vs code": "code", "cursor": "cursor", "matlab": "matlab.exe",
    "docker": "Docker Desktop.exe", "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe",
    "camera": "microsoft.windows.camera:", "clock": "ms-clock:", "snipping tool": "ms-screenclip:",
}
FOLDERS = {
    "downloads": "Downloads", "documents": "Documents", "desktop": "Desktop", "pictures": "Pictures",
    "music": "Music", "videos": "Videos", "jarvis documents": "Documents/Jarvis", "home": "",
}
KEYS = {"mute": 0xAD, "down": 0xAE, "up": 0xAF, "next": 0xB0, "previous": 0xB1, "play_pause": 0xB3}


def _config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _closest(name: str, choices) -> str | None:
    name = name.lower().strip()
    if name in choices:
        return name
    near = difflib.get_close_matches(name, list(choices), n=1, cutoff=0.6)
    return near[0] if near else next((c for c in choices if c in name or name in c), None)


def _press(key: int, times: int = 1) -> None:
    for _ in range(times):
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(key, 0, 2, 0)


def _fetch(url: str, data: bytes | None = None, timeout: int = 10) -> str:
    request = urllib.request.Request(url, data=data, headers=BROWSER)
    return urllib.request.urlopen(request, timeout=timeout).read().decode("utf-8", "replace")


def open_app(name: str) -> str:
    known = _closest(name, APPS)
    target = APPS[known] if known else name
    try:
        os.startfile(target)
    except OSError:
        return f"Could not open {name}: Windows does not know a program by that name."
    return f"Opened {known or name}."


def open_folder(name: str) -> str:
    known = _closest(name, FOLDERS)
    if known is None:
        return f"No folder called {name} is known. Known folders: {', '.join(FOLDERS)}."
    path = Path.home() / FOLDERS[known]
    path.mkdir(parents=True, exist_ok=True)
    os.startfile(path)
    return f"Opened the {known} folder."


def volume(action: str) -> str:
    action = action.lower().strip()
    if action in ("up", "down"):
        _press(KEYS[action], 5)  # each press is two points
        return f"Volume turned {action} by ten."
    if "mute" in action:
        _press(KEYS["mute"])
        return "Toggled mute."
    digits = re.search(r"\d+", action)
    if not digits:
        return "Say up, down, mute, or a level from 0 to 100."
    level = min(int(digits.group()), 100)
    _press(KEYS["down"], 50)
    _press(KEYS["up"], level // 2)
    return f"Volume set to about {level}."


def media(action: str) -> str:
    action = action.lower().strip().replace(" ", "_").replace("pause", "play_pause").replace("play_play_pause", "play_pause")
    key = {"play": "play_pause", "skip": "next", "back": "previous"}.get(action, action)
    if key not in ("play_pause", "next", "previous"):
        return "Say play, pause, next or previous."
    _press(KEYS[key])
    return f"Sent {key.replace('_', ' ')} to the media player."


def set_timer(minutes: str, label: str = "") -> str:
    try:
        amount = float(re.search(r"[\d.]+", str(minutes)).group())
    except (AttributeError, ValueError):
        return "How many minutes?"
    label = label.strip() or "timer"
    due = datetime.datetime.now() + datetime.timedelta(minutes=amount)
    message = f"Sir, your {label} is up." if "timer" in label.lower() else f"Sir, a reminder: {label}."
    timer = threading.Timer(amount * 60, lambda: finished.put({"kind": "say", "output": message}))
    timer.daemon = True
    timer.start()
    return f"Set for {due:%H:%M}. It is lost if Jarvis is closed before then."


def system_status() -> str:
    script = (
        "$o=Get-CimInstance Win32_OperatingSystem; $d=Get-PSDrive C; $b=Get-CimInstance Win32_Battery;"
        "$c=(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average;"
        "'Processor load {0:N0} percent. Memory {1:N1} of {2:N1} gigabytes in use. Drive C has {3:N0} gigabytes free of {4:N0}. Battery {5}.' -f "
        "$c, (($o.TotalVisibleMemorySize-$o.FreePhysicalMemory)/1MB), ($o.TotalVisibleMemorySize/1MB), ($d.Free/1GB), (($d.Free+$d.Used)/1GB),"
        "$(if($b){\"$($b.EstimatedChargeRemaining) percent\"}else{'not present'})"
    )
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return done.stdout.strip() or "Could not read the system status."


def find_file(name: str) -> str:
    """Newest matches by file name in Desktop, Documents and Downloads, giving up after a few seconds."""
    words = name.lower().split()
    deadline, hits = time.time() + 4, []
    for folder in ("Desktop", "Documents", "Downloads"):
        for base, dirs, files in os.walk(Path.home() / folder):
            if time.time() > deadline:
                break
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", ".venv", "__pycache__")]
            for file in files:
                if all(word in file.lower() for word in words):
                    hits.append(Path(base, file))
    if not hits:
        return f"No file with {name} in its name was found in Desktop, Documents or Downloads."
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return "Newest first: " + "; ".join(f"{p.name} in {p.parent.name}" for p in hits[:5])


def weather(place: str = "") -> str:
    place = place.strip() or _config().get("city", "Charlotte")
    try:
        data = json.loads(_fetch(f"https://wttr.in/{urllib.parse.quote(place)}?format=j1"))
        now, today = data["current_condition"][0], data["weather"][0]
        rain = max(int(hour["chanceofrain"]) for hour in today["hourly"])
        return (
            f"{place}: {now['weatherDesc'][0]['value'].strip().lower()}, {now['temp_F']} degrees Fahrenheit, feels like {now['FeelsLikeF']}. "
            f"Today's high {today['maxtempF']}, low {today['mintempF']}, chance of rain {rain} percent."
        )
    except (OSError, ValueError, KeyError, IndexError):
        return "The weather service could not be reached."


def web_search(query: str) -> str:
    """Top results from DuckDuckGo as title and snippet. What comes back is web text: information, never instructions."""
    try:
        page = _fetch("https://html.duckduckgo.com/html/", urllib.parse.urlencode({"q": query}).encode())
    except OSError:
        return "The search could not be reached."
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    clean = lambda text: html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    results = [f"{i}. {clean(t)}: {clean(s)}" for i, (t, s) in enumerate(zip(titles, snippets), 1)][:4]
    return "\n".join(results) or "The search returned nothing."


_feed_cache: tuple[float, list] = (0.0, [])


def _canvas_events() -> list[tuple[datetime.datetime, str]] | None:
    """(local start, title) for every event in the Canvas calendar feed, cached for ten minutes."""
    global _feed_cache
    feed = _config().get("canvas_feed", "").strip()
    if not feed:
        return None
    if time.time() - _feed_cache[0] < 600:
        return _feed_cache[1]
    text = re.sub(r"\r?\n[ \t]", "", _fetch(feed.replace("webcal://", "https://"), timeout=20))  # unfold long lines
    events = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        title = re.search(r"^SUMMARY[^:]*:(.*)$", block, re.M)
        start = re.search(r"^DTSTART([^:]*):(\d{8})(?:T(\d{6})(Z?))?", block, re.M)
        if not (title and start):
            continue
        day, clock, utc = start.group(2), start.group(3), start.group(4)
        when = datetime.datetime.strptime(day + (clock or "235900"), "%Y%m%d%H%M%S")
        if utc:
            when = when.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)
        name = title.group(1).replace("\\,", ",").replace("\\;", ";").strip()
        # Canvas ends each title with a long section tag; keep only the first course code in it
        tag = re.search(r"\s*\[\d*-?([A-Z]{4})-(\d{4})[^\]]*\]$", name)
        if tag:
            name = f"{tag.group(1)} {tag.group(2)}: {re.sub(r'\s+', ' ', name[:tag.start()])}"
        events.append((when, name))
    _feed_cache = (time.time(), sorted(events))
    return _feed_cache[1]


def due_soon(days: str = "7") -> str:
    try:
        span = int(re.search(r"\d+", str(days)).group())
    except (AttributeError, ValueError):
        span = 7
    try:
        events = _canvas_events()
    except OSError:
        return "The Canvas calendar could not be reached."
    if events is None:
        return "No Canvas calendar is connected yet. Paste the Canvas calendar feed link into config.json in the Jarvis folder."
    now = datetime.datetime.now()
    upcoming = [(when, title) for when, title in events if now <= when <= now + datetime.timedelta(days=span)]
    if not upcoming:
        return f"Nothing is due in the next {span} days according to Canvas, sir."
    count = "one thing" if len(upcoming) == 1 else f"{len(upcoming)} things"
    lines = [f"You have {count} due in the next {span} days, sir."]
    lines += [f"{_day(when)}, {when:%I:%M %p}: {_spoken(title)}.".replace(", 0", ", ") for when, title in upcoming[:12]]
    if len(upcoming) > 12:
        lines.append(f"And {len(upcoming) - 12} more after that.")
    return "\n".join(lines)


def _day(when: datetime.datetime) -> str:
    """'Sunday the 4th', or 'today' and 'tomorrow' where they apply."""
    days = (when.date() - datetime.date.today()).days
    if days in (0, 1):
        return ("Today", "Tomorrow")[days]
    suffix = "th" if 11 <= when.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(when.day % 10, "th")
    return f"{when:%A} the {when.day}{suffix}"


def _spoken(title: str) -> str:
    """A Canvas title as it should be read aloud: course names for codes, abbreviations spelt out."""
    courses = _config().get("courses", {})
    code, _, rest = title.partition(": ")
    if re.fullmatch(r"[A-Z]{4} \d{4}", code):
        title = f"{courses.get(code, f'{code[:4]} {code[5:7]} {code[7:]}')}, {rest}"
    for short, full in (("HWQ", "homework quiz"), ("RQ", "reading quiz"), ("Rpt", "report"), ("HW", "homework")):
        title = re.sub(rf"\b{short}\b", full, title)
    return re.sub(r"Lab0?(\d+)-", r"Lab \1 ", title).replace(":", ",").strip(" .")


def briefing() -> str:
    today = datetime.datetime.now()
    return f"Today is {today:%A}, {today.day} {today:%B}, sir.\n{weather()}\n{due_soon('3')}"


WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _when(value: str) -> datetime.datetime:
    """An iCalendar date or date-time as local time."""
    stamp = re.sub(r"[^0-9TZ]", "", value)
    day, _, clock = stamp.partition("T")
    moment = datetime.datetime.strptime(day[:8] + (clock.rstrip("Z") + "000000")[:6], "%Y%m%d%H%M%S")
    if stamp.endswith("Z"):
        moment = moment.replace(tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)
    return moment


def _happens_on(day: datetime.date, start: datetime.datetime, rule: str, excluded: set) -> bool:
    """Whether an event starting at `start` and repeating by `rule` (an RRULE, or empty) falls on `day`.
    Covers what timetables use: daily and weekly repeats with an interval, chosen weekdays, an end date or a count."""
    if day in excluded or day < start.date():
        return False
    if not rule:
        return day == start.date()
    parts = dict(item.split("=", 1) for item in rule.split(";") if "=" in item)
    if "UNTIL" in parts and day > _when(parts["UNTIL"]).date():
        return False
    interval = int(parts.get("INTERVAL", 1))
    days = parts["BYDAY"].split(",") if "BYDAY" in parts else [WEEKDAYS[start.weekday()]]
    monday = lambda d: d - datetime.timedelta(days=d.weekday())

    def falls(d: datetime.date) -> bool:
        if parts.get("FREQ") == "DAILY":
            return (d - start.date()).days % interval == 0
        if parts.get("FREQ") == "WEEKLY":
            return WEEKDAYS[d.weekday()] in days and ((monday(d) - monday(start.date())).days // 7) % interval == 0
        return False  # monthly and yearly repeats are not timetable material

    if not falls(day):
        return False
    if "COUNT" in parts:  # the repeat ends after so many occurrences: count them up to this day
        span = (day - start.date()).days
        return sum(falls(start.date() + datetime.timedelta(days=n)) for n in range(span + 1)) <= int(parts["COUNT"])
    return True


_calendar_cache: tuple[float, str] = (0.0, "")


def _calendar_today(today: datetime.date | None = None) -> list[tuple[datetime.datetime, str, str]] | None:
    """(start, title, place) for the events on the user's Apple calendar today, or on the day given, or None if it is not connected."""
    global _calendar_cache
    feed = _config().get("apple_calendar", "").strip()
    if not feed:
        return None
    if time.time() - _calendar_cache[0] > 600:
        _calendar_cache = (time.time(), _fetch(feed.replace("webcal://", "https://"), timeout=20))
    text = re.sub(r"\r?\n[ \t]", "", _calendar_cache[1])  # unfold long lines
    today, events = today or datetime.date.today(), []
    for block in text.split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        field = lambda name: (re.search(rf"^{name}[^:\n]*:(.*)$", block, re.M) or [None, ""])[1].strip()
        if not field("DTSTART") or field("STATUS") == "CANCELLED":
            continue
        start = _when(field("DTSTART"))
        excluded = {_when(stamp).date() for line in re.findall(r"^EXDATE[^:\n]*:(.*)$", block, re.M) for stamp in line.strip().split(",")}
        # A single moved occurrence (RECURRENCE-ID) is listed in its own right, without the rule
        rule = "" if field("RECURRENCE-ID") else field("RRULE")
        if _happens_on(today, start, rule, excluded):
            clean = lambda text: text.replace("\\,", ",").replace("\\;", ";").replace("\\n", ", ").strip()
            events.append((datetime.datetime.combine(today, start.time()), clean(field("SUMMARY")), clean(field("LOCATION"))))
    return sorted(set(events))


def _place(raw: str) -> str:
    """A calendar location as it should be said: no campus prefix, no 'to be announced', EPIC by its name."""
    place = re.sub(r"^Main/Uptown Center\s*", "", raw).replace("Energy Prod & Infrastructr Ctr", "EPIC").strip()
    return "" if place.lower() in ("", "none tba", "tba") else place


def _academic() -> list[tuple[datetime.date, datetime.date, str, bool]]:
    """(first day, last day, title, whether classes are off) for each date on the university's academic calendar."""
    try:
        events = json.loads((ROOT / "academic_calendar.json").read_text(encoding="utf-8"))["events"]
    except (OSError, ValueError, KeyError):
        return []
    day = datetime.date.fromisoformat
    return [(day(e["start"]), day(e["end"]), e["title"], bool(e.get("no_classes"))) for e in events]


def _span(first: datetime.date, last: datetime.date) -> str:
    """'Monday the 12th' or 'Monday the 12th to Tuesday the 13th', with the month once it is not this one."""
    def name(d: datetime.date) -> str:
        said = _day(datetime.datetime.combine(d, datetime.time()))
        return said if d.month == datetime.date.today().month and d.year == datetime.date.today().year or said in ("Today", "Tomorrow") else f"{said} of {d:%B}"
    return name(first) if first == last else f"{name(first)} to {name(last)}"


def academic_dates(count: str = "6") -> str:
    try:
        wanted = min(max(int(re.search(r"\d+", str(count)).group()), 1), 15)
    except (AttributeError, ValueError):
        wanted = 6
    today = datetime.date.today()
    coming = [e for e in _academic() if e[1] >= today][:wanted]
    if not coming:
        return "I have no more dates on the university calendar, sir. It may need updating for the new academic year."
    return "\n".join(["The next dates on the university calendar, sir."] + [f"{_span(first, last)}: {title}." for first, last, title, _ in coming])


def _which_day(text: str) -> datetime.date:
    """The day a request means: tomorrow, a weekday by name (the next one, today included), otherwise today."""
    text, today = text.lower(), datetime.date.today()
    if "tomorrow" in text:
        return today + datetime.timedelta(days=2 if "day after" in text else 1)
    for number, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")):
        if name in text:
            return today + datetime.timedelta(days=(number - today.weekday()) % 7)
    return today


def classes(day: str = "") -> str:
    """The user's classes and their times on one day, from their Apple calendar, never from Canvas."""
    date = _which_day(day)
    said = _day(datetime.datetime.combine(date, datetime.time()))
    said = said.lower() if said in ("Today", "Tomorrow") else f"on {said}"
    off = [title for first, last, title, no_classes in _academic() if first <= date <= last and no_classes]
    if off:
        return f"No classes {said}, sir. The university calendar has {off[0]}."
    try:
        found = _calendar_today(date)
    except OSError:
        return "I could not reach your Apple calendar just now, sir."
    if found is None:
        return "Your Apple calendar is not connected yet, sir, so I cannot tell you your classes."
    if not found:
        return f"You have no classes {said}, sir."
    lines = [f"You have {'one class' if len(found) == 1 else f'{len(found)} classes'} {said}, sir."]
    for when, title, place in found:
        hour = "All day" if when.time() == datetime.time(0) else f"{when:%I:%M %p}".lstrip("0")
        lines.append(f"{hour}: {title}{', in ' + _place(place) if _place(place) else ''}.")
    return "\n".join(lines)


VERSES = [  # one for each day in turn; the words themselves are always fetched, never recalled from memory
    "John 3:16", "Philippians 4:6-7", "Philippians 4:13", "Proverbs 3:5-6", "Romans 8:28", "Jeremiah 29:11",
    "Isaiah 40:31", "Joshua 1:9", "Psalm 23:1-3", "Psalm 46:1", "Psalm 118:24", "Psalm 119:105", "Psalm 121:1-2",
    "Matthew 6:33-34", "Matthew 11:28-30", "Romans 12:2", "Romans 12:12", "2 Timothy 1:7", "Isaiah 41:10",
    "1 Corinthians 13:4-7", "1 Corinthians 16:13-14", "Galatians 5:22-23", "Galatians 6:9", "Ephesians 2:8-9",
    "Ephesians 4:32", "Colossians 3:23-24", "Hebrews 11:1", "Hebrews 12:1-2", "James 1:2-4", "James 1:5",
    "1 Peter 5:7", "1 John 4:18-19", "Lamentations 3:22-23", "Micah 6:8", "Proverbs 16:3", "Proverbs 27:17",
    "Psalm 27:1", "Psalm 34:8", "Psalm 37:4", "Psalm 55:22", "Psalm 91:1-2", "Psalm 139:13-14", "Isaiah 26:3",
    "John 14:6", "John 14:27", "John 15:5", "John 16:33", "Romans 5:3-5", "Romans 8:38-39", "Romans 15:13",
    "2 Corinthians 5:7", "2 Corinthians 12:9", "Ephesians 3:20-21", "Ephesians 6:10-11", "Philippians 1:6",
    "Philippians 2:3-4", "Colossians 3:12-14", "1 Thessalonians 5:16-18", "Hebrews 4:16", "Hebrews 13:8",
    "Matthew 5:14-16", "Matthew 7:7-8", "Matthew 22:37-39", "Mark 10:27", "Luke 6:31", "Deuteronomy 31:6",
    "Ecclesiastes 3:1", "Zephaniah 3:17", "Psalm 19:14", "Psalm 100:4-5", "Proverbs 4:23", "Isaiah 43:2", "Romans 10:9",
]
TRANSLATION = "webbe"  # World English Bible, British Edition: public domain, modern English, and says "the LORD"
VERSE_CACHE = ROOT / "verses.json"  # passages already fetched, so they need no internet the second time


def _passage(reference: str) -> tuple[str, str] | None:
    """(reference as the source names it, text) from the chosen translation, or None if it cannot be had."""
    try:
        cache = json.loads(VERSE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    key = " ".join(reference.lower().split())
    if key not in cache:
        try:
            found = json.loads(_fetch(f"https://bible-api.com/{urllib.parse.quote(reference)}?translation={TRANSLATION}"))
            cache[key] = [found["reference"], " ".join(found["text"].split())]
        except (OSError, ValueError, KeyError):
            return None
        VERSE_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
    return tuple(cache[key])


def _spoken_reference(reference: str) -> str:
    """'Philippians 4:6-7' as 'Philippians, chapter 4, verses 6 to 7'."""
    match = re.fullmatch(r"(.+?) (\d+):(\d+)(?:-(\d+))?", reference)
    if not match:
        return reference
    book, chapter, first, last = match.groups()
    verses = f"verses {first} to {last}" if last else f"verse {first}"
    return f"{book}, chapter {chapter}, {verses}"


def bible(reference: str = "") -> str:
    of_the_day = not reference.strip()
    wanted = VERSES[datetime.date.today().toordinal() % len(VERSES)] if of_the_day else reference.strip()
    found = _passage(wanted)
    if found is None:
        return f"I could not fetch {wanted} just now, sir, and I will not quote scripture from memory."
    name, text = found
    if len(text) > 1500:
        return f"{name} is a long passage, sir. Ask me for a few verses of it at a time."
    opening = "Your verse for today is from" if of_the_day else "From"
    return f"{opening} {_spoken_reference(name)}.\n{text}"


def good_morning() -> str:
    """The user's morning rundown: the date, the local weather, today's classes, what is due today, and a verse."""
    today = datetime.datetime.now()
    lines = [f"Good morning, sir. It is {today:%A}, {today.day} {today:%B}.", weather()]
    university = _academic()
    on_today = [(title, off) for first, last, title, off in university if first <= today.date() <= last]
    lines += [f"On the university calendar today: {title}." for title, _ in on_today]
    try:
        classes = _calendar_today()
    except OSError:
        classes = "unreachable"
    if any(off for _, off in on_today):
        lines.append("So there are no regular classes today.")
    elif classes is None:
        lines.append("Your Apple calendar is not connected yet, so I cannot tell you today's classes.")
    elif classes == "unreachable":
        lines.append("I could not reach your Apple calendar this morning.")
    elif not classes:
        lines.append("There is nothing on your calendar today.")
    else:
        lines.append("One class today:" if len(classes) == 1 else f"{len(classes)} classes today:")
        for when, title, place in classes:
            hour = "All day" if when.time() == datetime.time(0) else f"{when:%I:%M %p}".lstrip("0")
            lines.append(f"{hour}: {title}{', in ' + _place(place) if _place(place) else ''}.")
    try:
        events = _canvas_events()
    except OSError:
        events = None
    due = [(when, title) for when, title in events or [] if when.date() == today.date()]
    if events is None:
        lines.append("I could not check Canvas for what is due.")
    elif not due:
        lines.append("Nothing is due today.")
    else:
        lines.append("One thing is due today:" if len(due) == 1 else f"{len(due)} things are due today:")
        lines += [f"{when:%I:%M %p}: {_spoken(title)}.".lstrip("0") for when, title in due]
    week = today.date() + datetime.timedelta(days=7)
    soon = [(first, last, title) for first, last, title, _ in university if today.date() < first <= week][:3]
    if soon:
        lines.append("Coming up at the university this week:")
        lines += [f"{_span(first, last)}: {title}." for first, last, title in soon]
    report = ROOT / "tune-up-report.md"
    if report.exists() and time.time() - report.stat().st_mtime < 36 * 3600:  # mentioned the morning after it runs
        lines.append("Claude gave me my weekly tune-up, sir. The report is in my folder if you care to read it.")
    if enabled("bible"):
        lines.append(bible())
    return "\n".join(lines)


# Skills whose results are read out exactly as returned, a line at a time,
# because the small model blurs lists when it summarises them, and because
# scripture must never be paraphrased by it
RECITED = {"due_soon", "briefing", "bible", "good_morning", "academic_dates", "classes"}


# name -> (function, description for the model, {argument: description})
SKILLS = {
    "open_app": (open_app, "Open a program or a settings page on the laptop, such as Spotify, Discord, Chrome, calculator, settings, bluetooth settings or sound settings.", {"name": "The program's name."}),
    "open_folder": (open_folder, "Open a folder in File Explorer: downloads, documents, desktop, pictures, music, videos or jarvis documents.", {"name": "The folder's name."}),
    "volume": (volume, "Change the laptop's volume.", {"action": "up, down, mute, or a level from 0 to 100."}),
    "media": (media, "Control whatever is playing music or video.", {"action": "play, pause, next or previous."}),
    "set_timer": (set_timer, "Set a timer or a reminder that Jarvis announces aloud.", {"minutes": "Minutes from now.", "label": "What it is for, or 'timer'."}),
    "system_status": (system_status, "Processor load, memory in use, free disk space and battery level.", {}),
    "find_file": (find_file, "Find files by name in Desktop, Documents and Downloads.", {"name": "Words from the file name."}),
    "weather": (weather, "Current weather and today's forecast.", {"place": "City, or empty for {USER}'s city."}),
    "web_search": (web_search, "Search the internet for current or factual information.", {"query": "The search words."}),
    "due_soon": (due_soon, "Coursework deadlines and events from {USER}'s Canvas calendar.", {"days": "How many days ahead, usually 7."}),
    "classes": (classes, "{USER}'s classes and their times on a given day, from their Apple calendar. Use this, never due_soon, when {USER} asks what classes he has.", {"day": "today, tomorrow, or a weekday name."}),
    "briefing": (briefing, "A rundown of today's date, the weather and what is due in the next three days.", {}),
    "academic_dates": (academic_dates, "Upcoming dates on the university academic calendar: breaks, withdrawal and registration deadlines, last day of classes, finals, commencement.", {"count": "How many dates to give, usually 6."}),
    "good_morning": (good_morning, "{USER}'s morning rundown: weather, today's classes, what is due today, and a Bible verse if that feature is on.", {}),
    "bible": (bible, "Read out a Bible passage word for word, or the verse of the day. Always use this for scripture; never quote the Bible from memory.", {"reference": "Book, chapter and verses, such as John 3:16, or empty for the verse of the day."}),
}

if not enabled("bible"):  # an optional feature, switched on in config.json
    del SKILLS["bible"]


def run(name: str, arguments: dict) -> str:
    function, _, wanted = SKILLS[name]
    try:
        return function(**{key: str(arguments.get(key, "")) for key in wanted})
    except Exception as error:  # a broken skill must not take the conversation down
        return f"That did not work: {error}"
