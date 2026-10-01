from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")

def now():
    return datetime.now(TZ)

def period(at):
    if at.tzinfo is None:
        raise ValueError("Horário precisa de fuso.")
    local = at.astimezone(TZ)
    hour = 18 if local.hour >= 18 else 12 if local.hour >= 12 else 6 if local.hour >= 6 else 18
    day = local - timedelta(days=1) if local.hour < 6 else local
    return day.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()

def next_restart(at):
    start = datetime.fromisoformat(period(at))
    return start + timedelta(hours=12 if start.hour == 18 else 6)
