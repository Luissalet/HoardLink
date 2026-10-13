from hoard_link.hub.agora import Agora
from hoard_link.hub.coordination_resources import operate, resource_key


def test_native_rooms_share_the_existing_global_locks_with_development(tmp_path):
    agora = Agora(tmp_path / 'agora.db')
    try:
        root = tmp_path / 'repo'
        other = tmp_path / 'worktree'
        roots = [('Project', root), ('Project', other)]
        assert resource_key(agora, 'file:src/a.py', str(other), roots) == 'path:project/src/a.py'
        assert agora.lock({'agent': 'developer', 'resource': 'path:Project/src'})['ok']
        blocked = operate(agora, {'owner': 'native:member', 'action': 'claim', 'workspace': str(other),
                                 'resources': ['file:src/a.py', 'file:tests/b.py']}, roots)
        assert not blocked['ok']
        assert not any(r['owner'] == 'native:member' for r in agora.locks()['locks'])
        agora.unlock({'agent': 'developer', 'resource': 'path:Project/src'})
        assert operate(agora, {'owner': 'native:member', 'action': 'claim', 'workspace': str(root),
                              'resources': ['file:src/a.py']}, roots)['ok']
        assert not agora.for_space('ecosystem').lock({'agent': 'another', 'resource': 'path:Project/src/a.py'})['ok']
        assert operate(agora, {'owner': 'another-room', 'action': 'check', 'workspace': str(other),
                              'resources': ['file:src']}, roots)['conflicts'][0]['owner'] == 'native:member'
        operate(agora, {'owner': 'native:member', 'action': 'release', 'workspace': str(root), 'resources': ['file:src/a.py']}, roots)
        assert not agora.locks()['locks']
    finally:
        agora.close()


def test_unregistered_absolute_parent_paths_and_renewal_are_global(tmp_path):
    clock = [1000.0]
    agora = Agora(tmp_path / 'agora.db', clock=lambda: clock[0])
    try:
        args = {'owner': 'native:one', 'workspace': str(tmp_path), 'resources': ['file:work'], 'action': 'claim'}
        assert operate(agora, args)['ok']
        assert not operate(agora, {**args, 'owner': 'native:two', 'resources': ['file:work/a.txt']})['ok']
        before = agora.locks()['locks'][0]['expires']
        clock[0] += 500
        operate(agora, {'owner': 'native:one', 'action': 'renew'})
        assert agora.locks()['locks'][0]['expires'] == before + 500
        operate(agora, {**args, 'owner': 'native:two', 'action': 'release'})
        assert len(agora.locks()['locks']) == 1
    finally:
        agora.close()


def test_linked_worktree_uses_registered_repo_without_git_processes(tmp_path):
    main, tree = tmp_path/'main', tmp_path/'tree'
    (main/'.git/worktrees/tree').mkdir(parents=True)
    tree.mkdir()
    (tree/'.git').write_text('gitdir: '+str(main/'.git/worktrees/tree'), encoding='utf-8')
    (main/'.git/worktrees/tree/commondir').write_text('../..', encoding='utf-8')
    agora = Agora(tmp_path/'agora.db')
    try:
        assert resource_key(agora, 'file:src/a.py', str(tree), [('Project', main)]) == 'path:project/src/a.py'
    finally:
        agora.close()
