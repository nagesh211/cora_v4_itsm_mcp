"""Deterministic natural-language date-window resolution.

This is a self-contained port of the deterministic core of the project's
DateParserAgent. All application dependencies (autogen, redis, ``app.*``,
``services.*``, ``prompts``, ``ChatRequest``) have been removed; the numeric
date logic is preserved verbatim. There is **no LLM fallback** here — if the
deterministic extractor cannot recognise a phrase, we fall back to the
current-month-to-date window (the same final default the original used) and log
it. A future LLM fallback can slot in at the marked hook in :func:`resolve_dates`.

Defaults (matching the original spec):
  * fiscal_year_start_month = 4   (April FY)
  * week_starts_on          = 6   (Sunday)
  * current_period_to_date  = True

Public API
----------
    resolve_dates("last 3 months") -> {"start_date": "...", "end_date": "..."}
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

from pydantic import BaseModel

from cora_mcp.logging_config import get_logger

log = get_logger(__name__)


# ----------------------
# Utility date helpers
# ----------------------
def first_day_of_month(y: int, m: int) -> date:
    return date(y, m, 1)


def last_day_of_month(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def week_bounds_for(d: date, week_starts_on: int = 0) -> Tuple[date, date]:
    """Return (week_start, week_end) where week_start is week_starts_on (0=Mon..6=Sun)."""
    delta = (d.weekday() - week_starts_on) % 7
    start = d - timedelta(days=delta)
    return start, start + timedelta(days=6)


def quarter_index(month: int, fiscal_start_month: int = 1) -> int:
    shifted = (month - fiscal_start_month) % 12 + 1
    return (shifted - 1) // 3 + 1


def quarter_bounds(d: date, fiscal_start_month: int = 1) -> Tuple[date, date]:
    qi = quarter_index(d.month, fiscal_start_month)
    q_start_month = ((qi - 1) * 3 + fiscal_start_month - 1) % 12 + 1
    q_start_year = d.year if q_start_month <= d.month or fiscal_start_month == 1 else d.year - 1
    start = first_day_of_month(q_start_year, q_start_month)
    end = add_months(start, 3) - timedelta(days=1)
    return start, end


def previous_quarter_bounds(d: date, fiscal_start_month: int = 1) -> Tuple[date, date]:
    start_this_q, _ = quarter_bounds(d, fiscal_start_month)
    prev_q_end = start_this_q - timedelta(days=1)
    return quarter_bounds(prev_q_end, fiscal_start_month)


def _parse_iso(d: str) -> date:
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def bucket_windows(start_date: str, end_date: str, grain: str = "month"):
    """Split an inclusive ``[start_date, end_date]`` window into per-grain buckets.

    Returns ``[(label, (bucket_start, bucket_end)), ...]`` in ascending order,
    each bound a ``YYYY-MM-DD`` date clamped to the requested window. ``grain`` is
    one of ``day`` | ``week`` | ``month`` | ``quarter`` (weeks start Sunday, the
    project default; quarters are calendar quarters).

    Used to build a period-by-period series for **SQL-mode** KPIs: their authored
    query is a scalar over the whole window and cannot ``GROUP BY`` a time bucket,
    so a monthly/quarterly trend is produced by running that query once per
    bucket. Comparative questions ("increase or decrease over the last N months")
    need this breakout — a single window would blend the periods into one number.
    """
    s, e = _parse_iso(start_date), _parse_iso(end_date)
    if e < s:
        s, e = e, s
    g = (grain or "month").lower()
    out = []

    if g == "day":
        cur = s
        while cur <= e:
            iso = cur.isoformat()
            out.append((iso, (iso, iso)))
            cur += timedelta(days=1)
        return out

    if g == "week":
        cur, _ = week_bounds_for(s, 6)               # Sunday-start week
        while cur <= e:
            w_end = cur + timedelta(days=6)
            bs, be = max(cur, s), min(w_end, e)
            out.append((bs.isoformat(), (bs.isoformat(), be.isoformat())))
            cur = w_end + timedelta(days=1)
        return out

    if g == "quarter":
        cur = quarter_bounds(s, 1)[0]
        while cur <= e:
            q_end = add_months(cur, 3) - timedelta(days=1)
            bs, be = max(cur, s), min(q_end, e)
            label = "%d-Q%d" % (cur.year, (cur.month - 1) // 3 + 1)
            out.append((label, (bs.isoformat(), be.isoformat())))
            cur = add_months(cur, 3)
        return out

    # default: month
    cur = first_day_of_month(s.year, s.month)
    while cur <= e:
        m_end = last_day_of_month(cur.year, cur.month)
        bs, be = max(cur, s), min(m_end, e)
        out.append(("%04d-%02d" % (cur.year, cur.month), (bs.isoformat(), be.isoformat())))
        cur = add_months(cur, 1)
    return out


# ----------------------
# Intent & Normalization
# ----------------------
class TimeframeIntent(BaseModel):
    kind: str  # 'relative' | 'absolute' | 'fiscal' | 'rolling'
    grain: Optional[str] = None
    n: Optional[int] = None
    include_current: Optional[bool] = None
    start_expr: Optional[str] = None
    end_expr: Optional[str] = None
    fiscal_year_start_month: int = 1
    to_date: bool = False
    is_future: bool = False
    rest_of: bool = False


class TimeframeNormalizer:
    _DATE_FORMATS = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %Y",
        "%B %Y",
        "%Y-%m",
        "%m/%d/%Y",
    )

    MONTH_RE = (
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    )

    # Patterns to *extract* a date substring from noisy text.
    DATE_SUBSTRING_PATTERNS = [
        re.compile(r"\b\d{4}-\d{2}-\d{2}\b", re.I),               # 2025-06-10
        re.compile(r"\b\d{1,2}-\d{1,2}-\d{4}\b", re.I),           # 10-06-2025
        re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b", re.I),           # 06/10/2025
        re.compile(rf"\b\d{{1,2}}\s+(?:{MONTH_RE})\s+\d{{4}}\b", re.I),   # 10 Aug 2025
        re.compile(rf"\b(?:{MONTH_RE})\s+\d{{1,2}},\s*\d{{4}}\b", re.I),  # Aug 10, 2025
        re.compile(rf"\b(?:{MONTH_RE})\s+\d{{4}}\b", re.I),       # Aug 2025
        re.compile(r"\b\d{4}-\d{2}\b", re.I),                     # 2025-01
    ]

    def __init__(
        self,
        today: Optional[date] = None,
        fiscal_year_start_month: int = 4,
        week_starts_on: int = 6,
        current_period_to_date: bool = True,
    ):
        self.today = today or date.today()
        self.fiscal_year_start_month = fiscal_year_start_month
        self.week_starts_on = week_starts_on
        self.current_period_to_date = current_period_to_date

    # ----- parsing helpers -----
    def _try_parse_date(self, s: str) -> Optional[date]:
        s = s.strip()
        s_norm = re.sub(rf"\b({self.MONTH_RE})[-/]?(\d{{2}})\b", r"\1 \2", s, flags=re.I)

        formats = self._DATE_FORMATS + ("%b %y", "%B %y", "%m/%y", "%y-%m")
        for fmt in formats:
            try:
                dt = datetime.strptime(s_norm, fmt)
                if fmt in ("%b %Y", "%B %Y", "%Y-%m", "%b %y", "%B %y", "%m/%y", "%y-%m"):
                    return date(dt.year, dt.month, 1)
                return dt.date()
            except ValueError:
                continue
        m = re.fullmatch(r"(20\d{2}|19\d{2})", s_norm)
        if m:
            return date(int(m.group(1)), 1, 1)
        return None

    def _month_from_name(self, token: str) -> Optional[int]:
        try:
            return list(calendar.month_name).index(token.capitalize())
        except ValueError:
            try:
                return list(calendar.month_abbr).index(token.capitalize())
            except ValueError:
                return None

    def _parse_month_year_expr(self, expr: str) -> Optional[Tuple[date, date]]:
        tokens = re.split(r"\s+", expr.strip())
        if not tokens:
            return None
        if len(tokens) == 1:
            m = self._month_from_name(tokens[0])
            if m:
                y = self.today.year if m <= self.today.month else self.today.year - 1
                return first_day_of_month(y, m), last_day_of_month(y, m)
        else:
            m = self._month_from_name(tokens[0])
            y = self._try_parse_date(tokens[1])
            if m and y:
                return first_day_of_month(y.year, m), last_day_of_month(y.year, m)
        return None

    def _extract_date_substring(self, text: str) -> Optional[str]:
        for pat in self.DATE_SUBSTRING_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(0)
        return None

    # ----- core -----
    def normalize(self, intent: TimeframeIntent) -> Dict[str, str]:
        try:
            t = self.today
            fy_start = intent.fiscal_year_start_month or self.fiscal_year_start_month

            if intent.kind == "absolute":
                def clean_side(expr: Optional[str]) -> Optional[str]:
                    if not expr:
                        return None
                    sub = self._extract_date_substring(expr)
                    return sub or expr.strip()

                if intent.start_expr and intent.end_expr:
                    s_clean, e_clean = clean_side(intent.start_expr), clean_side(intent.end_expr)

                    start = self._try_parse_date(s_clean) or (self._parse_month_year_expr(s_clean) or (None, None))[0]
                    end = self._try_parse_date(e_clean) or (self._parse_month_year_expr(e_clean) or (None, None))[1]

                    if not start or not end:
                        raise ValueError(
                            f"Unrecognized absolute date(s): {intent.start_expr} to {intent.end_expr}")

                    if end.day == 1 and re.fullmatch(r"\d{4}-\d{2}-01", end.isoformat()):
                        end = last_day_of_month(end.year, end.month)

                    return {"start_date": start.isoformat(), "end_date": end.isoformat()}

                if intent.start_expr and not intent.end_expr:
                    s_clean = clean_side(intent.start_expr)
                    sd = self._try_parse_date(s_clean)
                    if sd:
                        if sd.month == 1 and sd.day == 1 and re.fullmatch(r"\d{4}", s_clean.strip()):
                            if sd.year == self.today.year:
                                return {"start_date": sd.isoformat(), "end_date": self.today.isoformat()}
                            return {"start_date": sd.isoformat(),
                                    "end_date": date(sd.year, 12, 31).isoformat()}
                        if sd.day == 1 and re.fullmatch(r"\d{4}-\d{2}-01", sd.isoformat()):
                            return {"start_date": sd.isoformat(),
                                    "end_date": last_day_of_month(sd.year, sd.month).isoformat()}
                    m_bounds = self._parse_month_year_expr(s_clean)
                    if m_bounds:
                        sd, ed = m_bounds
                        return {"start_date": sd.isoformat(), "end_date": ed.isoformat()}
                    raise ValueError(f"Unrecognized absolute expression: {intent.start_expr}")

            elif intent.kind == "fiscal":
                if intent.grain == "quarter":
                    if intent.include_current:
                        sd, ed = quarter_bounds(t, fy_start)
                        ed = t if intent.to_date or self.current_period_to_date else ed
                        return {"start_date": sd.isoformat(), "end_date": ed.isoformat()}
                    sd, ed = previous_quarter_bounds(t, fy_start)
                    return {"start_date": sd.isoformat(), "end_date": ed.isoformat()}
                if intent.grain == "year":
                    current_fy_start_year = t.year if t.month >= fy_start else t.year - 1
                    fy_start_date = date(current_fy_start_year, fy_start, 1)
                    fy_end_date = add_months(fy_start_date, 12) - timedelta(days=1)
                    if intent.include_current:
                        ed = t if (intent.to_date or self.current_period_to_date) else fy_end_date
                        return {"start_date": fy_start_date.isoformat(), "end_date": ed.isoformat()}
                    prev_fy_end = fy_start_date - timedelta(days=1)
                    prev_fy_start = add_months(prev_fy_end, -11).replace(day=1)
                    return {"start_date": prev_fy_start.isoformat(), "end_date": prev_fy_end.isoformat()}

            elif intent.kind == "relative":
                n = intent.n if (intent.n and intent.n > 0) else 1
                g = intent.grain or "month"
                include_current = bool(intent.include_current)

                # ----- relative single/multi day handling -----
                if g == "day":
                    parsed_start = self._try_parse_date(intent.start_expr.strip()) if intent.start_expr else None
                    parsed_end = self._try_parse_date(intent.end_expr.strip()) if intent.end_expr else None

                    if parsed_start and parsed_end:
                        return {"start_date": parsed_start.isoformat(), "end_date": parsed_end.isoformat()}
                    if parsed_start and not parsed_end:
                        return {"start_date": parsed_start.isoformat(), "end_date": parsed_start.isoformat()}

                    if getattr(intent, "is_future", False):
                        start = t + timedelta(days=1)
                        end = start + timedelta(days=n - 1)
                        return {"start_date": start.isoformat(), "end_date": end.isoformat()}

                    if include_current:
                        end = t
                        start = end - timedelta(days=n - 1)
                    else:
                        end = t - timedelta(days=1)
                        start = end - timedelta(days=n - 1)
                    return {"start_date": start.isoformat(), "end_date": end.isoformat()}

                # ===== REST OF CURRENT PERIOD =====
                if getattr(intent, "rest_of", False):
                    if g == "month":
                        return {"start_date": t.isoformat(),
                                "end_date": last_day_of_month(t.year, t.month).isoformat()}
                    if g == "quarter":
                        _, q_end = quarter_bounds(t, fiscal_start_month=1)
                        return {"start_date": t.isoformat(), "end_date": q_end.isoformat()}
                    if g == "year":
                        return {"start_date": t.isoformat(), "end_date": date(t.year, 12, 31).isoformat()}
                    if g == "week":
                        _, we = week_bounds_for(t, self.week_starts_on)
                        return {"start_date": t.isoformat(), "end_date": we.isoformat()}

                # ===== FUTURE DATES =====
                if getattr(intent, "is_future", False):
                    if g == "month":
                        start_anchor = add_months(date(t.year, t.month, 1), 1)
                        start = start_anchor
                        end = add_months(start_anchor, n - 1)
                        end = last_day_of_month(end.year, end.month)
                        return {"start_date": start.isoformat(), "end_date": end.isoformat()}
                    if g == "quarter":
                        _, curr_q_end = quarter_bounds(t, fiscal_start_month=1)
                        start = curr_q_end + timedelta(days=1)
                        end = add_months(start, 3 * n) - timedelta(days=1)
                        return {"start_date": start.isoformat(), "end_date": end.isoformat()}
                    if g == "year":
                        start = date(t.year + 1, 1, 1)
                        end = date(t.year + n, 12, 31)
                        return {"start_date": start.isoformat(), "end_date": end.isoformat()}
                    if g == "week":
                        _, we = week_bounds_for(t, self.week_starts_on)
                        start = we + timedelta(days=1)
                        end = start + timedelta(days=7 * n - 1)
                        return {"start_date": start.isoformat(), "end_date": end.isoformat()}

                if g == "month":
                    if include_current:
                        start = add_months(date(t.year, t.month, 1), -(n - 1))
                        end = last_day_of_month(t.year, t.month)
                        if intent.to_date or self.current_period_to_date:
                            end = t
                    else:
                        end_month = add_months(date(t.year, t.month, 1), -1)
                        start = add_months(first_day_of_month(end_month.year, end_month.month), -(n - 1))
                        end = last_day_of_month(end_month.year, end_month.month)
                    return {"start_date": start.isoformat(), "end_date": end.isoformat()}

                if g == "week":
                    if include_current:
                        ws, we = week_bounds_for(t, self.week_starts_on)
                        start, end = ws - timedelta(weeks=(n - 1)), we
                        if intent.to_date or self.current_period_to_date:
                            end = t
                    else:
                        prev_week_end = week_bounds_for(t, self.week_starts_on)[0] - timedelta(days=1)
                        start, end = week_bounds_for(prev_week_end, self.week_starts_on)
                        start = start - timedelta(weeks=(n - 1))
                    return {"start_date": start.isoformat(), "end_date": end.isoformat()}

                if g == "year":
                    if include_current:
                        start = date(t.year - (n - 1), 1, 1)
                        end = date(t.year, 12, 31)
                        if intent.to_date or self.current_period_to_date:
                            end = t
                    else:
                        end = date(t.year - 1, 12, 31)
                        start = date(end.year - (n - 1), 1, 1)
                    return {"start_date": start.isoformat(), "end_date": end.isoformat()}

                if g == "quarter":
                    if include_current:
                        sd, ed = quarter_bounds(t, fiscal_start_month=1)
                        start, end = add_months(sd, -3 * (n - 1)), ed
                        if intent.to_date or self.current_period_to_date:
                            end = t
                    else:
                        psd, ped = previous_quarter_bounds(t, fiscal_start_month=1)
                        start, end = add_months(psd, -3 * (n - 1)), ped
                    return {"start_date": start.isoformat(), "end_date": end.isoformat()}

            elif intent.kind == "rolling":
                n = intent.n or 7
                end = t
                start = t - timedelta(days=n - 1)
                return {"start_date": start.isoformat(), "end_date": end.isoformat()}

            raise ValueError(f"Unsupported or incomplete intent: {intent}")
        except Exception as ex:
            log.error("Error in TimeframeNormalizer.normalize: %s for intent %s", ex, intent)
            raise


# ----------------------
# Deterministic extractor
# ----------------------
class DeterministicExtractor:
    def __init__(self, fiscal_year_start_month: int = 4):
        self.fy_start = fiscal_year_start_month

    def extract(self, text: str) -> Optional[TimeframeIntent]:
        s = text.lower().strip()

        # Absolute range with keywords (allow trailing words).
        _START_EXPR = r"(\S+(?:\s+(?!(?:and|to)\s)\S+)*)"
        _END_EXPR = r"(\S+(?:\s+(?!(?:for|in|of|at|with|by|broken|grouped|split)\b)\S+)*)"
        m = re.search(
            r"\b(between|from)\s+" + _START_EXPR + r"\s+(and|to)\s+" + _END_EXPR
            + r"(?:\s+(?:for|in|of|at|with|by|broken|grouped|split)\b[^,.!?]{0,120})?"
            + r"(?:$|[,.!?])",
            s,
        )
        if m:
            start_expr = m.group(2).strip()
            end_expr = m.group(4).strip()
            if self._looks_like_date_expression(start_expr) and self._looks_like_date_expression(end_expr):
                return TimeframeIntent(kind="absolute", start_expr=start_expr, end_expr=end_expr)
            return None

        # Absolute range WITHOUT keywords (compact tokens).
        DATE_TOKEN = (
            r"(?:"
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[-/]?(?:\d{2}|\d{4})"
            r"|(?:\d{4}-\d{2}-\d{2})"
            r"|(?:\d{4}-\d{2})"
            r"|(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
            r")"
        )
        RANGE_SEP = r"(?:-|–|—|through|thru|till|until|~)"
        m = re.search(rf"\b({DATE_TOKEN})\s*{RANGE_SEP}\s*({DATE_TOKEN})\b", s, re.I)
        if m:
            start_expr = m.group(1).strip()
            end_expr = m.group(2).strip()
            if self._looks_like_date_expression(start_expr) and self._looks_like_date_expression(end_expr):
                return TimeframeIntent(kind="absolute", start_expr=start_expr, end_expr=end_expr)
            return None

        # REST / REMAINING / BALANCE OF ...
        m = re.search(r"\b(rest|remaining|balance)\s+of\s+the\s+(month|quarter|year|week)\b", s)
        if m:
            return TimeframeIntent(kind="relative", grain=m.group(2), include_current=True, to_date=False, rest_of=True)
        m = re.search(r"\b(rest|remaining)\s+(?:of\s+)?(?:this\s+)?(month|quarter|year|week)\b", s)
        if m:
            return TimeframeIntent(kind="relative", grain=m.group(2), include_current=True, to_date=False, rest_of=True)

        # To-date shorthands.
        if re.search(r"\b(mtd|month[- ]to[- ]date|this month to date|current month to date|cmtd)\b", s):
            return TimeframeIntent(kind="relative", grain="month", include_current=True, to_date=True)
        if re.search(r"\b(qtd|quarter[- ]to[- ]date|this quarter to date|current quarter to date|cqtd)\b", s):
            return TimeframeIntent(kind="relative", grain="quarter", include_current=True, to_date=True)
        if re.search(r"\b(ytd|year[- ]to[- ]date|this year to date|current year to date|cytd)\b", s):
            return TimeframeIntent(kind="relative", grain="year", include_current=True, to_date=True)

        # THIS <period>.
        m = re.search(r"\bthis\s+(month|quarter|year|week)\b", s)
        if m:
            return TimeframeIntent(kind="relative", grain=m.group(1), include_current=True)

        # NEXT / UPCOMING (future windows).
        m = re.search(r"\bnext\s+(\d+)\s+(months?|quarters?|years?|weeks?)\b", s)
        if m:
            g = m.group(2)
            grain = ("month" if g.startswith("month") else "quarter" if g.startswith("quarter")
                     else "year" if g.startswith("year") else "week")
            return TimeframeIntent(kind="relative", grain=grain, n=int(m.group(1)),
                                   include_current=False, is_future=True)
        m = re.search(r"\b(next|upcoming)\s+(month|quarter|year|week)\b", s)
        if m:
            return TimeframeIntent(kind="relative", grain=m.group(2), n=1, include_current=False, is_future=True)

        # PAST relative windows.
        m = re.search(r"\b(last|past|previous)\s+(\d+)\s+(months?|quarters?|years?|weeks?)\b", s)
        if m:
            g = m.group(3)
            grain = ("month" if g.startswith("month") else "quarter" if g.startswith("quarter")
                     else "year" if g.startswith("year") else "week")
            return TimeframeIntent(kind="relative", grain=grain, n=int(m.group(2)), include_current=False)
        m = re.search(r"\b(\d+)\s+(months?|quarters?|years?|weeks?)\b", s)
        if m:
            g = m.group(2)
            grain = ("month" if g.startswith("month") else "quarter" if g.startswith("quarter")
                     else "year" if g.startswith("year") else "week")
            return TimeframeIntent(kind="relative", grain=grain, n=int(m.group(1)), include_current=True)
        m = re.search(r"\blast\s+(week|month|quarter|year)\b", s)
        if m:
            return TimeframeIntent(kind="relative", grain=m.group(1), n=1, include_current=False)
        if re.search(r"\bprevious\s+quarter\b", s):
            return TimeframeIntent(kind="relative", grain="quarter", n=1, include_current=False)
        m = re.search(r"\blast\s+(\d+)\s+days\b", s)
        if m:
            return TimeframeIntent(kind="rolling", n=int(m.group(1)))

        # Absolute month tokens.
        m = re.search(
            r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
            r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b(\s+\d{4})?",
            s)
        if m:
            return TimeframeIntent(kind="absolute", start_expr=m.group(0).strip())
        # Month + 2-digit year (jan25, jan-25, jan/25).
        m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[-/]?(\d{2})\b", s)
        if m:
            return TimeframeIntent(kind="absolute", start_expr=f"{m.group(1)} {m.group(2)}".strip())

        # Bare year.
        m = re.search(r"\b(20\d{2}|19\d{2})\b", s)
        if m:
            return TimeframeIntent(kind="absolute", start_expr=m.group(1))

        # Fiscal phrases & FYyyyy.
        if re.search(r"\bthis\s+fiscal\s+quarter\b", s):
            return TimeframeIntent(kind="fiscal", grain="quarter", include_current=True,
                                   fiscal_year_start_month=self.fy_start)
        if re.search(r"\bprevious\s+fiscal\s+quarter\b", s):
            return TimeframeIntent(kind="fiscal", grain="quarter", include_current=False,
                                   fiscal_year_start_month=self.fy_start)
        if re.search(r"\bthis\s+fiscal\s+year\b", s):
            return TimeframeIntent(kind="fiscal", grain="year", include_current=True,
                                   fiscal_year_start_month=self.fy_start)
        if re.search(r"\bprevious\s+fiscal\s+year\b", s):
            return TimeframeIntent(kind="fiscal", grain="year", include_current=False,
                                   fiscal_year_start_month=self.fy_start)
        m = re.search(r"\bfy[- ]?(\d{4})\b", s)
        if m:
            y = int(m.group(1))
            start = date(y, self.fy_start, 1)
            end = add_months(start, 12) - timedelta(days=1)
            return TimeframeIntent(kind="absolute", start_expr=start.isoformat(), end_expr=end.isoformat())

        return None

    def _looks_like_date_expression(self, expr: str) -> bool:
        if not expr:
            return False
        date_patterns = [
            r"\d{4}-\d{2}-\d{2}",
            r"\d{4}-\d{2}",
            r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{2,4}\b",
            r"\b(?:20\d{2}|19\d{2})\b",
        ]
        for pattern in date_patterns:
            if re.search(pattern, expr, re.I):
                return True
        # Words that indicate this is not a date expression.
        non_date_words = ["module", "business", "service", "cost", "spend", "data", "report",
                          "analysis", "breakdown", "centers", "products", "top", "optix",
                          "for", "by", "the"]
        expr_lower = expr.lower()
        for word in non_date_words:
            if word in expr_lower:
                return False
        return False


# ----------------------
# Public API
# ----------------------
def resolve_dates(
    phrase: str,
    today: Optional[date] = None,
    fiscal_year_start_month: int = 4,
    week_starts_on: int = 6,
    current_period_to_date: bool = True,
) -> Dict[str, str]:
    """Resolve a natural-language time phrase into an inclusive date window.

    Returns ``{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
    "matched": bool, "phrase": <input>}``.

    Deterministic only. If nothing matches, falls back to current-month-to-date
    (the original agent's final default) and sets ``matched=False``.
    """
    today = today or date.today()
    det = DeterministicExtractor(fiscal_year_start_month=fiscal_year_start_month)
    norm = TimeframeNormalizer(
        today=today,
        fiscal_year_start_month=fiscal_year_start_month,
        week_starts_on=week_starts_on,
        current_period_to_date=current_period_to_date,
    )

    intent = det.extract(phrase or "")
    matched = intent is not None
    if intent is None:
        # TODO: optional LLM fallback hook — structure an unusual phrase into a
        # TimeframeIntent here, then let norm.normalize() compute the dates.
        log.info("date_resolver: no deterministic match for %r; defaulting to month-to-date", phrase)
        intent = TimeframeIntent(kind="relative", grain="month", include_current=True, to_date=True)

    window = norm.normalize(intent)
    result = {**window, "matched": matched, "phrase": phrase}
    log.debug("date_resolver: %r -> %s (matched=%s)", phrase, window, matched)
    return result


if __name__ == "__main__":  # quick manual check
    for p in ["last 3 months", "2 months", "this month", "mtd", "ytd",
              "between 2025-06-10 and 2025-08-15", "Aug 2025", "fy2024",
              "last 30 days", "rest of the year"]:
        print(f"{p:40s} -> {resolve_dates(p)}")
