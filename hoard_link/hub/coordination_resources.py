"""Native room reservations use the existing, globally shared Agora leases."""
from __future__ import annotations

import os
from pathlib import Path

from .agora import conflicts, normalize_resource


def git_directory(root):
    marker = Path(root) / '.git'
    if marker.is_dir():
        return marker.resolve()
    if marker.is_file():
        text = marker.read_text(encoding='utf-8').strip()
        if text.startswith('gitdir:'):
            directory = (Path(root) / text[7:].strip()).resolve()
            common = directory / 'commondir'
            return (directory / common.read_text(encoding='utf-8').strip()).resolve() if common.is_file() else directory
    return None


def resource_key(agora, value, workspace='', roots=()):
    kind, separator, name = str(value).partition(':')
    if kind not in ('file', 'directory', 'folder', 'absolute'):
        return normalize_resource(value)
    path = Path(name)
    if not path.is_absolute():
        path = Path(workspace or os.getcwd()) / path
    path = path.resolve()
    # Registered repositories and ALL their worktrees share the same logical
    # path. The catalog is the authority for repository names, not basenames.
    if roots:
        for repo, root in sorted(roots, key=lambda item: len(str(item[1])), reverse=True):
            try:
                relative = path.relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                continue
            return normalize_resource('path:' + repo + '/' + relative)
        # Linked worktrees appear immediately; no scan of every repository's
        # Git processes belongs on a native file-write path.
        for ancestor in (path, *path.parents):
            common = git_directory(ancestor)
            if common is None:
                continue
            for repo, root in roots:
                if git_directory(root) == common:
                    return normalize_resource('path:' + repo + '/' + path.relative_to(ancestor).as_posix())
            break
    elif agora.repo_roots:
        # Known names include declarations even when no app is running.
        names = {r['repo'] for r in agora.db.query("SELECT DISTINCT repo FROM tasks WHERE repo<>''")}
        for repo in sorted(names):
            for root in agora.repo_roots(repo):
                try:
                    relative = path.relative_to(Path(root).resolve()).as_posix()
                except ValueError:
                    continue
                return normalize_resource('path:' + repo + '/' + relative)
    # Unregistered folders still overlap across rooms, including parent paths.
    canonical = str(path).replace('\\', '/')
    if os.name == 'nt':
        canonical = canonical.lower()
    return normalize_resource('path:@filesystem/' + canonical)


def operate(agora, args, roots=()):
    owner = str(args.get('owner') or '')
    action = args.get('action')
    resources = [resource_key(agora, p, args.get('workspace') or '', roots) for p in args.get('resources') or []]
    if not owner or action not in ('claim', 'check', 'release', 'renew'):
        raise ValueError('A reservation needs an owner and a valid action')
    with agora.db.tx():
        live = agora._live_locks()
        if action == 'check':
            return {'ok': True, 'conflicts': [r for r in live if r['owner'] != owner
                    and any(conflicts(p, r['resource']) for p in resources)]}
        if action == 'claim':
            return agora._acquire(owner, resources, None, agora._ttl(None), 'Native working room')
        if action == 'renew':
            now = agora.clock()
            agora.db.execute('UPDATE locks SET expires=?+ttl_s WHERE owner=?', (now, owner))
            return {'ok': True}
        released = []
        for resource in resources:
            row = agora.db.one('SELECT owner FROM locks WHERE resource=?', (resource,))
            if row and row['owner'] == owner:
                agora.db.execute('DELETE FROM locks WHERE resource=? AND owner=?', (resource, owner))
                released.append(resource)
        return {'ok': True, 'released': released}
