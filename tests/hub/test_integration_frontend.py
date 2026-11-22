import hashlib
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from hoard_link.hub import integration_frontend as frontend
from hoard_link.hub.integration_git import IntegrationError


@pytest.mark.parametrize(
    'marker,source,destination,entry',
    [('apps/agora/package.json', 'hoard_link/hub/ui/production_decisions_data.js', 'apps/agora/dist', 'index.html'),
     ('studio/src/screens/agora/AgoraApp.tsx', 'studio/src/screens/agora/ChannelList.tsx', 'static/studio', 'studio.js')],
)
def test_prepare_builds_only_when_frontend_inputs_change(tmp_path, monkeypatch, marker, source, destination, entry):
    marker_path = tmp_path / marker
    marker_path.parent.mkdir(parents=True)
    marker_path.touch()
    calls = []

    def run(command, *, cwd, **options):
        calls.append((command[1], cwd))
        if command[1] == 'run':
            output = Path(command[-1])
            output.mkdir()
            (output / entry).write_text('compiled entry', encoding='utf-8')
        return CompletedProcess(command, 0)

    monkeypatch.setattr(frontend.shutil, 'which', lambda _: 'npm')
    monkeypatch.setattr(frontend.subprocess, 'run', run)
    assert frontend.prepare_frontend(tmp_path, {'docs/INTEGRATION.md'}, tmp_path, revision="a" * 40) is None
    build = frontend.prepare_frontend(tmp_path, {source}, tmp_path, revision="a" * 40)
    workdir = tmp_path / 'apps/agora' if marker.startswith('apps/') else tmp_path
    assert calls == [('ci', workdir), ('run', workdir)]
    assert build['destination'] == destination
    assert build['entry'] == entry
    assert len(build['sha256']) == 64
    receipt = json.loads((tmp_path / 'frontend-output' / frontend.RECEIPT).read_text())
    assert receipt['source_commit'] == 'a' * 40
    assert receipt['entry_sha256'] == build['sha256']


def test_failed_build_does_not_change_the_served_entry(tmp_path, monkeypatch):
    manifest = tmp_path / 'apps/agora/package.json'
    served = manifest.parent / 'dist/index.html'
    served.parent.mkdir(parents=True)
    manifest.touch()
    served.write_text('original', encoding='utf-8')
    monkeypatch.setattr(frontend.shutil, 'which', lambda _: 'npm')
    monkeypatch.setattr(frontend.subprocess, 'run', lambda cmd, **kw: CompletedProcess(cmd, 1))
    with pytest.raises(IntegrationError, match='frontend-build.log'):
        frontend.prepare_frontend(tmp_path, {'apps/agora/src/App.tsx'}, tmp_path, revision="a" * 40)
    assert served.read_text(encoding='utf-8') == 'original'


def test_publishing_activates_entry_after_dependencies_and_undo_restores_previous_bundle(tmp_path):
    staging = tmp_path / 'frontend-output'
    destination = tmp_path / 'dist'
    for folder in (staging, destination):
        (folder / 'assets').mkdir(parents=True)
    (destination / 'index.html').write_bytes(b'old entry')
    (destination / 'assets/previous.js').write_bytes(b'old chunk')
    (staging / 'index.html').write_bytes(b'new entry')
    (staging / 'assets/next.js').write_bytes(b'new chunk')

    build = {'destination': 'dist', 'entry': 'index.html'}
    frontend.prepare_publication(tmp_path, build, tmp_path)
    assert build['files'] == ['assets/next.js', 'index.html']
    frontend.publish_frontend(tmp_path, build, tmp_path)
    assert (destination / 'index.html').read_bytes() == b'new entry'
    assert (destination / 'assets/next.js').read_bytes() == b'new chunk'
    assert (destination / 'assets/previous.js').read_bytes() == b'old chunk'
    frontend.restore_frontend(tmp_path, build, tmp_path)
    assert (destination / 'index.html').read_bytes() == b'old entry'
    assert not (destination / 'assets/next.js').exists()
    assert (destination / 'assets/previous.js').read_bytes() == b'old chunk'


@pytest.mark.parametrize('source', ('apps/agora/src-tauri/src/main.rs', 'hoard_link/hub/ui_session.py'))
def test_native_build_updates_the_shortcut_target_and_can_be_undone(tmp_path, monkeypatch, source):
    candidate = tmp_path / 'candidate'
    app = candidate / 'apps/agora'
    (app / 'src-tauri').mkdir(parents=True)
    (app / 'package.json').touch()
    (app / 'src-tauri/Cargo.toml').touch()
    receipt = tmp_path / 'receipt'
    receipt.mkdir()
    repo = tmp_path / 'installed'
    shortcut = repo / 'apps/agora/bin/hoard-agora.exe'
    shortcut.parent.mkdir(parents=True)
    shortcut.write_bytes(b'previous executable')
    calls = []

    def run(command, *, cwd, env, **options):
        assert cwd == app
        calls.append(command[1:])
        if command[1] == 'run':
            (app / 'dist').mkdir()
            (app / 'dist/index.html').write_bytes(b'new web entry')
            release = Path(env['CARGO_TARGET_DIR']) / 'release'
            release.mkdir(parents=True)
            (release / shortcut.name).write_bytes(b'new executable')
        return CompletedProcess(command, 0)

    monkeypatch.setattr(frontend, 'WINDOWS', True)
    monkeypatch.setattr(frontend.shutil, 'which', lambda command: 'npm' if command == 'npm.cmd' else None)
    monkeypatch.setattr(frontend.subprocess, 'run', run)
    assert frontend.prepare_frontend(candidate, {'docs/INTEGRATION.md'}, receipt, revision="a" * 40) is None
    build = frontend.prepare_frontend(candidate, {source}, receipt, revision="a" * 40)
    assert calls == [['ci', '--no-audit', '--no-fund'], ['run', 'desktop:build', '--', '--', '--locked']]
    assert build['native']['sha256'] == hashlib.sha256(b'new executable').hexdigest()
    assert shortcut.read_bytes() == b'previous executable'

    # Simulate Windows holding the old image open until it is renamed.
    replace = Path.replace

    def replace_if_closed(path, destination):
        if destination == shortcut and shortcut.exists():
            raise PermissionError('running executable')
        return replace(path, destination)

    monkeypatch.setattr(Path, 'replace', replace_if_closed)
    frontend.prepare_publication(repo, build, receipt)
    frontend.publish_frontend(repo, build, receipt)
    assert shortcut.read_bytes() == b'new executable'
    assert (repo / 'apps/agora/dist/index.html').read_bytes() == b'new web entry'
    frontend.restore_frontend(repo, build, receipt)
    assert shortcut.read_bytes() == b'previous executable'
    assert not (repo / 'apps/agora/dist/index.html').exists()
