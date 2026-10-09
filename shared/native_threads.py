"""Cap native math-library thread pools before numpy/xgboost/sklearn load.

libgomp, OpenBLAS and MKL size their pools from the host's CPU count, not the
container's share. On Railway that let one model call try to start dozens of
OpenMP threads; on 10/7 and 10/9 it failed with "libgomp: Thread creation
failed" and the whole process died. Must run before the first numpy import.
An operator-set value always wins (setdefault).
"""
from __future__ import annotations

import os
from typing import MutableMapping

THREAD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)
DEFAULT_THREADS = "2"


def cap_native_threads(env: MutableMapping[str, str] | None = None,
                       default: str = DEFAULT_THREADS) -> dict[str, str]:
    env = os.environ if env is None else env
    for name in THREAD_VARS:
        env.setdefault(name, default)
    return {name: env[name] for name in THREAD_VARS}
