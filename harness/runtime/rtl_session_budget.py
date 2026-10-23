"""Controller-owned model budget feedback; never an authority for completion.

The local sidecar is written only by the parent model controller. Remote RTL
file/job tools cannot address it. Bridge processes read it at reply time, so a
long status observation does not return a stale remaining wall-clock budget.
"""
import json
import math
import os
from pathlib import Path
import tempfile
import time


def write_budget(path, session_id, limits, turns, started_monotonic):
    """Atomically publish one assistant-turn observation for this session."""
    value = {'version': 1, 'session_id': session_id,
             'assistant_turns_used': turns,
             'assistant_turns_limit': limits['assistant_turns'],
             'session_seconds_limit': limits['session_seconds'],
             'started_monotonic': started_monotonic}
    target = Path(path)
    # mkstemp creates a private file; replacement never exposes a partial JSON.
    fd, temporary = tempfile.mkstemp(prefix=target.name + '.', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def decorate(context, result, action):
    """Add advisory session_budget to a tool reply, leaving its payload intact.

The bridge supplies trusted context, never a model-supplied sidecar path.
Missing or malformed feedback is omitted rather than making an otherwise
completed remote action fail. Neither phase nor remaining budget authorizes a
handoff, a new job, or adoption; the controller/native guards own those checks.
    """
    path = context.get('session_budget_path')
    if not path or not isinstance(result, dict):
        return result
    try:
        state = json.loads(Path(path).read_text())
        if state.get('version') != 1 or state.get('session_id') != context.get('session_id'):
            return result
        turns = state['assistant_turns_used']
        limit = state['assistant_turns_limit']
        seconds = state['session_seconds_limit']
        started = state['started_monotonic']
        if (type(turns) is not int or turns < 0 or type(limit) is not int or limit <= 0
                or type(seconds) is not int or seconds <= 0
                or type(started) not in (int, float) or not math.isfinite(started)):
            return result
        elapsed = max(0, time.monotonic() - started)
        remaining_turns = max(0, limit - turns)
        remaining_seconds = max(0, seconds - elapsed)
        reserve_turns = min(8, max(1, math.ceil(limit / 10)))
        reserve_seconds = min(60, max(1, math.ceil(seconds / 10)))
        closing = remaining_turns <= reserve_turns or remaining_seconds <= reserve_seconds
        metadata = {'assistant_turns_used': turns,
                    'assistant_turns_remaining': remaining_turns,
                    'session_seconds_remaining': round(remaining_seconds, 3),
                    'phase': 'closing' if closing else 'investigation',
                    'close_reserve_turns': reserve_turns,
                    'close_reserve_seconds': reserve_seconds}
        if closing:
            metadata['closing_instruction'] = (
                'Return the ordinary final diagnosis JSON now and end normally; '
                'preserve uncertainty and avoid new investigation. No file write or handoff is needed.'
                if context.get('role') == 'readiness_diagnosis' else
                'Finish the evidence and required delivery now; avoid new investigations. '
                'If a finalization job has completed, use handoff with its job_id; '
                'a successful verified handoff ends this actor without extra reads or prose.'
                if context.get('role') == 'rtl_readiness' else
                'Finish the required report or repair delivery now; avoid new investigations. '
                'Write any required report before ending this session normally.')
        return dict(result, session_budget=metadata)
    except (OSError, ValueError, KeyError, TypeError):
        return result
