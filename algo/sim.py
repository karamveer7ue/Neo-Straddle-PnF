"""The algo over a simulator run (pnf_sim): the live page's rules (core.py) and chart
maths (pnf/pnf.py) on past minutes, with paper fills.

The run is played as the simulator page replays it. Within each minute, the price
steps the page plays (open, the two extremes, close) are the live prices the loss
limits watch; at the minute's close the 1-minute rules run on the chart as the page
draws it, the context day included. The algo trades from `start_at` (when it was
switched On in the replay); at each later session's open it switches itself On
again (a replay has nobody to do it), and it goes Off at the Exit Time as on the
live page.

The page shows the run as of any moment of the replay, scrubbing back included, so
every trade comes with `frames` (its state each time it changed) and `stops` (the
level its stop stood at, each minute), and the algo with `status` frames.
"""

import dataclasses
from datetime import date, datetime, timedelta

from pnf.pnf import PnfBuilder
from pnf.series import IST

from .core import PAPER, AlgoCore, AlgoError, ChartView, Settings, trade_json


def leg_keys(chart_key: str) -> tuple[str, str]:
    """The simulator's stand-ins for the Neo leg keys (its page prices them by these)."""
    return f"sim|{chart_key}|CE", f"sim|{chart_key}|PE"


def simulate(run: dict, settings: Settings, start_at: int | None = None) -> dict:
    """Every algo trade of `run` (a pnf_sim.replay.build_run result) with `settings`."""
    settings = dataclasses.replace(settings, mode=PAPER)
    core = AlgoCore(settings)
    underlying = run["underlying"]["key"]
    expiry = date.fromisoformat(run["expiry"])
    lot = run["lotSize"]
    steps = run["pathSteps"]
    begin = max(run["replayStart"], start_at or 0)

    charts = []
    for c in run["charts"]:
        if c.get("error") or not c["series"]:
            continue
        ce_key, pe_key = leg_keys(c["key"])
        builder = PnfBuilder(c["series"][0][1])
        rows = {}
        for row in c["series"]:
            if row[0] < run["replayStart"]:
                builder.add(row[:3])  # the context day: drawn, not replayed
            else:
                rows[row[0]] = row
        charts.append(
            {
                "def": c,
                "ce": {"key": ce_key, "symbol": c.get("ce", "")},
                "pe": {"key": pe_key, "symbol": c.get("pe", "")},
                "builder": builder,
                "rows": rows,
            }
        )
    minutes = sorted({ts for c in charts for ts in c["rows"]})

    prices: dict[str, float] = {}
    trades: dict[str, dict] = {}  # trade id -> {"frames": [...], "stops": [...]}
    status: list[dict] = []
    events: list[dict] = []

    def carry_out(intents: list, at: datetime, close: bool = False) -> None:
        for intent in intents:
            core.fill_paper(intent, prices, at)
        if intents or core.events or close:  # nothing else changes a trade or the algo
            record(at)

    def record(at: datetime) -> None:
        epoch = int(at.timestamp())
        for ev in core.take_events():
            ev["title"] = ev["title"].replace(" (paper)", "")
            events.append(ev)
        for t in core.trades:
            entry = trades.setdefault(t.id, {"frames": [], "stops": []})
            if entry["frames"] and not entry["frames"][-1]["view"]["active"]:
                continue  # finished: it changes no more
            view = trade_json(t)
            del view["pnl"]  # at the last prices: the page prices it at the replay's own
            stop = view.pop("stop")
            if not entry["frames"] or entry["frames"][-1]["view"] != view:
                entry["frames"].append({"at": epoch, "view": view})
            level = stop["level"] if stop else None
            if not entry["stops"] or entry["stops"][-1][1] != level:
                entry["stops"].append([epoch, level])
        now = {
            "on": core.on,
            "halted": core.halted,
            "exitTimeDone": core.exit_time_done,
            "day": core.day.isoformat() if core.day else None,
            "entries": len(core.todays()),
        }
        if not status or {k: v for k, v in status[-1].items() if k != "at"} != now:
            status.append({"at": epoch, **now})

    day = None
    for ts in minutes:
        start = datetime.fromtimestamp(ts, IST)
        if start.date() != day:
            day = start.date()
            core.on_clock(start, prices)  # a new session: the day's counters start afresh
        if not core.on and ts >= begin and not core.exit_time_done and not core.halted:
            try:
                core.switch_on(start)
            except AlgoError:
                pass  # e.g. past the Last entry time: Off until the next session
        if core.events:
            record(start)
        for k in range(steps):
            at = start + timedelta(seconds=60 * k / steps)
            for c in charts:
                row = c["rows"].get(ts)
                if row:
                    _, prices[c["ce"]["key"]], prices[c["pe"]["key"]] = row[5][k]
            carry_out(core.on_prices(at, prices) + core.on_clock(at, prices), at)
        close_at = start + timedelta(minutes=1)
        views = []
        for c in charts:
            row = c["rows"].get(ts)
            d = c["def"]
            if row:
                c["builder"].add(row[:3])
                prices[c["ce"]["key"]], prices[c["pe"]["key"]] = row[3], row[4]
            views.append(
                ChartView(
                    key=d["key"],
                    offset=d["offset"],
                    strike=d["strike"],
                    underlying=underlying,
                    expiry=expiry,
                    ce=c["ce"],
                    pe=c["pe"],
                    lot_size=lot,
                    bar=tuple(row[:3]) if row else None,
                    tip=c["builder"].tip(ts) if row else None,
                )
            )
        carry_out(core.on_minute(close_at, views, prices), close_at)
        carry_out(core.on_prices(close_at, prices) + core.on_clock(close_at, prices), close_at, close=True)

    closed = [f["frames"][-1]["view"] for f in trades.values()]
    return {
        "settings": settings.to_json(),
        "startAt": begin,
        "trades": [{"id": tid, **f} for tid, f in trades.items()],
        "status": status,
        "events": events,
        "total": {
            "trades": len(closed),
            "won": sum(1 for v in closed if not v["active"] and v["booked"] > 0),
            "lost": sum(1 for v in closed if not v["active"] and v["booked"] < 0),
            "booked": round(sum(v["booked"] for v in closed), 2),
        },
    }
