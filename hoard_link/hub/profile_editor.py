"""Human-facing group editing, reusing profiles and the Windows installer."""
from __future__ import annotations
import copy
import re
from . import autostart


def save(hub, name, body):
    if not isinstance(name, str) or not re.fullmatch(r'\w[\w .-]{0,63}', name, re.UNICODE) or name != name.strip():
        return {'ok': False, 'error': 'group name: 1–64 letters, digits, spaces, dot, hyphen or underscore'}
    if set(body) - {'apps', 'desktop', 'create'}:
        return {'ok': False, 'error': 'only apps and desktop selections can be edited here'}
    if 'create' in body and not isinstance(body['create'], bool):
        return {'ok': False, 'error': 'create must be a boolean'}
    apps, desktop = body.get('apps'), body.get('desktop', [])
    if not isinstance(apps, list) or not isinstance(desktop, list) or not all(isinstance(a, str) for a in apps + desktop):
        return {'ok': False, 'error': 'apps and desktop must be lists of app IDs'}
    apps, desktop = list(dict.fromkeys(apps)), list(dict.fromkeys(desktop))
    if not apps or set(desktop) - set(apps) or len(apps) > 128:
        return {'ok': False, 'error': 'select at least one app; desktop apps must belong to the group'}
    unknown = [a for a in apps if hub.get(a) is None]
    if unknown:
        return {'ok': False, 'error': 'unknown apps: ' + ', '.join(unknown)}
    with hub._lock:
        old = copy.deepcopy(hub.config.profiles)
        if body.get('create') and name in old:
            return {'ok': False, 'error': 'a group with this name already exists'}
        spec = copy.deepcopy(old.get(name, {}))
        spec.update(apps=apps, desktop=desktop)  # preserve external commands from config
        hub.config.profiles = {**old, name: spec}
        try:
            hub.config.save()
        except OSError as exc:
            hub.config.profiles = old
            return {'ok': False, 'error': str(exc)}
    return {'ok': True, 'name': name}


def remove(hub, name):
    with hub._lock:
        if name not in hub.config.profiles:
            return {'ok': False, 'error': 'unknown group'}
        group = hub.profile_status(name)
        if any(m.get('state') in {'running', 'starting'} for m in group.get('members', [])):
            return {'ok': False, 'error': 'stop the group before removing it; its running apps and commands remain controllable'}
        current = autostart.status()
        if current.get('installed') and current.get('profile') == name:
            return {'ok': False, 'error': 'disable this group’s Windows startup before removing it'}
        old = hub.config.profiles
        hub.config.profiles = {k: v for k, v in old.items() if k != name}
        try:
            hub.config.save()
        except OSError as exc:
            hub.config.profiles = old
            return {'ok': False, 'error': str(exc)}
    return {'ok': True, 'name': name}


def startup(hub, body):
    with hub._lock:
        if body.get('enabled') is False:
            return autostart.uninstall()
        if body.get('enabled') is not True:
            return {'ok': False, 'error': 'enabled must be a boolean'}
        name = body.get('profile')
        profiles = hub.profiles()
        p = profiles.get(name) if isinstance(name, str) else None
        if not p or not p.members or any(hub.get(a) is None for a in p.members):
            return {'ok': False, 'error': 'select a nonempty group containing registered apps'}
        if not re.fullmatch(r'\w[\w .-]{0,63}', name, re.UNICODE):
            return {'ok': False, 'error': 'group name cannot be represented safely at Windows startup'}
        return autostart.install(name, extra=['--data-dir', hub.config.data_dir, '--port', str(hub.config.port)])
