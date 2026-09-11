#!/usr/bin/env python3
"""Read-only case gate, layer resolver and source-version checker. Never edits media."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys


class CaseError(ValueError):
    pass


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise CaseError(message)


def contained(root, relative):
    require(isinstance(relative, str) and bool(relative), 'Missing relative path')
    posix, win = PurePosixPath(relative), PureWindowsPath(relative)
    require(not posix.is_absolute() and not win.is_absolute() and not win.drive
            and '..' not in posix.parts and '..' not in win.parts and '\\' not in relative,
            'Use a portable relative path without traversal or drive letters')
    root = Path(root).resolve()
    result = (root / relative).resolve()
    require(result.is_relative_to(root), 'Path escapes selected root')
    return result


def load_layers(case, skill_root):
    catalog = read(skill_root / 'references/catalog.json')
    cid, tid = case.get('client_id'), case.get('type_id')
    client = catalog['clients'].get(cid)
    require(client is not None, 'Unknown client: propose and confirm a new catalog entry first')
    typ = client['types'].get(tid)
    require(typ is not None, 'Unknown type for this client; never borrow another client profile')
    path = ['影片剪輯', client['name'], typ['name']]
    require(case.get('classification', {}).get('path') == path, 'Classification path disagrees with IDs')
    paths = ['SKILL.md', 'references/workflow.md', 'references/common.json', client['client'], typ['path']]
    active, pending, seen, refs = [], [], set(), []
    for relative in paths:
        p = contained(skill_root, relative)
        require(p.is_file(), f'Missing required layer: {relative}')
        if p.suffix != '.json':
            continue
        layer = read(p)
        if 'client_id' in layer:
            require(layer['client_id'] == cid, 'Cross-client layer rejected')
        if 'type_id' in layer:
            require(layer['type_id'] == tid, 'Cross-type layer rejected')
        for rule in layer.get('rules', []):
            require(rule['id'] not in seen, 'Duplicate rule ID; reconcile primary ownership')
            seen.add(rule['id'])
            require(rule.get('sources') and rule.get('scope') and rule.get('primary'), 'Rule lacks provenance or ownership')
            require(contained(skill_root, rule['primary']).is_file(), 'Missing rule primary')
            state = rule.get('status')
            require(state in ('confirmed', 'starting_preference', 'candidate', 'conflict', 'superseded'), 'Invalid rule status')
            (active if state in ('confirmed', 'starting_preference') else pending).append(rule)
        for ref in layer.get('external_refs', []):
            # Cross-skill references allowed only inside canonical skills directory.
            target = (skill_root / ref['path']).resolve()
            require(target.is_relative_to(skill_root.parent.resolve()) and target.is_file(), 'Missing or escaped external primary')
            refs.append(ref)
    return {'classification_path': path, 'subtitle_skill': catalog.get('defaults', {}).get('subtitle_skill'),
            'read_order': paths, 'active_rules': active,
            'inactive_rules': pending, 'conditional_external_refs': refs}


def identity(case):
    require(case.get('schema_version') == 1, 'Unsupported case schema')
    for key in ('case_id', 'episode_id', 'title', 'client_id', 'type_id', 'version'):
        require(isinstance(case.get(key), str) and case[key].strip(), f'Missing {key}')
    require(case.get('status') in ('intake', 'in_progress', 'delivered', 'final'), 'Invalid case status')
    require(isinstance(case.get('scope'), dict) and isinstance(case['scope'].get('stages'), list), 'Missing authorized scope')


def confirmed(case):
    c = case.get('classification', {})
    require(c.get('status') == 'confirmed', 'Classification awaits a real user answer; do not process media')
    answer = c.get('confirmation')
    require(isinstance(answer, dict), 'Missing classification confirmation')
    for key in ('case_id', 'client_id', 'type_id'):
        require(answer.get(key) == case[key], 'Confirmation belongs to a different case/client/type')
    require(answer.get('path') == c['path'], 'Confirmation names a different classification')
    for key in ('quote', 'source', 'at'):
        require(isinstance(answer.get(key), str) and answer[key].strip(), f'Missing user confirmation {key}')
    if case['status'] == 'final':
        f = case.get('final_confirmation', {})
        require(f.get('case_id') == case['case_id'] and f.get('version') == case['version'], 'Final approval must identify this exact case/version')
        require(all(isinstance(f.get(k), str) and f[k].strip() for k in ('quote', 'source', 'at')), 'Delivery is not human final approval')


def verify_sources(case, workspace, bindings):
    require(isinstance(bindings, dict) and 'workspace' not in bindings, 'Bindings cannot override workspace')
    roots = {'workspace': Path(workspace), **{k: Path(v).expanduser() for k, v in bindings.items()}}
    require(all(p.is_absolute() for p in roots.values()), 'Root bindings must be absolute on this device')
    sources = case.get('sources')
    require(isinstance(sources, list) and bool(sources), 'No registered case sources')
    verified = {}
    for source in sources:
        sid = source.get('id')
        require(isinstance(sid, str) and sid and sid not in verified, 'Missing or duplicate source ID')
        role = source.get('role')
        require(role in ('media', 'timeline', 'audio', 'transcript', 'reference', 'evidence'), 'Invalid source role')
        require(role == 'reference' or source.get('case_id') == case['case_id'], 'Cross-episode source rejected')
        require(source.get('version'), 'Source version required')
        root = roots.get(source.get('root'))
        require(root is not None, 'Missing device root binding')
        path = contained(root, source.get('path'))
        require(path.is_file(), f'Missing source: {sid}')
        actual = digest(path)
        require(actual == source.get('sha256'), f'Source changed: {sid}; register a revision, never reuse old timing')
        verified[sid] = actual
    for artifact in case.get('artifacts', []):
        require(artifact.get('id') and artifact.get('path'), 'Artifact needs id and path')
        require(artifact.get('status') in ('draft', 'delivered', 'stale', 'final'), 'Invalid artifact status')
        require(artifact['status'] != 'stale', f'Stale artifact: {artifact["id"]}')
        require(isinstance(artifact.get('depends_on'), dict) and artifact['depends_on'], 'Artifact dependencies required')
        for sid, expected in artifact['depends_on'].items():
            require(sid in verified and verified[sid] == expected, f'Stale artifact dependency: {artifact["id"]}/{sid}')
    return verified


def evaluate(case, skill_root, workspace=None, bindings=None, inspect=False):
    identity(case)
    layers = load_layers(case, Path(skill_root))
    if inspect:
        return {'status': 'metadata-only', 'classification_status': case['classification'].get('status'),
                'media_verified': False, **layers}
    confirmed(case)  # Gate before reading any source file.
    require(workspace is not None, '--workspace is required for check')
    verified = verify_sources(case, workspace, bindings or {})
    return {'status': 'context-verified', 'user_answer_authenticity': 'agent-must-check-original-message',
            'editing_executed': False, 'source_hashes': verified, **layers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect', 'check'])
    parser.add_argument('case', type=Path)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--bindings', type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(read(args.case), Path(__file__).resolve().parents[1], args.workspace,
                          read(args.bindings) if args.bindings else {}, args.command == 'inspect')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (CaseError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
