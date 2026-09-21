"""Fail closed on incompatible native libraries before starting the web service.

Runs offline in CI, Railway's final-image pre-deploy container, and every boot.
Synthetic arithmetic checks ABI loading only, not forecast accuracy. No app,
network, database, model artifact, or issuance code is imported here.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "numpy": "numpy",
    "scipy": "scipy.linalg",
    "pandas": "pandas",
    "scikit-learn": "sklearn.linear_model",
    "xgboost": "xgboost",
    "numba": "numba",
    "llvmlite": "llvmlite.binding",
    "shap": "shap",
    "psycopg2-binary": "psycopg2",
}


def check_python(actual: str, expected: str) -> None:
    if actual != expected:
        raise RuntimeError(f"Python {actual} does not match .python-version {expected}")
    # NumPy 1.26.4 officially supports Python 3.9-3.12, not 3.13.
    if tuple(map(int, actual.split('.')[:2])) != (3, 12):
        raise RuntimeError("The pinned NumPy 1.26 runtime requires Python 3.12")


def check_pins(requirements: str, versions: dict[str, str]) -> None:
    for line in requirements.splitlines():
        if '==' not in line or line.lstrip().startswith('#'):
            continue
        name, want = line.strip().split('==', 1)
        if name in MODULES and versions[name] != want:
            raise RuntimeError(f"{name} {versions[name]} does not match required {want}")


def numerical_smoke(modules: dict) -> None:
    np = modules['numpy']
    matrix = np.array([[3., 1.], [1., 2.]])
    rhs = np.array([9., 8.])
    for solve in (np.linalg.solve, modules['scipy'].solve):
        result = solve(matrix, rhs)
        if not np.allclose(result, [2., 3.]):
            raise RuntimeError("Native linear algebra smoke check failed")
    frame = modules['pandas'].DataFrame(matrix)
    if not np.array_equal(frame.to_numpy(), matrix):
        raise RuntimeError("Pandas/NumPy interchange smoke check failed")
    x = np.array([[0.], [1.], [2.], [3.]])
    y = np.array([0, 0, 1, 1])
    model = modules['scikit-learn'].LogisticRegression(random_state=0).fit(x, y)
    if not np.isfinite(model.predict_proba(x)).all():
        raise RuntimeError("Scikit-learn smoke check failed")
    xgb = modules['xgboost']
    data = xgb.DMatrix(x, label=y, nthread=1)
    booster = xgb.train({'objective': 'binary:logistic', 'nthread': 1, 'seed': 0},
                        data, num_boost_round=1)
    prediction = booster.predict(data)
    if prediction.shape != (4,) or not np.isfinite(prediction).all():
        raise RuntimeError("XGBoost smoke check failed")


def run_checks() -> dict:
    check_python(platform.python_version(), (ROOT / '.python-version').read_text().strip())
    versions = {name: importlib.metadata.version(name) for name in MODULES}
    check_pins((ROOT / 'requirements.txt').read_text(), versions)
    modules = {name: importlib.import_module(module) for name, module in MODULES.items()}
    numerical_smoke(modules)
    return {'status': 'ok', 'python': platform.python_version(),
            'platform': platform.system(), 'libc': platform.libc_ver(),
            'versions': versions, 'accuracy_proven': False}


def main() -> int:
    try:
        report = run_checks()
    except Exception as exc:
        print(f'[RUNTIME_PREFLIGHT] FAILED: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    print('[RUNTIME_PREFLIGHT] ' + json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
