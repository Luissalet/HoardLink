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
        def change(profiles):
            if body.get('create') and name in profiles:
                raise ValueError('a group with this name already exists')
            spec = copy.deepcopy(profiles.get(name, {}))
            spec.update(apps=apps, desktop=desktop)
            return {**profiles, name: spec}
        try:
            hub.config.update_profiles(change)
        except (OSError, ValueError) as exc:
            return {'ok': False, 'error': str(exc)}
    return {'ok': True, 'name': name}


def patch(hub, name, apps, desktop):
    """Change selected membership flags against the latest stored profile."""
    if not isinstance(name, str) or not re.fullmatch(r'\w[\w .-]{0,63}', name, re.UNICODE) or name != name.strip():
        return {'ok': False, 'error': 'invalid group name'}
    for values in (apps, desktop):
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, bool) for k, v in values.items()):
            return {'ok': False, 'error': 'membership changes must map app IDs to booleans'}
        if any(value and hub.get(app_id) is None for app_id, value in values.items()):
            return {'ok': False, 'error': 'unknown app in membership changes'}
    def apply_flags(old, changes):
        return [value for value in old if changes.get(value, True)] + [key for key, value in changes.items() if value and key not in old]
    def change(profiles):
        from .profiles import parse
        if name not in profiles:
            raise ValueError('unknown group')
        spec = copy.deepcopy(profiles[name])
        profile = parse({name: spec})[0][name]
        next_apps = apply_flags(profile.apps, apps)
        next_desktop = apply_flags(profile.desktop, {**{key: False for key, value in apps.items() if not value}, **desktop})
        if set(next_desktop) - set(next_apps + profile.desktop) or any(not apps.get(key, True) for key in next_desktop):
            raise ValueError('desktop apps must belong to the group')
        if not next_apps and not next_desktop and not profile.commands:
            raise ValueError('select at least one app')
        if apps:
            spec['apps'] = next_apps
        if desktop or any(not value for value in apps.values()):
            spec['desktop'] = next_desktop
        return {**profiles, name: spec}
    with hub._lock:
        try:
            hub.config.update_profiles(change)
        except (OSError, ValueError) as exc:
            return {'ok': False, 'error': str(exc)}
    return {'ok': True, 'name': name}


def remove(hub, name):
    with hub._lock:
        if name not in hub.config.read_profiles():
            return {'ok': False, 'error': 'unknown group'}
        group = hub.profile_status(name)
        if any(m.get('state') in {'running', 'starting'} for m in group.get('members', [])):
            return {'ok': False, 'error': 'stop the group before removing it; its running apps and commands remain controllable'}
        current = autostart.status()
        if current.get('installed') and current.get('profile') == name:
            return {'ok': False, 'error': 'disable this group’s Windows startup before removing it'}
        try:
            hub.config.update_profiles(lambda profiles: {k: v for k, v in profiles.items() if k != name})
        except (OSError, ValueError) as exc:
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
