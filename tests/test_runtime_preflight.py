"""Release/runtime parity: reject the ABI failure that passed earlier unit CI."""
import json
from pathlib import Path

import pytest

from scripts import runtime_preflight as preflight

ROOT = Path(__file__).resolve().parents[1]


def test_supported_python_passes():
    preflight.check_python('3.12.14', '3.12.14')


@pytest.mark.parametrize('actual,expected', [
    ('3.13.13', '3.13.13'), ('3.13.13', '3.12.14'), ('3.12.13', '3.12.14'),
])
def test_unsupported_or_drifted_python_fails(actual, expected):
    with pytest.raises(RuntimeError):
        preflight.check_python(actual, expected)


def test_exact_native_pins_checked():
    preflight.check_pins('numpy==1.26.4\n', {'numpy': '1.26.4'})
    with pytest.raises(RuntimeError, match='numpy'):
        preflight.check_pins('numpy==1.26.4\n', {'numpy': '2.0.0'})


@pytest.mark.parametrize('error', [ImportError('GLIBC_2.38 not found'),
                                 RuntimeError('native arithmetic failed')])
def test_preflight_failure_exits_nonzero(monkeypatch, capsys, error):
    def fail():
        raise error
    monkeypatch.setattr(preflight, 'run_checks', fail)
    assert preflight.main() == 1
    assert str(error) in capsys.readouterr().err


def test_preflight_success_does_not_claim_accuracy(monkeypatch, capsys):
    monkeypatch.setattr(preflight, 'run_checks', lambda: {'status': 'ok', 'accuracy_proven': False})
    assert preflight.main() == 0
    assert '"accuracy_proven": false' in capsys.readouterr().out


def test_native_failure_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(preflight, 'check_python', lambda *args: None)
    monkeypatch.setattr(preflight, 'check_pins', lambda *args: None)
    monkeypatch.setattr(preflight.importlib.metadata, 'version', lambda _: 'test')
    def fail(_):
        raise ImportError('GLIBC_2.38 not found')
    monkeypatch.setattr(preflight.importlib, 'import_module', fail)
    with pytest.raises(ImportError, match='GLIBC'):
        preflight.run_checks()


def test_ci_and_runtime_use_one_python_pin():
    pin = (ROOT / '.python-version').read_text().strip()
    assert pin.startswith('3.12.')
    assert (ROOT / 'runtime.txt').read_text().strip() == 'python-' + pin
    for file in ['ci.yml', 'integration.yml']:
        workflow = (ROOT / '.github/workflows' / file).read_text()
        assert 'python-version:' not in workflow
        assert 'python-version-file: ".python-version"' in workflow
    assert 'python scripts/runtime_preflight.py' in (ROOT / '.github/workflows/ci.yml').read_text()


def test_final_image_and_each_boot_require_preflight():
    deploy = json.loads((ROOT / 'railway.json').read_text())['deploy']
    assert deploy['preDeployCommand'] == ['python scripts/runtime_preflight.py --pre-deploy']
    assert deploy['healthcheckPath'] == '/health'
    # Measured DB schema startup took 118s, before Uvicorn could bind.
    assert 180 <= deploy['healthcheckTimeout'] <= 300
    procfile = (ROOT / 'Procfile').read_text()
    assert 'python scripts/runtime_preflight.py && exec python -m uvicorn' in procfile


def test_critical_native_dependencies_never_build_from_source():
    line = next(line for line in (ROOT / 'requirements.txt').read_text().splitlines()
                if line.startswith('--only-binary='))
    assert set(preflight.MODULES) <= set(line.split('=', 1)[1].split(','))


def test_release_wait_keeps_exact_sha_and_three_successes():
    workflow = (ROOT / '.github/workflows/ci.yml').read_text()
    assert '[ "$deployed_sha" = "$EXPECTED_SHA" ]' in workflow
    assert '[ "$ok" -ge 3 ]' in workflow
    assert 'deadline=$((SECONDS + 900))' in workflow
