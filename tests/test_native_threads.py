from pathlib import Path

from shared.native_threads import THREAD_VARS, cap_native_threads

ROOT = Path(__file__).resolve().parents[1]


def test_caps_every_pool_when_unset():
    env = {}
    caps = cap_native_threads(env)
    assert caps == {name: "2" for name in THREAD_VARS}
    assert env == caps


def test_operator_value_wins():
    env = {"OMP_NUM_THREADS": "4"}
    cap_native_threads(env)
    assert env["OMP_NUM_THREADS"] == "4"
    assert env["OPENBLAS_NUM_THREADS"] == "2"


def test_wolf_app_caps_before_first_third_party_import():
    lines = (ROOT / "wolf_app.py").read_text().splitlines()
    cap = next(i for i, l in enumerate(lines) if l.startswith("_cap_native_threads()"))
    first_pkg = next(i for i, l in enumerate(lines) if l.startswith("import config.symbols"))
    assert cap < first_pkg
    stdlib = {"collections", "hmac", "json", "logging", "os", "sys", "threading", "time"}
    for line in lines[:cap]:
        if line.startswith("import "):
            assert line.split()[1] in stdlib, line
