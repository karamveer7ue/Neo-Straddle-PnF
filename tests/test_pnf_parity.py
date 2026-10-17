"""pnf/pnf.py (the algo's chart maths) must agree exactly with static/pnf.js (the chart's).

Both are run on the same series - random walks over several sessions, with gaps,
zero and missing volumes, prices on exact box boundaries and other reversal and
VWMA settings - and every column, box, signal, VWAP and VWMA must be identical,
float for float.
"""

import json
import random
import shutil
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from pnf.pnf import Pnf, PnfBuilder, build_pnf, tip_of
from pnf.series import IST

NODE = shutil.which("node")
DUMP = Path(__file__).with_name("pnf_dump.mjs")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is needed to run static/pnf.js")


def as_json(chart: Pnf | None, bars: list) -> dict | None:
    """The shape tests/pnf_dump.mjs prints."""
    if chart is None:
        return None
    return {
        "p0": chart.p0,
        "box": chart.box,
        "hi": chart.hi,
        "lo": chart.lo,
        "columns": [
            {
                "dir": c.dir,
                "boxes": [[b.row, b.ts] for b in c.boxes],
                "hi": c.hi,
                "lo": c.lo,
                "startTs": c.start_ts,
                "endTs": c.end_ts,
                "volume": c.volume,
            }
            for c in chart.columns
        ],
        "signals": [[s.type, s.col, s.row, s.level, s.ts] for s in chart.signals],
        "vwma": chart.vwma,
        "vwap": chart.vwap,
        "vwapAt": [chart.vwap_at(b[0]) for b in bars],
    }


def session_minutes(first: date, days: int):
    """Epoch seconds of every session minute (09:15-15:29) of `days` weekdays from `first`."""
    day = first
    for _ in range(days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        start = datetime.combine(day, time(9, 15), IST)
        for m in range(375):
            yield int((start + timedelta(minutes=m)).timestamp())
        day += timedelta(days=1)


def random_case(rng: random.Random) -> dict:
    days = rng.randint(1, 4)
    minutes = list(session_minutes(date(2026, 10, 5), days))
    minutes = minutes[: rng.randint(1, len(minutes))]
    if rng.random() < 0.3:  # an illiquid straddle skips minutes
        minutes = [ts for ts in minutes if rng.random() < 0.7] or minutes[:1]
    price = rng.choice([200.0, 87.35, 153.2, 412.9, 1234.5, 31.05])
    on_grid = rng.random() < 0.25  # closes exactly on box boundaries (p0 = 200 gives a box of 1.00)
    if on_grid:
        price = 200.0
    vol = rng.choice([0.002, 0.006, 0.015])
    bars = []
    for ts in minutes:
        if on_grid:
            price = max(1.0, price + rng.choice([-3, -2, -1, 0, 1, 2, 3]))
        else:
            price = max(0.05, round(price * (1 + rng.gauss(0, vol)) + rng.choice([0, 0, 0, 0.35, -0.35]), 2))
        volume = rng.choice([0, 0, rng.randint(1, 5000), rng.randint(1, 200_000), None])
        bars.append([ts, price, volume] if volume is not None or rng.random() < 0.5 else [ts, price])
    opts = {}
    if rng.random() < 0.3:
        opts = {"reversal": rng.choice([1, 2, 3, 4]), "vwmaPeriod": rng.choice([3, 5, 10, 20])}
    if rng.random() < 0.15:
        opts["boxPct"] = rng.choice([0.25, 0.5, 1.0])
    return {"bars": bars, "opts": opts}


def run_js(cases: list[dict]) -> list:
    out = subprocess.run(
        [NODE, str(DUMP)], input=json.dumps({"cases": cases}), capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def run_py(cases: list[dict]) -> list:
    keys = {"boxPct": "box_pct", "reversal": "reversal", "vwmaPeriod": "vwma_period"}
    return [
        as_json(build_pnf(c["bars"], **{keys[k]: v for k, v in c["opts"].items()}), c["bars"]) for c in cases
    ]


def test_python_and_javascript_charts_are_identical():
    rng = random.Random(20261016)
    cases = [random_case(rng) for _ in range(250)]
    # Edge cases: nothing, a single bar, a zero first close (no box), and no full box move.
    cases += [
        {"bars": [], "opts": {}},
        {"bars": [[1_791_000_000, 150.0, 10]], "opts": {}},
        {"bars": [[1_791_000_000, 0.0, 10], [1_791_000_060, 5.0, 10]], "opts": {}},
        {"bars": [[1_791_000_000, 200.0, 10], [1_791_000_060, 200.9, 10]], "opts": {}},
    ]
    js, py = run_js(cases), run_py(cases)
    assert len(js) == len(py) == len(cases)
    signals = sum(len(r["signals"]) for r in js if r)
    assert signals > 500  # the random cases really exercise signal detection
    for i, (a, b) in enumerate(zip(js, py, strict=True)):
        assert a == b, f"case {i} differs (bars: {cases[i]['bars'][:5]}..., opts: {cases[i]['opts']})"


def test_the_newest_column_read_bar_by_bar_matches_the_whole_chart():
    """The simulator's algo reads the chart bar by bar (PnfBuilder.tip); the live one
    reads the whole chart rebuilt every minute (tip_of). Both must see the same."""
    rng = random.Random(7)
    keys = {"boxPct": "box_pct", "reversal": "reversal", "vwmaPeriod": "vwma_period"}
    checked = sells = 0
    for _ in range(60):
        case = random_case(rng)
        bars = case["bars"][:300]
        opts = {keys[k]: v for k, v in case["opts"].items()}
        builder = PnfBuilder(bars[0][1], **opts)
        for i, bar in enumerate(bars):
            builder.add(bar)
            tip = builder.tip(bar[0])
            assert tip == tip_of(build_pnf(bars[: i + 1], **opts), bar[0]), f"bar {i}: {bars[: i + 1][-3:]}"
            checked += 1
            sells += tip is not None and tip.sell is not None
    assert checked > 5000 and sells > 50


def test_the_same_documented_examples_hold_in_python():
    """A few of tests/pnf.test.mjs's hand-checked cases, so a failure here is easy to read."""

    def bars(*closes):
        return [[i, c] for i, c in enumerate(closes)]

    def shape(chart):
        return [f"{c.dir}{','.join(str(b.row) for b in c.boxes)}" for c in chart.columns]

    assert build_pnf([]) is None
    assert shape(build_pnf(bars(200, 205, 202))) == ["X1,2,3,4,5", "O4,3,2"]
    assert shape(build_pnf(bars(200, 205, 201, 204))) == ["X1,2,3,4,5", "O4,3,2,1", "X2,3,4"]
    assert shape(build_pnf(bars(200, 200.1 + 202.9 - 200.0))) == ["X1,2,3"]  # float drift at 203
    breakout = build_pnf(bars(200, 205, 202, 205, 206.2))
    assert [(s.type, s.col, s.row, s.level, s.ts) for s in breakout.signals] == [("buy", 2, 6, 5, 4)]
