"""Bootstrap recovery uses real local venv/ensurepip, without installing app deps."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import venv

import pytest


def launcher_at(root, monkeypatch):
    spec = importlib.util.spec_from_file_location('bootstrap_launcher', Path(__file__).parents[1] / 'start.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (root / 'frontend' / 'dist').mkdir(parents=True)
    (root / 'frontend' / 'dist' / 'index.html').write_text('test')
    (root / 'requirements.txt').write_text('')
    (root / 'data').mkdir()
    (root / 'data' / 'keep.txt').write_text('BUSINESS DATA')
    (root / '.env').write_text('TCG_API_KEY=KEEP-EXISTING-KEY\n')
    monkeypatch.setattr(module, 'ROOT', root)
    monkeypatch.setattr(sys, 'argv', ['start.py', '--check'])
    return module


@pytest.mark.parametrize('state', ['new', 'missing_pip', 'missing_activate'])
def test_bootstrap_recovers_incomplete_environment_with_visible_pip_output(tmp_path, monkeypatch, state):
    launcher = launcher_at(tmp_path, monkeypatch)
    environment = tmp_path / '.venv'
    scripts = environment / ('Scripts' if sys.platform == 'win32' else 'bin')
    executable = scripts / ('python.exe' if sys.platform == 'win32' else 'python')
    activation = scripts / 'activate'
    if state != 'new':
        venv.EnvBuilder(with_pip=state == 'missing_activate').create(environment)
        if state == 'missing_activate':
            activation.unlink()
    commands = []
    original = subprocess.run
    def run(command, *args, **kwargs):
        command = [str(part) for part in command]
        commands.append((command, kwargs))
        if 'pip' in command and 'install' in command:
            # Only application dependency installation is replaced. pip itself must
            # actually work before the launcher reaches this boundary.
            assert original([str(executable), '-m', 'pip', '--version'], capture_output=True).returncode == 0
            return subprocess.CompletedProcess(command, 0)
        if '-c' in command and 'import fastapi' in command[-1]:
            return subprocess.CompletedProcess(command, 0)
        return original(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    launcher.main()
    assert activation.is_file()
    assert original([str(executable), '-m', 'pip', '--version'], capture_output=True).returncode == 0
    if state != 'missing_activate':
        installs = [(cmd, kw) for cmd, kw in commands if 'ensurepip' in cmd]
        assert installs and all(kw.get('stdout') != subprocess.PIPE for _, kw in installs)
    assert (tmp_path / '.env').read_text() == 'TCG_API_KEY=KEEP-EXISTING-KEY\n'
    assert (tmp_path / 'data' / 'keep.txt').read_text() == 'BUSINESS DATA'
    assert (environment / 'tcg-requirements.sha256').is_file()
    # A completed environment is reused without recreating it or reinstalling deps.
    commands.clear()
    launcher.main()
    assert not any('ensurepip' in cmd or 'install' in cmd for cmd, _ in commands)


def test_keyboard_interrupt_gives_recovery_instruction_without_success_stamp(tmp_path, monkeypatch, capsys):
    launcher = launcher_at(tmp_path, monkeypatch)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(venv.EnvBuilder, 'create', interrupt)
    with pytest.raises(SystemExit) as stopped:
        launcher.main()
    assert stopped.value.code == 130
    assert '重新运行' in capsys.readouterr().out
    assert not (tmp_path / '.venv' / 'tcg-requirements.sha256').exists()
