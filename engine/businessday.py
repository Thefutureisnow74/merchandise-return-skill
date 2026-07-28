#!/usr/bin/env python3
"""businessday.py — shared business-day date math for the AMBS Merchandise Returns Engine.

Stdlib only. The single source of truth for "N business days" SLA/deadline math, so the
calendar-vs-business-day bug from the PPG incident (MER-3) cannot recur per-case. Before
this, the SLA math lived ad-hoc in the ladder/case_tick and one path treated a "7 business
day" promise as 7 CALENDAR days — which would have fired an escalation on 2026-07-24 when
the letter's real deadline (7 business days from Fri 2026-07-17) is end of Tue 2026-07-28.

A "business day" here = Monday–Friday and NOT a US federal holiday (observed-day adjusted).

⚠️ TIMEZONE (2026-07-28). Every deadline decision in this engine is DATE-ONLY, and the
container runs UTC. `date.today()` under UTC is already TOMORROW from 19:00 America/Chicago
onward (18:00 in winter), so a phase deadline could elapse — and an escalation fire — a full
calendar day early, every single evening. Nothing in the engine may call `date.today()` for a
decision that drives a deadline. Call `businessday.today()` instead: it resolves "today" in the
USER'S timezone (profile `timezone`, default America/Chicago, env override MER_TIMEZONE).

Public API:
    today(tz=None) -> date                           # "today" in the profile's timezone
    now(tz=None) -> datetime                         # tz-aware now, same zone
    profile_timezone() -> str
    is_business_day(d) -> bool
    add_business_days(start_date, n) -> date        # n business days on/after start (n>=0 walks forward)
    business_day_deadline(start_date, n) -> date     # the date that is n business days AFTER start_date
    federal_holidays_named(year) -> {name: date}
    US_FEDERAL_HOLIDAYS_2026  -> frozenset[date]
"""
import json
import os
from datetime import date, datetime, timedelta, timezone, tzinfo

# ---------------------------------------------------------------------------------------
# Timezone — "today" is the USER'S today, never the container's.
# ---------------------------------------------------------------------------------------
DEFAULT_TZ = "America/Chicago"
_PROFILE_PATHS = (
    os.environ.get("MER_PROFILE") or "",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile.json"),
    "/opt/data/scripts/profile.json",
)


def profile_timezone():
    """The engine's operating timezone: $MER_TIMEZONE, else profile.json `timezone`, else CT.

    Deliberately tolerant — a missing/unreadable profile falls back to the documented
    default rather than raising, because the alternative (silently using UTC) is the bug.
    """
    env = (os.environ.get("MER_TIMEZONE") or "").strip()
    if env:
        return env
    for path in _PROFILE_PATHS:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                tz = (json.load(fh) or {}).get("timezone")
            if tz:
                return str(tz).strip()
        except Exception:
            continue
    return DEFAULT_TZ


class _USCentral(tzinfo):
    """Last-resort US Central implementation for hosts with no tz database.

    Only used when zoneinfo cannot load the requested zone. Implements the post-2007 US
    rule: DST from the 2nd Sunday in March 02:00 to the 1st Sunday in November 02:00.
    Being approximate is fine here — being UTC is not.
    """

    def _dst_bounds(self, year):
        # 2nd Sunday in March, 1st Sunday in November (weekday Sun == 6)
        start = _nth_weekday(year, 3, 6, 2)
        end = _nth_weekday(year, 11, 6, 1)
        return (datetime(start.year, start.month, start.day, 2),
                datetime(end.year, end.month, end.day, 2))

    def utcoffset(self, dt):
        return timedelta(hours=-5) if self._is_dst(dt) else timedelta(hours=-6)

    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "CDT" if self._is_dst(dt) else "CST"

    def _is_dst(self, dt):
        if dt is None:
            return False
        naive = dt.replace(tzinfo=None)
        start, end = self._dst_bounds(naive.year)
        return start <= naive < end


def _tzinfo(name=None):
    name = name or profile_timezone()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return _USCentral()


def now(tz=None):
    """Timezone-aware 'now' in the engine's operating timezone."""
    return datetime.now(timezone.utc).astimezone(_tzinfo(tz))


def today(tz=None):
    """TODAY IN THE USER'S TIMEZONE — the only 'today' a deadline decision may use.

    `date.today()` on a UTC container rolls over at 19:00 Central. Every deadline
    comparison that used it could fire a full day early; this is the replacement.
    """
    return now(tz).date()


