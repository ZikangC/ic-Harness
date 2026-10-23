"""Persistent job/time admission for caller-selected readiness RTL repairs.

The controller's source-bound repair receipt selects this policy. A model cannot
opt out by omitting a bridge flag. Each submitted job permanently reserves its
configured timeout; arbitrary helper scripts do not have a variant-count claim.
"""
from contextlib import contextmanager
import fcntl
import json

from harness.runtime.rtl_readiness import settings


ACTOR = 'rtl-implementation-readiness'


def binding(out, review):
    """Return a readiness repair policy only for its active controller identity."""
    if not review or (review.get('repair') or {}).get('status') != 'active':
        return None
    path = out / 'control/rtl-audit' / (review['review'] + '.repair.json')
    if not path.is_file():
        return None
    receipt = json.loads(path.read_text())
    if receipt.get('repair_actor') != ACTOR:
        return None
    decision = review.get('decision') or {}
    if (decision.get('verdict') != 'revise'
            or receipt.get('candidate_sha256') != review['candidate_sha256']
            or receipt.get('decision_report_sha256') != decision.get('report_sha256')):
        raise ValueError('readiness repair budget has stale controller candidate/report binding')
    limits = settings(out)
    return {'repair_actor': ACTOR, 'review': review['review'],
            'candidate_sha256': review['candidate_sha256'],
            'decision_report_sha256': decision['report_sha256'],
            'budgets': {key: limits[key] for key in
                        ('max_jobs', 'max_job_seconds', 'tool_seconds')}}


def _usage(out, value):
    count = seconds = 0
    for path in (out / 'jobs').glob('*/request.json'):
        request = json.loads(path.read_text())
        if request.get('repair_review') != value['review']:
            continue
        timeout = request.get('timeout_seconds')
        if type(timeout) is not int or timeout <= 0:
            raise ValueError('readiness repair has an invalid existing job timeout')
        saved = request.get('repair_budget')
        if saved is not None and any(saved.get(key) != value[key] for key in value):
            raise ValueError('readiness repair job has stale budget binding')
        count += 1
        seconds += timeout
    return count, seconds


@contextmanager
def submit_reservation(out, review, value, timeout):
    """Lock budget admission until the caller has persisted the new job request."""
    path = out / 'control/rtl-audit' / (review['review'] + '.repair-budget.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        current = binding(out, review)
        if current != value:
            raise ValueError('readiness repair controller identity changed during submit')
        limits = value['budgets']
        if type(timeout) is not int or timeout <= 0 or timeout > limits['max_job_seconds']:
            raise ValueError('readiness repair per-job timeout budget exceeded')
        count, seconds = _usage(out, value)
        if count >= limits['max_jobs'] or seconds + timeout > limits['tool_seconds']:
            raise ValueError('readiness repair native submit budget exhausted')
        yield value | {'reserved_seconds': timeout}
