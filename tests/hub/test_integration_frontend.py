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
    assert frontend.prepare_frontend(tmp_path, {'docs/INTEGRATION.md'}, tmp_path) is None
    build = frontend.prepare_frontend(tmp_path, {source}, tmp_path)
    workdir = tmp_path / 'apps/agora' if marker.startswith('apps/') else tmp_path
    assert calls == [('ci', workdir), ('run', workdir)]
    assert build['destination'] == destination
    assert build['entry'] == entry
    assert len(build['sha256']) == 64


def test_failed_build_does_not_change_the_served_entry(tmp_path, monkeypatch):
    manifest = tmp_path / 'apps/agora/package.json'
    served = manifest.parent / 'dist/index.html'
    served.parent.mkdir(parents=True)
    manifest.touch()
    served.write_text('original', encoding='utf-8')
    monkeypatch.setattr(frontend.shutil, 'which', lambda _: 'npm')
    monkeypatch.setattr(frontend.subprocess, 'run', lambda cmd, **kw: CompletedProcess(cmd, 1))
    with pytest.raises(IntegrationError, match='frontend-build.log'):
        frontend.prepare_frontend(tmp_path, {'apps/agora/src/App.tsx'}, tmp_path)
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
