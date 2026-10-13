"""Canonical room repositories for Agora, Council and native working groups."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..coordination.council_store import CouncilStore
from ..coordination.groups import GroupStore
from ..coordination.migration import import_legacy
from ..coordination.rpc import COUNCIL_METHODS, GROUP_METHODS, decode, encode, error_payload
from ..coordination.spaces import DEFAULT_SPACE_ID
from .facets import Facet, Request


class CoordinationFacet(Facet):
    id = 'coordination'

    def __init__(self, hub):
        super().__init__(hub)
        self.path = Path(hub.config.data_dir) / 'agora.db'
        self.council = CouncilStore(path=str(self.path), allow_quarantine=False)
        self.groups = GroupStore(self.path)
        self.council.stats()  # establish the extension schema, not another engine
        self._resource_roots = []
        self._resource_roots_at = 0

    def repository(self, args: dict[str, Any]) -> dict:
        kind, operation = args.get('kind'), args.get('operation')
        allowed = COUNCIL_METHODS if kind == 'council' else GROUP_METHODS if kind == 'groups' else ()
        if operation not in allowed:
            return {'ok': False, 'status': 400, 'error': {'type': 'ValueError', 'detail': 'Unknown coordination operation'}}
        try:
            store = self.council if kind == 'council' else self.groups
            positional = decode(args.get('args') or [])
            named = decode(args.get('kwargs') or {})
            result = getattr(store, operation)(*positional, **named)
            return {'ok': True, 'result': encode(result)}
        except Exception as error:
            return {'ok': False, 'status': 409 if type(error).__name__ in ('RevisionConflict', 'ClaimConflict', 'DuplicateTurn') else 400,
                    'error': error_payload(error)}

    def config(self, args=None):
        return {'ok': True, 'authority': 'hub', 'schema_version': 1,
                'default_space_id': DEFAULT_SPACE_ID, 'room_kinds': ['council', 'groups'],
                'workspace_url': str((self.hub.config.faustus_urls or [''])[0]).rstrip('/') + '/agora',
                'execution': 'native-faustus'}

    def resources(self, args):
        import time
        from .coordination_resources import operate
        if time.monotonic() - self._resource_roots_at > 60:
            roots = []
            for repo in self.hub.repos.discover():
                roots.append((repo['name'], Path(repo['path'])))
            self._resource_roots = roots
            self._resource_roots_at = time.monotonic()
        return operate(self.hub.facet('agora').agora, args, self._resource_roots)

    def bind(self, args):
        try:
            receipt = import_legacy(self.path, args.get('sources') or {})
            return {'ok': True, 'authority': 'hub', 'imports': receipt}
        except Exception as error:
            return {'ok': False, 'status': 409, 'error': error_payload(error)}

    def rooms(self, args=None):
        args = args or {}
        sid = args.get('space_id') or DEFAULT_SPACE_ID
        self.hub.facet('agora').agora.space({'space_id': sid})
        owner = args.get('owner') or ''
        sessions, offset = [], 0
        while True:
            page = self.council.list_sessions(owner=owner, space_id=sid, limit=1000, offset=offset)
            sessions.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
        rooms = [{'kind': 'council', 'id': room.id, 'space_id': room.space_id, 'title': room.title,
                  'owner': room.owner, 'mode': room.policy, 'status': room.status, 'revision': room.revision}
                 for room in sessions]
        db = self.hub.facet('agora').agora.db
        where, params = 'archived=0 AND space_id=?', [sid]
        group_owner = args.get('group_owner', owner)
        if group_owner or 'group_owner' in args:
            where += ' AND owner=?'
            params.append(group_owner)
        for row in db.query('SELECT id,owner,revision FROM groups WHERE ' + where, params):
            group = self.groups.group(row['id'], row['owner'])
            turns = self.groups.turns(row['id'])
            latest = turns[-1]['status'] if turns else 'idle'
            rooms.append({'kind': 'groups', 'id': row['id'], 'space_id': sid, 'title': group.get('name') or row['id'],
                          'owner': row['owner'], 'mode': group.get('mode'), 'status': latest, 'revision': row['revision']})
        return {'ok': True, 'space_id': sid, 'rooms': rooms}

    def room(self, args):
        kind, ident, owner = args.get('kind'), args.get('id'), args.get('owner') or ''
        if kind not in ('council', 'groups'):
            return {'ok': False, 'status': 400, 'error': 'Unknown room kind'}
        if kind == 'council':
            room = self.council.get_session(ident, owner=owner)
            if room is None:
                return {'ok': False, 'status': 404, 'error': 'Room not found'}
            if args.get('space_id') and room.space_id != args['space_id']:
                return {'ok': False, 'status': 404, 'error': 'Room not found in this space'}
            return {'ok': True, 'kind': kind, 'session': room.to_dict(),
                    'participants': [p.to_dict() for p in self.council.list_participants(ident)]}
        try:
            group = self.groups.group(ident, owner)
            if args.get('space_id') and group.get('space_id', DEFAULT_SPACE_ID) != args['space_id']:
                return {'ok': False, 'status': 404, 'error': 'Room not found in this space'}
            return {'ok': True, 'kind': kind, 'group': group,
                    'turns': self.groups.turns(ident)}
        except KeyError:
            return {'ok': False, 'status': 404, 'error': 'Room not found'}

    def ledger(self, args):
        visible = self.room(args)
        if not visible.get('ok'):
            return visible
        ident = args.get('id')
        if args.get('kind') == 'council':
            return {'ok': True, 'kind': 'council', **{name: [r.to_dict() for r in getattr(self.council, 'list_' + name)(ident)]
                    for name in ('tasks', 'decisions', 'objections', 'claims')}}
        turns = self.groups.turns(ident)
        return {'ok': True, 'kind': 'groups', 'turns': turns,
                'steps': [step for turn in turns for step in self.groups.steps(turn['id'])]}

    def messages(self, args):
        visible = self.room(args)
        if not visible.get('ok'):
            return visible
        if args.get('kind') == 'council':
            return {'ok': True, 'messages': [m.to_dict() for m in self.council.list_messages(args['id'],
                    viewer_id=args.get('viewer_id') or args.get('owner') or '', since_id=args.get('since_id') or '')]}
        messages = []
        for turn in self.groups.turns(args['id']):
            messages.append({'id': turn['id'], 'author_kind': 'human', 'content': turn['message'], 'turn_id': turn['id']})
            for step in self.groups.steps(turn['id']):
                if step['text'] or step['error']:
                    messages.append({'id': step['id'], 'author_id': step['member_id'], 'author_kind': 'model',
                                     'content': step['text'], 'error': step['error'], 'status': step['status'],
                                     'metadata': step['detail'], 'turn_id': turn['id']})
        return {'ok': True, 'messages': messages}

    def get(self, req: Request):
        if not req.path.startswith('/api/coordination/'):
            return None
        op = req.path.rsplit('/', 1)[-1]
        if op not in ('config', 'rooms', 'room', 'ledger', 'messages'):
            return {'ok': False, 'status': 404, 'error': 'Unknown coordination route'}
        return getattr(self, op)({k: v[0] for k, v in req.query.items() if v})

    def post(self, req: Request):
        if not req.path.startswith('/api/coordination/'):
            return None
        if not req.caller():
            return {'ok': False, 'status': 401, 'error': 'Use the existing Hub identity'}
        op = req.path.rsplit('/', 1)[-1]
        if op not in ('repository', 'bind', 'resources'):
            return {'ok': False, 'status': 404, 'error': 'Unknown coordination operation'}
        return getattr(self, op)(dict(req.body or {}))

    @classmethod
    def tools(cls):
        properties = {'space_id': {'type': 'string'}, 'owner': {'type': 'string'}, 'group_owner': {'type': 'string'},
                      'kind': {'type': 'string', 'enum': ['council', 'groups']}, 'id': {'type': 'string'},
                      'viewer_id': {'type': 'string'}, 'since_id': {'type': 'string'}}
        tools = [{'name': 'hub_coordination_' + name, 'description': description,
                 'inputSchema': {'type': 'object', 'properties': properties, 'required': required},
                 'annotations': {'readOnlyHint': True}}
                for name, description, required in (
                    ('config', 'Read the canonical coordination authority and native execution binding.', []),
                    ('rooms', 'List canonical Council and working-group sessions in one Agora space.', []),
                    ('room', 'Read the canonical room and participants without copying its state.', ['kind', 'id']),
                    ('ledger', 'Read actual tasks, claims, objections, decisions or group turn outcomes.', ['kind', 'id']),
                    ('messages', 'Read canonical room messages with participant visibility and native outcome metadata.', ['kind', 'id']))]
        tools.extend([
            {'name': 'hub_coordination_resources', 'description': 'Claim, inspect, release or renew native room resources in the globally shared Agora reservation service.',
             'inputSchema': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['claim', 'check', 'release', 'renew']},
                 'owner': {'type': 'string'}, 'resources': {'type': 'array', 'items': {'type': 'string'}}, 'workspace': {'type': 'string'}},
                 'required': ['action', 'owner']}, 'annotations': {'readOnlyHint': False}},
            {'name': 'hub_coordination_bind', 'description': 'Import legacy local Council/group records once into canonical Hub storage. Originals are read-only and retained.',
             'inputSchema': {'type': 'object', 'properties': {'sources': {'type': 'object', 'properties': {
                 'council': {'type': 'string'}, 'groups': {'type': 'string'}}}}, 'required': ['sources']},
             'annotations': {'readOnlyHint': False}},
        ])
        return tools

    def handlers(self):
        return {'hub_coordination_' + name: getattr(self, name)
                for name in ('config', 'rooms', 'room', 'ledger', 'messages', 'bind', 'resources')}
