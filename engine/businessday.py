#!/usr/bin/env python3
"""businessday.py — shared business-day date math for the AMBS Merchandise Returns Engine.

Stdlib only. The single source of truth for "N business days" SLA/deadline math, so the
calendar-vs-business-day bug from the PPG incident (MER-3) cannot recur per-case. Before
this, the SLA math lived ad-hoc in the ladder/case_tick and one path treated a "7 business
day" promise as 7 CALENDAR days — which would have fired an escalation on 2026-07-24 when
the letter's real deadline (7 business days from Fri 2026-07-17) is end of Tue 2026-07-28.

A "business day" here = Monday–Friday and NOT a US federal holiday (observed-day adjusted).

Public API:
    is_business_day(d) -> bool
    add_business_days(start_date, n) -> date        # n business days on/after start (n>=0 walks forward)
    business_day_deadline(start_date, n) -> date     # the date that is n business days AFTER start_date
    US_FEDERAL_HOLIDAYS_2026  -> frozenset[date]
"""
from datetime import date, timedelta


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


def _us_federal_holidays(year):
    """The observed dates of the 11 US federal holidays for a given year."""
    holidays = {
        _observed(date(year, 1, 1)),          # New Year's Day
        _nth_weekday(year, 1, 0, 3),          # MLK Jr. Day — 3rd Mon Jan
        _nth_weekday(year, 2, 0, 3),          # Washington's Birthday — 3rd Mon Feb
        _last_weekday(year, 5, 0),            # Memorial Day — last Mon May
        _observed(date(year, 6, 19)),         # Juneteenth
        _observed(date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),          # Labor Day — 1st Mon Sep
        _nth_weekday(year, 10, 0, 2),         # Columbus / Indigenous Peoples' — 2nd Mon Oct
        _observed(date(year, 11, 11)),        # Veterans Day
        _nth_weekday(year, 11, 3, 4),         # Thanksgiving — 4th Thu Nov
        _observed(date(year, 12, 25)),        # Christmas Day
    }
    return frozenset(holidays)


# Explicit 2026 set (the engine's operating year) — usable as a constant.
US_FEDERAL_HOLIDAYS_2026 = _us_federal_holidays(2026)


def _holidays_for(d):
    """Holiday set for the year of d (falls back to the 2026 constant for 2026)."""
    if d.year == 2026:
        return US_FEDERAL_HOLIDAYS_2026
    return _us_federal_holidays(d.year)


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

    print("OK — all businessday.py self-tests passed")
    print("  PPG: 7 business days after Fri 2026-07-17 = %s (calendar +7 would be %s)"
          % (business_day_deadline(start, 7), start + timedelta(days=7)))
    print("  weekend: 1 business day after Fri 2026-07-17 = %s" % add_business_days(date(2026, 7, 17), 1))
    print("  holiday: 1 business day after Thu 2026-07-02 = %s (skips observed 7/3)"
          % add_business_days(date(2026, 7, 2), 1))
    print("  2026 federal holidays (%d): %s"
          % (len(US_FEDERAL_HOLIDAYS_2026), ", ".join(str(d) for d in sorted(US_FEDERAL_HOLIDAYS_2026))))
