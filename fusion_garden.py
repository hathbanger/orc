"""Opt-in drafting and unanimous council approval, with persisted policy and progress."""
import contextlib
import fcntl
from pathlib import Path
import time

from fusion_learning import decision_rows, read_object
from fusion_decisions import digest

DEFAULTS = {'enabled': False, 'agent': 'auto', 'since_ms': 0,
            'labeling_mode': 'single', 'council_agents': [], 'council_rule': 'unanimous', 'approval_mode': 'human', 'approval_since_ms': 0}


@contextlib.contextmanager
def locked(workspace, name='garden'):
    path = Path(workspace) / '.fusion/decisions' / (name + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def settings(workspace):
    value = {**DEFAULTS, **read_object(Path(workspace) / '.fusion/decisions/garden.json')}
    # Legacy limits no longer stop an enabled garden, including after a restart.
    value.pop('daily_limit', None)
    return value


def save(app, workspace, body):
    from fusion_ui import atomic_json
    if type(body.get('enabled')) is not bool:
        raise ValueError('Choose whether automatic drafting is enabled')
    from fusion_labeling import labeling_options, approval_options, council_rule, member_lane, _local_config
    agent = body.get('agent', 'auto')
    config = _local_config(workspace)
    if agent != 'auto':
        member_lane(config, agent)
    with app.lock, locked(workspace):
        old = settings(workspace)
        options = labeling_options(body.get('labeling_mode', old['labeling_mode']),
                                   body.get('council_agents', old['council_agents']), config)
        approval = approval_options(body.get('approval_mode', old['approval_mode']), options['labeling_mode'])
        rule = council_rule(body.get('council_rule', old['council_rule']))
        since = old['since_ms'] if old.get('configured') else int(time.time() * 1000)
        approval_since = old['approval_since_ms'] if old['approval_mode'] == approval and old['council_rule'] == rule else int(time.time() * 1000)
        if body.get('include_existing') is True:
            since = 0
            approval_since = 0
        value = {'enabled': body['enabled'], 'agent': agent, **options, 'council_rule': rule,
                 'since_ms': since, 'configured': True, 'approval_mode': approval, 'approval_since_ms': approval_since}
        value['policy_id'] = digest({key: value[key] for key in ('approval_mode', 'approval_since_ms', 'labeling_mode', 'council_agents', 'council_rule')})
        atomic_json(Path(workspace) / '.fusion/decisions/garden.json', value)
    return status(app, workspace)


def status(app, workspace, rows=None):
    value = settings(workspace)
    jobs = app.jobs(workspace, limit=None)
    now = int(time.time() * 1000)
    garden_jobs = [j for j in jobs if j.get('garden')]
    attempted = {j.get('decision_id') for j in jobs if j.get('action') == 'suggest-labels'}
    automatic = value['approval_mode'] == 'council'
    attempted_policy = {j.get('decision_id') for j in jobs if j.get('action') == 'suggest-labels' and j.get('garden_policy') == value.get('policy_id')}
    queue = []
    for row in (decision_rows(workspace) if rows is None else rows):
        if row.get('time_ms', 0) < max(value['since_ms'], value['approval_since_ms'] if automatic else 0):
            continue
        if automatic:
            human_reviewed = any(e.get('verified') and e.get('source') not in {'council_approved_suggestion', 'structural_gate', 'gym_grade'}
                                 for e in row.get('labels', []))
            eligible = row['garden_state'] in {'needs_draft', 'needs_review', 'needs_evidence', 'needs_attention'} and not human_reviewed
            if eligible and row['id'] not in attempted_policy:
                queue.append(row)
        elif row['garden_state'] == 'needs_draft' and row['id'] not in attempted:
            queue.append(row)
    used = sum(j.get('started_at_ms', 0) // 86400000 == now // 86400000 for j in garden_jobs)
    active = next((j for j in jobs if j.get('action') == 'suggest-labels' and j.get('status') in {'queued', 'running', 'stopping'}), None)
    latest = garden_jobs[0] if garden_jobs else None
    state = 'paused' if not value['enabled'] else 'drafting' if active else 'waiting' if not queue else 'queued'
    return {**value, 'state': state, 'queued': len(queue), 'used_today': used,
            'active_job': active, 'latest_job': latest, 'next_id': queue[-1]['id'] if queue else None}


def tick(app, workspace):
    # Disabled workspaces stay read-only and do not acquire/create any artifacts.
    if not settings(workspace)['enabled']:
        return
    with app.lock, locked(workspace):
        current = status(app, workspace)
        if current['state'] != 'queued':
            return
        app.launch(workspace, {'action': 'suggest-labels', 'decision_id': current['next_id'],
                               'agent': current['agent'], 'labeling_mode': current['labeling_mode'],
                               'council_agents': current['council_agents'], 'approval_mode': current['approval_mode'],
                               'council_rule': current['council_rule'],
                               'garden_policy': current.get('policy_id')}, garden=True)
