"""Event-day awareness for the high-conviction screener.

Dealer gamma/vanna pinning is a mean-reversion signal that assumes no exogenous
catalyst. Around a binary event — earnings, a product keynote (e.g. Apple's WWDC),
an FDA decision, an analyst day — the underlying gaps and the front-expiry implied
vol crushes, so the gamma structure that normally magnetises price is overwhelmed.
A 0-2 DTE pick whose horizon spans such an event is therefore unreliable and must
not be surfaced as high conviction (this is exactly why an AAPL call printed into
the 2026-06-08 WWDC keynote went the wrong way while its next *earnings* sat weeks
out on 2026-07-30).

This module answers one question: "does a known event fall on/before ``expiry`` and
on/after ``today`` for ``ticker``?" from two sources:

* a MANUAL config map (``event_dates``: ticker -> [ISO date, ...]) — the source of
  truth for the non-earnings catalysts (WWDC, FDA, splits, analyst days) that plain
  earnings calendars miss; and
* AUTOMATIC earnings via yfinance ``Ticker(t).calendar['Earnings Date']`` — best
  effort, cached, and never fatal. ``get_earnings_dates()`` is intentionally NOT used
  (it needs ``lxml``, which is not a project dependency); ``.calendar`` carries the
  next scheduled date with no extra deps.

Resolution is deliberately DATE-LEVEL, not intraday: a contract is flagged whenever a
known event *date* lands in ``[today, expiry-date]`` inclusive — including a same-day
0DTE on the event date itself. Time-of-day is not reliably available (yfinance drops it
from ``.calendar``, and manual entries are dates), and the catalysts that actually
break pinning are mostly intraday or before-open (keynotes, BMO earnings, FDA), so a
same-day expiry usually *does* straddle the move. We therefore err toward caution:
flagging only removes a name from *high conviction* (it still surfaces as a tagged
caution candidate), so a rare false positive is far cheaper than printing a
high-conviction pick into an event gap — the exact failure this guard exists to stop.

The manual path is pure-stdlib so the module stays importable — and unit-testable —
without pandas/yfinance installed.
"""
from datetime import date, datetime


def _to_date(x):
    """Best-effort coerce a date / datetime / pandas Timestamp / ISO string to ``date``.

    Returns ``None`` for anything unparseable (including pandas ``NaT``, whose ``str``
    is ``"NaT"``), so callers can simply skip falsy results.
    """
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    # pandas.Timestamp (and similar) expose a .date() method.
    meth = getattr(x, "date", None)
    if callable(meth):
        try:
            val = meth()
            if isinstance(val, date):
                return val
        except Exception:
            pass
    s = str(x).strip()
    if not s or s.lower() in ("nat", "nan", "none"):
        return None
    s = s.split("T")[0].split(" ")[0]  # drop any time component
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_manual(ticker, manual_map):
    out = []
    for raw in (manual_map or {}).get(ticker, []) or []:
        d = _to_date(raw)
        if d:
            out.append((d, "event"))
    return out


def _earnings_dates(ticker, _cache={}):
    """Upcoming earnings date(s) for ``ticker`` via yfinance ``.calendar``.

    Best-effort: any failure (no network, odd shape, missing key) yields ``[]``.
    Cached per process so a ticker is only hit once per run.
    """
    if ticker in _cache:
        return _cache[ticker]
    dates = []
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar or {}
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if raw is None:
            raw = []
        elif not isinstance(raw, (list, tuple)):
            raw = [raw]
        for x in raw:
            d = _to_date(x)
            if d:
                dates.append(d)
    except Exception:
        dates = []
    _cache[ticker] = dates
    return dates


def ticker_event_dates(ticker, manual_map=None, enable_earnings=True):
    """All known event dates for ``ticker`` as ``(date, label)`` tuples.

    Manual catalysts are labelled ``"event"``; auto-detected ones ``"earnings"``.
    """
    events = _parse_manual(ticker, manual_map)
    if enable_earnings:
        events += [(d, "earnings") for d in _earnings_dates(ticker)]
    return events


def event_in_window(events, start, end):
    """Does any ``(date, label)`` in ``events`` fall within ``[start, end]`` inclusive?

    Returns ``(True, label)`` for the earliest matching event, else ``(False, None)``.
    ``start``/``end`` are ``date`` objects (today and the contract's expiry date); they
    are swapped if passed out of order. The check is intentionally date-level — a
    same-day event date equal to ``start``/``end`` counts (see module docstring for the
    deliberately conservative rationale).
    """
    if start and end and start > end:
        start, end = end, start
    hits = sorted((d, lbl) for d, lbl in events
                  if d and start <= d <= end)
    if hits:
        return True, hits[0][1]
    return False, None
