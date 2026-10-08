"""Describe available backup sources without letting one bad source hide the store."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from ..paths import clean_user_path, unsafe_folder


def read_shared_root(data_dir: str) -> str:
    configuration = json.loads((Path(data_dir) / 'storage.json').read_text(encoding='utf-8'))
    raw = configuration.get('root') if isinstance(configuration, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('Atlas shared storage root is not configured')
    return clean_user_path(raw)


def inventory(apps: Iterable[Any], hub_dir: str, only: Optional[list[str]] = None) -> dict[str, Any]:
    wanted = set(only or [])
    apps = list(apps)
    sources = {a.id: a.data_dir for a in apps if a.data_dir and (not wanted or a.id in wanted)}
    errors: list[dict[str, str]] = []
    atlas = next((a for a in apps if a.id == 'atlas'), None)
    if atlas and atlas.data_dir and (not wanted or wanted.intersection({'atlas', 'atlas-files'})):
        try:
            raw = read_shared_root(atlas.data_dir)
            # Validate the configured path before resolving it: a relative path must never become cwd.
            problem = unsafe_folder(raw, lang='en')
            if problem:
                raise ValueError('Atlas shared storage root is unavailable: ' + problem)
            sources['atlas-files'] = str(Path(raw).resolve())
        except (OSError, ValueError, TypeError) as exc:
            errors.append({'source': 'atlas-files', 'error': str(exc)})
    if not wanted or 'hub' in wanted:
        sources['hub'] = hub_dir
    return {'sources': sources, 'source_errors': errors}
