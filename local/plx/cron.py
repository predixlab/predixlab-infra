"""Five-field cron expressions evaluated in UTC, as Azure Container Apps jobs do."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_MONTHS = {name: i for i, name in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_WEEKDAYS = {name: i for i, name in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}

# (minimum, maximum, names) for minute, hour, day of month, month, day of week.
_FIELDS = (
    (0, 59, {}),
    (0, 23, {}),
    (1, 31, {}),
    (1, 12, _MONTHS),
    (0, 7, _WEEKDAYS),
)

_SEARCH_LIMIT = timedelta(days=366 * 5)


class CronError(ValueError):
    pass


def _value(token: str, names: dict[str, int], low: int, high: int) -> int:
    token = token.strip().upper()
    if token in names:
        return names[token]
    if not token.isdigit():
        raise CronError(f"invalid cron value {token!r}")
    number = int(token)
    if not low <= number <= high:
        raise CronError(f"cron value {number} outside {low}-{high}")
    return number


def _parse_field(text: str, low: int, high: int, names: dict[str, int]) -> tuple[frozenset[int], bool]:
    """Returns the allowed values and whether the field was unrestricted ("*")."""
    values: set[int] = set()
    unrestricted = False
    for part in text.split(","):
        if not part:
            raise CronError(f"empty list item in {text!r}")
        step = 1
        has_step = "/" in part
        if has_step:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"invalid step in {text!r}")
            step = int(step_text)
        if part in ("*", "?"):
            start, end = low, high
            unrestricted = unrestricted or step == 1
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = _value(start_text, names, low, high), _value(end_text, names, low, high)
            if start > end:
                raise CronError(f"descending range in {text!r}")
        else:
            start = _value(part, names, low, high)
            # "N/step" means "N-max/step" for this list item only.
            end = high if has_step else start
        values.update(range(start, end + 1, step))
    return frozenset(values), unrestricted


class CronExpression:
    def __init__(self, expression: str) -> None:
        fields = expression.split()
        if len(fields) != 5:
            raise CronError(f"expected 5 cron fields, got {len(fields)}: {expression!r}")
        self.expression = expression
        parsed = [_parse_field(text, low, high, names) for text, (low, high, names) in zip(fields, _FIELDS)]
        (self.minutes, _), (self.hours, _), (self.days, self._any_day), (self.months, _), (weekdays, self._any_weekday) = parsed
        # Both 0 and 7 mean Sunday.
        self.weekdays = frozenset(0 if day == 7 else day for day in weekdays)

    def _day_matches(self, moment: datetime) -> bool:
        day_ok = moment.day in self.days
        weekday_ok = (moment.isoweekday() % 7) in self.weekdays
        if self._any_day and self._any_weekday:
            return True
        if self._any_day:
            return weekday_ok
        if self._any_weekday:
            return day_ok
        # Vixie cron: when both fields are restricted either one may match.
        return day_ok or weekday_ok

    def matches(self, moment: datetime) -> bool:
        moment = _utc(moment)
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and moment.month in self.months
            and self._day_matches(moment)
        )

    def next_after(self, moment: datetime) -> datetime:
        """First fire time strictly after ``moment``."""
        candidate = _utc(moment).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = candidate + _SEARCH_LIMIT
        while candidate < limit:
            if candidate.month not in self.months:
                year, month = (candidate.year + 1, 1) if candidate.month == 12 else (candidate.year, candidate.month + 1)
                candidate = candidate.replace(year=year, month=month, day=1, hour=0, minute=0)
                continue
            if not self._day_matches(candidate):
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.hour not in self.hours:
                candidate = (candidate + timedelta(hours=1)).replace(minute=0)
                continue
            if candidate.minute not in self.minutes:
                candidate += timedelta(minutes=1)
                continue
            return candidate
        raise CronError(f"{self.expression!r} never fires")

    def previous_at_or_before(self, moment: datetime, lookback: timedelta = timedelta(days=32)) -> datetime | None:
        """Latest fire time at or before ``moment`` within ``lookback``."""
        moment = _utc(moment).replace(second=0, microsecond=0)
        candidate = moment
        earliest = moment - lookback
        while candidate >= earliest:
            if not (candidate.month in self.months and self._day_matches(candidate)):
                candidate = candidate.replace(hour=23, minute=59) - timedelta(days=1)
                continue
            if candidate.hour not in self.hours:
                candidate = candidate.replace(minute=59) - timedelta(hours=1)
                continue
            if candidate.minute in self.minutes:
                return candidate
            candidate -= timedelta(minutes=1)
        return None


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
