"""Write academic_calendar.json out as an .ics file that Apple Calendar (or any other) can import."""
import datetime
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).parent
NAME = json.loads((ROOT / "academic_calendar.json").read_text(encoding="utf-8")).get("name", "Academic Calendar")
OUT = Path.home() / "Documents" / "Jarvis" / f"{NAME}.ics"

data = json.loads((ROOT / "academic_calendar.json").read_text(encoding="utf-8"))
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Jarvis//Academic Calendar//EN", f"X-WR-CALNAME:{NAME}"]
for event in data["events"]:
    first = datetime.date.fromisoformat(event["start"])
    after = datetime.date.fromisoformat(event["end"]) + datetime.timedelta(days=1)  # all-day events end on the day after
    title = event["title"] + (" (no classes)" if event["no_classes"] else "")
    lines += [
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid5(uuid.NAMESPACE_URL, event['start'] + event['title'])}@jarvis",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{first:%Y%m%d}",
        f"DTEND;VALUE=DATE:{after:%Y%m%d}",
        "SUMMARY:" + title.replace(",", "\\,").replace(";", "\\;"),
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]
lines.append("END:VCALENDAR")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
print(f"{len(data['events'])} events written to {OUT}")
