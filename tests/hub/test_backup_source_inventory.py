"""A broken shared source is visible without hiding unrelated backup history."""
from pathlib import Path
import json
import pytest
from hoard_link.hub import tools
from ._hub_fakes import make_hub, serve, http


@pytest.fixture
def hub(tmp_path):
    h = make_hub(tmp_path, [], extra_apps=['atlas', 'other'])
    Path(h.get('other').data_dir, 'report.txt').write_text('preserved evidence', encoding='utf-8')
    yield h
    h.close()


@pytest.mark.parametrize('storage', ['{}', '{', '[]', '{"root":123}', '{"root":"relative-folder"}'])
def test_bad_source_keeps_status_readable_and_selected_backup_operational(hub, storage):
    Path(hub.get('atlas').data_dir, 'storage.json').write_text(storage, encoding='utf-8')
    server = serve(hub)
    try:
        status, state = http(hub.config.url.rstrip('/') + '/api/backups')
        assert status == 200 and state['ok'] and state['source_errors'][0]['source'] == 'atlas-files'
        assert 'other' in state['sources'] and 'atlas-files' not in state['sources']
        mcp = tools.call(hub, 'hub_backup_status', {})
        assert mcp['ok'] and mcp['source_errors'] == state['source_errors']
        blocked = hub.backup_run()
        assert not blocked['ok'] and blocked['source_errors'] and not hub.backups.list_snapshots()
        selected = hub.backup_run(['other'], 'selected')
        assert selected['ok'] and list(selected['apps']) == ['other']
        assert hub.backups.verify(selected['snapshot'])['ok']
        status, history = http(hub.config.url.rstrip('/') + '/api/backups')
        assert status == 200 and history['snapshots'][-1]['id'] == selected['snapshot']
        with pytest.raises(ValueError):
            hub.backup_sources(['atlas'])
    finally:
        server.shutdown()
        server.server_close()


def test_missing_configuration_and_nonexistent_or_unsafe_root_are_explicit(hub, tmp_path):
    assert hub.backup_source_inventory()['source_errors']
    config = Path(hub.get('atlas').data_dir, 'storage.json')
    for root in [str(tmp_path / 'missing'), Path(tmp_path).anchor]:
        config.write_text(json.dumps({'root': root}), encoding='utf-8')
        result = hub.backup_run(['atlas-files'])
        assert not result['ok'] and result['source_errors'][0]['source'] == 'atlas-files'
        assert not hub.backups.list_snapshots()
    assert not hub.backup_source_inventory(['other'])['source_errors']


def test_valid_root_includes_original_files_and_keeps_selection_scoped(hub, tmp_path):
    shared = tmp_path / 'originals'
    shared.mkdir()
    (shared / 'original.txt').write_text('original', encoding='utf-8')
    Path(hub.get('atlas').data_dir, 'storage.json').write_text(json.dumps({'root': ' "' + str(shared) + '" '}), encoding='utf-8')
    plan = hub.backup_source_inventory()
    assert not plan['source_errors'] and plan['sources']['atlas-files'] == str(shared.resolve())
    result = hub.backup_run(['atlas'])
    assert result['ok'] and set(result['apps']) == {'atlas', 'atlas-files'}
    result = hub.backup_run(['atlas-files'])
    assert result['ok'] and set(result['apps']) == {'atlas-files'}
    assert not hub.backup_restore(result['snapshot'], 'atlas-files', in_place=True)['ok']
    assert (shared / 'original.txt').read_text(encoding='utf-8') == 'original'
    restored = hub.backup_restore(result['snapshot'], 'atlas-files', dest=str(tmp_path / 'restored-originals'))
    assert restored['ok']
    assert (tmp_path / 'restored-originals' / 'original.txt').read_text(encoding='utf-8') == 'original'
    assert hub.backup_sources(['other']) == {'other': hub.get('other').data_dir}


def test_explicit_destination_cannot_restore_into_old_or_current_shared_roots(hub, tmp_path):
    original = tmp_path / 'old-originals'
    current = tmp_path / 'new-originals'
    original.mkdir()
    current.mkdir()
    config = Path(hub.get('atlas').data_dir, 'storage.json')
    config.write_text(json.dumps({'root': str(original)}), encoding='utf-8')
    (original / 'valuable.txt').write_text('historical original', encoding='utf-8')
    snapshot = hub.backup_run(['atlas-files'])['snapshot']
    # Emptying the old root would make BackupStore accept it as a normal empty destination.
    (original / 'valuable.txt').unlink()
    config.write_text(json.dumps({'root': str(current)}), encoding='utf-8')
    for dest in (original, original / 'child', original.parent, current, current / 'child'):
        blocked = hub.backup_restore(snapshot, 'atlas-files', dest=str(dest), in_place=False)
        assert not blocked['ok'] and 'outside the original storage roots' in blocked['error']
    assert list(original.iterdir()) == [] and list(current.iterdir()) == []
    safe = tmp_path / 'review-copy'
    assert hub.backup_restore(snapshot, 'atlas-files', dest=str(safe))['ok']
    assert (safe / 'valuable.txt').read_text(encoding='utf-8') == 'historical original'


def test_default_destination_is_also_checked_against_current_shared_storage(hub, tmp_path):
    from hoard_link.hub.backup import _stamp
    original = tmp_path / 'originals'
    original.mkdir()
    (original / 'file.txt').write_text('original', encoding='utf-8')
    config = Path(hub.get('atlas').data_dir, 'storage.json')
    config.write_text(json.dumps({'root': str(original)}), encoding='utf-8')
    snapshot = hub.backup_run(['atlas-files'])['snapshot']
    hub.backups._now = lambda: 1800000000
    # A configured but currently missing root must not be created by a default restore.
    current = original.with_name(original.name + '.restored-' + _stamp(hub.backups._now()))
    config.write_text(json.dumps({'root': str(current)}), encoding='utf-8')
    blocked = hub.backup_restore(snapshot, 'atlas-files')
    assert not blocked['ok'] and not current.exists()
