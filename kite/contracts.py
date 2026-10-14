"""Kite option contracts for the charted underlyings.

The charts name a straddle by underlying, expiry and strike; Kite trades it
by exchange and tradingsymbol. Both come from Kite's instrument dump, which
is fetched once a day per exchange and indexed here. Nothing in this module
talks to the network.
"""

from dataclasses import dataclass
from datetime import date

from pnf.series import UNDERLYINGS, Underlying

# Kite's exchange for each Neo F&O segment.
KITE_EXCHANGE = {"nse_fo": "NFO", "bse_fo": "BFO"}


def kite_exchange(u: Underlying) -> str:
    return KITE_EXCHANGE[u.fo_segment]


@dataclass(frozen=True)
class Contract:
    exchange: str  # "NFO"
    tradingsymbol: str  # "NIFTY26O2025000CE"
    instrument_token: int
    underlying: str  # an UNDERLYINGS key, "NIFTY"
    expiry: date
    strike: int
    kind: str  # "CE" | "PE"
    lot_size: int

    @property
    def key(self) -> str:
        """Kite's "EXCHANGE:TRADINGSYMBOL" instrument name."""
        return f"{self.exchange}:{self.tradingsymbol}"


def group_key(underlying: str, expiry: date, strike: int) -> str:
    """Identifies one straddle (its CE and PE) in browser messages: "NIFTY|2026-10-20|25000"."""
    return f"{underlying}|{expiry.isoformat()}|{strike}"


class ContractBook:
    """Index-option contracts of the charted underlyings, from Kite's instrument dump."""

    def __init__(self, rows_by_exchange: dict[str, list[dict]], loaded_on: date):
        self.loaded_on = loaded_on
        self._by_name: dict[tuple, Contract] = {}
        self._by_key: dict[str, Contract] = {}
        names = {kite_exchange(u): {} for u in UNDERLYINGS.values()}
        for u in UNDERLYINGS.values():
            names[kite_exchange(u)][u.key] = u
        for exchange, rows in rows_by_exchange.items():
            wanted = names.get(exchange, {})
            for row in rows:
                u = wanted.get(row.get("name"))
                kind = row.get("instrument_type")
                if not u or kind not in ("CE", "PE") or not isinstance(row.get("expiry"), date):
                    continue
                strike = float(row["strike"])
                if strike != int(strike):
                    continue  # the charts use whole-number strikes only
                c = Contract(
                    exchange=exchange,
                    tradingsymbol=row["tradingsymbol"],
                    instrument_token=int(row["instrument_token"]),
                    underlying=u.key,
                    expiry=row["expiry"],
                    strike=int(strike),
                    kind=kind,
                    lot_size=int(row["lot_size"]),
                )
                self._by_name[(c.underlying, c.expiry, c.strike, c.kind)] = c
                self._by_key[c.key] = c

    def __len__(self) -> int:
        return len(self._by_key)

    def option(self, underlying: str, expiry: date, strike: int, kind: str) -> Contract | None:
        return self._by_name.get((underlying, expiry, strike, kind))

    def straddle(self, underlying: str, expiry: date, strike: int) -> tuple[Contract, Contract] | None:
        ce, pe = (self.option(underlying, expiry, strike, k) for k in ("CE", "PE"))
        return (ce, pe) if ce and pe else None

    def by_key(self, key: str) -> Contract | None:
        """Look up by "EXCHANGE:TRADINGSYMBOL"."""
        return self._by_key.get(key)

    def lot_sizes(self) -> dict[str, int]:
        """Lot size per "<underlying>|<expiry>", for the quantity fields in the browser.

        Lot sizes are revised from time to time, and older contracts keep the old
        one, so the size belongs to an expiry, not to the underlying.
        """
        sizes = {}
        for c in self._by_key.values():
            sizes[f"{c.underlying}|{c.expiry.isoformat()}"] = c.lot_size
        return dict(sorted(sizes.items()))
