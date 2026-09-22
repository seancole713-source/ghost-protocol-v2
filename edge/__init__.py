"""edge -- an independent forecasting system built beside Ghost, not from it.

Design rule: nothing in this package imports Ghost's prediction engine, gates,
models or calibration (core/signal_engine, core/precision_gate, ...). It shares
only hosting and the database. Ghost's ten months showed that machinery
designed to judge a pick is worthless without the data to find one, so edge is
built data-first:

    providers/  what each data source can actually see (and a probe that proves it)
    contracts   what a forecast IS, stated before its window opens
    ledger      the frozen forward record -- specs are hashed, edits are refused
    resolver    target-before-stop outcomes from minute bars, ambiguity explicit
    detectors   named, measurable setup components
    miss_audit  every +5% mover, and why the system did or didn't catch it
    stats       Wilson intervals, expectancy, calibration -- the only judges

Nothing here trades. Nothing here is decision-eligible for Ghost.
"""
