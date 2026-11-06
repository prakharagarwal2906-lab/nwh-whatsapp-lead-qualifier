"""Dealer calendar: is a timestamp outside working hours, and when is the next callback?"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import DealerConfig

PRE_CLOSE_BUFFER_MIN = 15   # a lead created in the last 15 minutes of a window will not be called today
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse(t: str) -> time:
    h, m = t.split(":")
    return time(int(h), int(m))


class DealerCalendar:
    def __init__(self, cfg: DealerConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.holidays = {date.fromisoformat(d) for d in cfg.holidays}

    def windows(self, day: date) -> list[tuple[datetime, datetime]]:
        if day in self.holidays:
            return []
        spans = self.cfg.working_hours.get(DAYS[day.weekday()], [])
        return [
            (
                datetime.combine(day, _parse(start), self.tz),
                datetime.combine(day, _parse(end), self.tz),
            )
            for start, end in spans
        ]

    def is_non_working(self, when: datetime) -> bool:
        """True if a lead created at `when` will not be called before close.

        The last PRE_CLOSE_BUFFER_MIN minutes of a window also count as
        non-working, since nobody will realistically call that lead today.
        """
        local = when.astimezone(self.tz)
        buffer = timedelta(minutes=PRE_CLOSE_BUFFER_MIN)
        for start, end in self.windows(local.date()):
            if start <= local < end - buffer:
                return False
        return True

    def next_window_start(self, when: datetime) -> tuple[datetime, datetime] | None:
        """Next working window that starts after `when` (searches 14 days)."""
        local = when.astimezone(self.tz)
        for offset in range(0, 15):
            day = local.date() + timedelta(days=offset)
            for start, end in self.windows(day):
                if start > local:
                    return start, end
        return None

    def callback_phrase(self, when: datetime) -> tuple[str, str, str]:
        """('tomorrow' | 'today' | 'on Monday', '9:30 AM', '5:30 PM')."""
        nxt = self.next_window_start(when)
        if nxt is None:
            return ("soon", "", "")
        start, end = nxt
        local_day = when.astimezone(self.tz).date()
        delta = (start.date() - local_day).days
        if delta == 0:
            day = "today"
        elif delta == 1:
            day = "tomorrow"
        else:
            day = "on " + start.strftime("%A")
        fmt = lambda d: d.strftime("%I:%M %p").lstrip("0")  # noqa: E731
        return day, fmt(start), fmt(end)

    # ---- visit booking -------------------------------------------------
    def next_open_days(self, when: datetime, n: int = 7) -> list[date]:
        """The next `n` days the dealer is open, starting today if a visit is still possible."""
        local = when.astimezone(self.tz)
        out: list[date] = []
        for offset in range(0, 31):
            day = local.date() + timedelta(days=offset)
            wins = self.windows(day)
            if not wins:
                continue
            if offset == 0 and local >= wins[-1][1] - timedelta(hours=2):
                continue  # too late for a same-day visit
            out.append(day)
            if len(out) == n:
                break
        return out

    def hourly_slots(self, day: date, max_slots: int = 10) -> list[tuple[str, str]]:
        """[("1130", "11:30 AM - 12:30 PM"), ...] within working hours (WhatsApp lists hold 10 rows)."""
        fmt = lambda d: d.strftime("%I:%M %p").lstrip("0")  # noqa: E731
        out = []
        for start, end in self.windows(day):
            t = start
            while t + timedelta(hours=1) <= end and len(out) < max_slots:
                out.append((t.strftime("%H%M"), f"{fmt(t)} - {fmt(t + timedelta(hours=1))}"))
                t += timedelta(hours=1)
        return out


def day_label(day: date) -> str:
    return day.strftime("%a, ") + str(day.day) + day.strftime(" %b")