def _nth_weekday(year, month, weekday, n):
    """The date of the nth given weekday in a month. weekday: Mon=0..Sun=6."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    """The date of the last given weekday in a month."""
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _observed(d):
    """US federal observed-day rule: Sat holiday -> observed Fri; Sun holiday -> observed Mon."""
    if d.weekday() == 5:          # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:          # Sunday
        return d + timedelta(days=1)
    return d


def federal_holidays_named(year):
    """{holiday name: observed date} for a given year. ALWAYS 11 entries.

    Kept as a NAME->date mapping rather than a bare set so a year in which two observed
    dates collide is visible instead of silently shrinking the set (a dropped holiday makes
    a deadline land one business day early — the same class of bug as the calendar/business
    day mix-up this module exists to prevent).
    """
    return {
        "New Year's Day": _observed(date(year, 1, 1)),
        "MLK Jr. Day": _nth_weekday(year, 1, 0, 3),             # 3rd Mon Jan
        "Washington's Birthday": _nth_weekday(year, 2, 0, 3),   # 3rd Mon Feb
        "Memorial Day": _last_weekday(year, 5, 0),              # last Mon May
        "Juneteenth": _observed(date(year, 6, 19)),
        "Independence Day": _observed(date(year, 7, 4)),
        "Labor Day": _nth_weekday(year, 9, 0, 1),               # 1st Mon Sep
        "Columbus / Indigenous Peoples' Day": _nth_weekday(year, 10, 0, 2),  # 2nd Mon Oct
        "Veterans Day": _observed(date(year, 11, 11)),
        "Thanksgiving": _nth_weekday(year, 11, 3, 4),           # 4th Thu Nov
        "Christmas Day": _observed(date(year, 12, 25)),
    }


def _us_federal_holidays(year):
    """The observed dates of the 11 US federal holidays for a given year."""
    return frozenset(federal_holidays_named(year).values())


# Explicit 2026 set (the engine's operating year) — usable as a constant.
US_FEDERAL_HOLIDAYS_2026 = _us_federal_holidays(2026)


_HOLIDAY_CACHE = {}


def _holidays_for(d):
    """Holiday set covering the year of d, INCLUDING neighbouring-year spillover.

    New Year's Day falling on a Saturday is OBSERVED on Friday 31 December of the PREVIOUS
    year (e.g. 2022-01-01 was a Saturday, so Fri 2021-12-31 was the federal holiday). Looking
    only at `d.year` misses it and counts 12/31 as a business day — a deadline crossing it
    lands a day early. The union of year-1 / year / year+1 makes that structurally impossible.
    """
    y = d.year
    hit = _HOLIDAY_CACHE.get(y)
    if hit is None:
        hit = frozenset().union(*(_us_federal_holidays(yy) for yy in (y - 1, y, y + 1)))
        _HOLIDAY_CACHE[y] = hit
    return hit


def is_business_day(d):
    """True if d is Mon–Fri and not a US federal holiday (observed)."""
    if d.weekday() >= 5:          # Sat/Sun
        return False
    return d not in _holidays_for(d)


def add_business_days(start_date, n):
    """Return the date reached by stepping n business days from start_date.

    n > 0 walks forward (skipping weekends/holidays); n < 0 walks backward; n == 0
    returns start_date unchanged even if it is not itself a business day.
    """
    if n == 0:
        return start_date
    step = 1 if n > 0 else -1
    remaining = abs(n)
    d = start_date
    while remaining > 0:
        d = d + timedelta(days=step)
        if is_business_day(d):
            remaining -= 1
    return d


def business_day_deadline(start_date, n):
    """The date that is n business days AFTER start_date (the SLA deadline).

    Equivalent to add_business_days(start_date, n) for n >= 0. This is the function
    escalation ladders should call: "letter sent Fri 7/17, 7 business days" -> 7/28.
    """
    return add_business_days(start_date, n)


def _deadline_cli(argv):
    """M44: business-day arithmetic as a command, for the human path.

    case_tick already calls business_day_deadline() when it arms a phase clock, but a
    person drafting a letter by hand needs the same answer without running the tick. The
    PPG incident (MER-3) is exactly this: "seven (7) business days" was computed as seven
    CALENDAR days and the deadline was wrong by two days.

        python3 businessday.py --deadline 7
        python3 businessday.py --deadline 7 --from 2026-07-17
        python3 businessday.py --is-business-day 2026-07-03

    Exit 0 on a computed answer, 3 when it cannot compute one. It refuses a negative or
    unparseable day count rather than emitting a date that would silently be wrong — a
    wrong deadline is worse than no deadline, because it looks like an answer.
    """
    import argparse
    ap = argparse.ArgumentParser(description="US business-day deadline arithmetic")
    ap.add_argument("--deadline", type=str, default=None,
                    help="number of BUSINESS days from --from (default: today)")
    ap.add_argument("--from", dest="start", default=None, help="start date YYYY-MM-DD")
    ap.add_argument("--is-business-day", dest="probe", default=None,
                    help="ask whether one date is a business day")
    a = ap.parse_args(argv)

    def _parse(s):
        from datetime import datetime
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()

    if a.probe:
        try:
            d = _parse(a.probe)
        except ValueError:
            print("CANNOT ANSWER — %r is not a YYYY-MM-DD date." % a.probe)
            return 3
        ok = is_business_day(d)
        print("%s is %s (%s)" % (d, "a BUSINESS day" if ok else "NOT a business day",
                                 d.strftime("%A")))
        return 0

    if a.deadline is None:
        print("CANNOT ANSWER — give --deadline N (business days) or --is-business-day DATE.")
        return 3
    try:
        n = int(a.deadline)
    except ValueError:
        print("CANNOT ANSWER — --deadline must be a whole number of business days, got %r."
              % a.deadline)
        return 3
    if n < 0:
        print("CANNOT ANSWER — a deadline cannot be a negative number of business days.")
        return 3
    try:
        start = _parse(a.start) if a.start else date.today()
    except ValueError:
        print("CANNOT ANSWER — --from %r is not a YYYY-MM-DD date." % a.start)
        return 3

    due = business_day_deadline(start, n)
    naive = start + timedelta(days=n)
    print("%d business day(s) from %s (%s)  ->  %s (%s)"
          % (n, start, start.strftime("%a"), due, due.strftime("%a")))
    if due != naive:
        print("  (a calendar +%d would have said %s — %d day(s) too early; that error is "
              "the MER-3 incident)" % (n, naive, (due - naive).days))
    skipped = sorted(d for d in _holidays_for(start) if start < d <= due)
    if skipped:
        print("  holidays skipped: %s" % ", ".join(str(d) for d in skipped))
    return 0


if __name__ == "__main__":
    import sys
    if "--deadline" in sys.argv or "--is-business-day" in sys.argv:
        sys.exit(_deadline_cli(sys.argv[1:]))

    # --- The known-correct case from the PPG incident (MER-3) ---
    # Tier-1 letter sent Fri 2026-07-17 gave "seven (7) BUSINESS days".
    # 7 business days from Fri 7/17 = end of Tue 2026-07-28 (NOT +7 calendar = 7/24).
    start = date(2026, 7, 17)
    assert business_day_deadline(start, 7) == date(2026, 7, 28), \
        "PPG 7-business-day deadline must be 2026-07-28, got %s" % business_day_deadline(start, 7)
    # And it must NOT collapse to the calendar-day answer that caused the bug.
    assert business_day_deadline(start, 7) != start + timedelta(days=7), \
        "business-day math must differ from +7 calendar days"

    # --- Crossing a weekend ---
    # Fri 2026-07-17 + 1 business day = Mon 2026-07-20 (Sat/Sun skipped).
    assert add_business_days(date(2026, 7, 17), 1) == date(2026, 7, 20), \
        "1 business day after Fri 7/17 must be Mon 7/20"

    # --- Crossing a holiday ---
    # Independence Day 2026 (Sat 7/4) is observed Fri 2026-07-03.
    assert date(2026, 7, 3) in US_FEDERAL_HOLIDAYS_2026, "7/3 must be observed Independence Day"
    assert not is_business_day(date(2026, 7, 3)), "observed holiday 7/3 is not a business day"
    # Thu 2026-07-02 + 1 business day skips Fri 7/3 (holiday) + weekend -> Mon 2026-07-06.
    assert add_business_days(date(2026, 7, 2), 1) == date(2026, 7, 6), \
        "1 business day after Thu 7/2 must skip observed holiday + weekend to Mon 7/6"

    # --- Sanity on the holiday set ---
    assert len(US_FEDERAL_HOLIDAYS_2026) == 11, "expected 11 federal holidays"
    assert date(2026, 11, 26) in US_FEDERAL_HOLIDAYS_2026, "Thanksgiving 2026 = 11/26"
    assert date(2026, 1, 19) in US_FEDERAL_HOLIDAYS_2026, "MLK 2026 = 1/19"

    # --- The holiday count is asserted PER YEAR, not just for 2026 (2026-07-28 fix) ---
    # The old assertion only covered the constant for the engine's first operating year. A
    # later year in which two OBSERVED dates collided would silently produce a 10-element
    # set, and every deadline crossing the missing holiday would come out one business day
    # early. Named mapping => 11 always; the set is checked for collisions explicitly.
    for _y in range(2020, 2041):
        named = federal_holidays_named(_y)
        assert len(named) == 11, "%d: expected 11 NAMED federal holidays, got %d" % (_y, len(named))
        observed = _us_federal_holidays(_y)
        if len(observed) != 11:
            dupes = sorted(d for d in observed if list(named.values()).count(d) > 1)
            raise AssertionError(
                "%d: two federal holidays share an observed date %s — the set collapsed to "
                "%d entries and a holiday would be silently dropped from the business-day "
                "count. Fix federal_holidays_named() before shipping this year."
                % (_y, dupes, len(observed)))
        assert all(is_business_day(d) is False for d in observed), \
            "%d: every observed federal holiday must be a non-business day" % _y

    # --- Neighbouring-year spillover: NYD 2022 fell on a Saturday -> observed Fri 2021-12-31 ---
    assert not is_business_day(date(2021, 12, 31)), \
        "Fri 2021-12-31 was the observed New Year's Day holiday, not a business day"
    assert add_business_days(date(2021, 12, 30), 1) == date(2022, 1, 3), \
        "1 business day after Thu 2021-12-30 must skip the observed 12/31 holiday + weekend"

    # --- Timezone: "today" is the USER'S today, not the UTC container's ---
    # This is the 2026-07-28 defect: date.today() under UTC is already tomorrow from ~19:00
    # Central, so a deadline could elapse (and an escalation fire) a full day early.
    _tz_today = today()
    _utc_today = datetime.now(timezone.utc).date()
    assert isinstance(_tz_today, date), "today() must return a date"
    assert (_utc_today - _tz_today).days in (0, 1), \
        "profile-timezone today (%s) must be the same day as UTC or one day behind, got %s" \
        % (_tz_today, _utc_today)
    assert profile_timezone(), "an operating timezone must always resolve (default %s)" % DEFAULT_TZ
    # The fallback zone must behave like US Central even with no tz database present.
    _fb = _USCentral()
    assert _fb.utcoffset(datetime(2026, 7, 15, 12)) == timedelta(hours=-5), "July = CDT (-5)"
    assert _fb.utcoffset(datetime(2026, 1, 15, 12)) == timedelta(hours=-6), "January = CST (-6)"
    # 19:00 Central on 2026-07-27 is 2026-07-28 in UTC — the exact off-by-one-day trap.
    _evening_utc = datetime(2026, 7, 28, 0, 30, tzinfo=timezone.utc)
    assert _evening_utc.date() == date(2026, 7, 28), "UTC says the 28th"
    assert _evening_utc.astimezone(_tzinfo("America/Chicago")).date() == date(2026, 7, 27), \
        "Central says it is still the 27th — this is the day a deadline would fire early"

    print("OK — all businessday.py self-tests passed")
    print("  PPG: 7 business days after Fri 2026-07-17 = %s (calendar +7 would be %s)"
          % (business_day_deadline(start, 7), start + timedelta(days=7)))
    print("  weekend: 1 business day after Fri 2026-07-17 = %s" % add_business_days(date(2026, 7, 17), 1))
    print("  holiday: 1 business day after Thu 2026-07-02 = %s (skips observed 7/3)"
          % add_business_days(date(2026, 7, 2), 1))
    print("  2026 federal holidays (%d): %s"
          % (len(US_FEDERAL_HOLIDAYS_2026), ", ".join(str(d) for d in sorted(US_FEDERAL_HOLIDAYS_2026))))
