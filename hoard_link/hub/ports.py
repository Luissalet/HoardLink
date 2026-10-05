"""Stable, opt-in ports for apps the Hub launches; never take another listener."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
from urllib.parse import urlsplit, urlunsplit

from . import procs


def _loopback(url):
    try:
        p = urlsplit(url)
        return p.scheme == 'http' and p.hostname in {'localhost', '127.0.0.1', '::1'} and p.port and not p.username and not p.password
    except ValueError:
        return False


def _with_port(url, port):
    p = urlsplit(url)
    host = f'[{p.hostname}]' if ':' in p.hostname else p.hostname
    return urlunsplit((p.scheme, f'{host}:{port}', p.path, p.query, p.fragment))


class PortAssignments:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            self.records = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self.records = {}

    def _apply(self, app, port):
        old = app.url
        app.url = _with_port(old, port)
        if app.launch:
            spec = app.launch
            if spec.readiness_url and urlsplit(spec.readiness_url).netloc == urlsplit(old).netloc:
                spec.readiness_url = _with_port(spec.readiness_url, port)
            if spec.port_argument:
                args, skip = [], False
                for arg in spec.argv:
                    if skip:
                        skip = False
                        continue
                    if arg == spec.port_argument:
                        skip = True
                    elif not arg.startswith(spec.port_argument + '='):
                        args.append(arg)
                spec.argv = args + [spec.port_argument, str(port)]

    def restore(self, apps):
        with self.lock:
            for app in apps:
                app._manifest_url = app.url
                rec = self.records.get(app.id, {})
                if (app.launch and app.launch.port_argument and isinstance(rec, dict)
                        and rec.get('manifest_url') == app.url and isinstance(rec.get('port'), int)
                        and 1024 <= rec['port'] <= 65535 and _loopback(app.url)):
                    self._apply(app, rec['port'])
                self.discover(app)

    def discover(self, app):
        """A declared local sidecar is a hint; only matching live identity wins."""
        if not app.runtime_url_file or not app.expect_service:
            return False
        try:
            path, root = Path(app.runtime_url_file).resolve(), Path(app.data_dir).resolve()
            if not path.is_relative_to(root) or path.stat().st_size > 1024:
                return False
            url = path.read_text(encoding='utf-8').strip().rstrip('/')
            if not _loopback(url) or urlsplit(url).path not in ('', '/') or urlsplit(url).query or urlsplit(url).fragment:
                return False
            status, body = procs.fetch_json(url + app.health_path, timeout=.5,
                                            follow_redirects=False, max_bytes=8192)
            if status != 200 or not isinstance(body, dict) or body.get('service') != app.expect_service:
                return False
            self._apply(app, urlsplit(url).port)
            return True
        except (OSError, ValueError):
            return False

    def prepare(self, app, apps, hub_port):
        with self.lock:
            self.discover(app)
            observed = procs.health(app)
            if observed.state == 'healthy':
                return {'ok': True}
            listeners = procs.listening_pids()
            conflicts = {hub_port, *(a.port for a in apps if a.id != app.id)}
            conflicts.update(urlsplit(getattr(a, '_manifest_url', a.url)).port for a in apps if a.id != app.id and _loopback(a.url))
            reserved = conflicts | {urlsplit(getattr(app, '_manifest_url', app.url)).port}
            if observed.state != 'foreign' and app.port not in listeners and app.port not in conflicts:
                if app.launch and app.launch.port_argument and _loopback(app.url):
                    self._apply(app, app.port)
                return {'ok': True}
            if not app.launch or not app.launch.port_argument or not _loopback(app.url):
                return {'ok': False, 'error': f'port {app.port} is busy; this app has no declared port adaptation'}
            # The Hub serializes allocation; apps still bind exclusively, so a
            # process outside the Hub winning the final race causes an error.
            old = app.port
            for port in range(max(1024, old + 1), min(65536, max(1024, old + 1) + 512)):
                if port in reserved or port in listeners:
                    continue
                try:
                    with socket.socket() as probe:
                        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                        probe.bind(('127.0.0.1', port))
                except OSError:
                    continue
                record = {'manifest_url': getattr(app, '_manifest_url', app.url), 'port': port}
                records = {**self.records, app.id: record}
                temp = self.path.with_suffix('.tmp')
                try:
                    temp.write_text(json.dumps(records, indent=2), encoding='utf-8')
                    os.replace(temp, self.path)
                except OSError as exc:
                    return {'ok': False, 'error': f'could not save port assignment: {exc}'}
                self.records = records
                self._apply(app, port)
                return {'ok': True, 'previous_port': old, 'port': port}
            return {'ok': False, 'error': 'no free unreserved port in the allocation range'}
