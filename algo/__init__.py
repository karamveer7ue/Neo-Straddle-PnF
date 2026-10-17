"""Algo trading: sells a chart's straddle on a double bottom sell below VWAP and VWMA 10,
books half at a third of the target and the rest at the target, stops out on an X above
the lower of VWAP and VWMA 10, and closes everything at the Exit Time (README.md, Algo
trading). The rules live in core.py with no I/O; runner.py wires them to the live charts
and to Kite (or paper fills), sim.py to the simulator's replays."""
