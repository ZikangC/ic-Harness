"""Pure evidence of complete tool exposure, never of semantic understanding.

``verify_readback(raw_jsonl, session, packet)`` accepts the original bytes of one
actual OMP session. The caller supplies trusted context identity and the SHA of
that same stream, plus the adopted packet's immutable file index. No live files,
server state, model prose, tool arguments or ``details`` are evidence of content
being returned. Only successful paired ``rtl_remote`` public text results count.
"""
import hashlib
import json
import re
from pathlib import PurePosixPath


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _path(value):
    return (isinstance(value, str) and bool(value) and '\0' not in value
            and not value.startswith(('/', '@')) and '..' not in value.split('/')
            and str(PurePosixPath(value)) == value and value != '.')


def _integer(value):
    return type(value) is int and value >= 0


def _ranges(chunks, size):
    merged = []
    for start, end, _ in sorted(chunks):
        if start == end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    gaps = []; cursor = 0
    for start, end in merged:
        if start > cursor:
            gaps.append([cursor, start])
        cursor = end
    if cursor < size:
        gaps.append([cursor, size])
    return merged, gaps


def verify_readback(events_jsonl, session, packet):
    """Return a body-free witness; malformed or mismatched evidence is incomplete.

    Required session keys: run_id, session_id, role, scope, completed,
    tool_isolation_ok, events_sha256. ``role`` is the trusted context role,
    never the model-selection role. Packet keys: run_id, parent_session_id,
    baseline_path/sha256, handoff_path/sha256, required_files (a nonempty list
    of resolved_run_path, sha256, bytes). Trusted callers must bind that complete
    required-file index to the adopted packet; this function does not infer it.
    """
    errors = []; rejected = 0; ignored = 0; page_count = 0
    def error(code):
        if code not in errors:
            errors.append(code)
    if isinstance(events_jsonl, str):
        try:
            raw = events_jsonl.encode('utf-8')
        except UnicodeError:
            raw = b''; error('invalid_events_encoding')
    elif isinstance(events_jsonl, bytes):
        raw = events_jsonl
    else:
        raw = b''; error('invalid_events_type')
    events_sha = hashlib.sha256(raw).hexdigest()
    result = {'version': 1, 'complete': False, 'reasons': errors,
              'events_sha256': events_sha, 'session': {}, 'packet': {}, 'files': [],
              'boundary': 'Complete successful tool exposure only; not semantic understanding, RTL correctness or physical acceptance.'}
    if not isinstance(session, dict):
        error('invalid_session_metadata'); return result
    session_keys = ('run_id', 'session_id', 'role', 'scope', 'completed', 'tool_isolation_ok', 'events_sha256')
    result['session'] = {key: session.get(key) for key in session_keys}
    if (not all(isinstance(session.get(key), str) and session[key] for key in ('run_id', 'session_id'))
            or session.get('role') != 'implementation' or session.get('scope') != 'work'
            or session.get('completed') is not True or session.get('tool_isolation_ok') is not True):
        error('ineligible_parent_session')
    if not _sha(session.get('events_sha256')) or session['events_sha256'] != events_sha:
        error('session_events_digest_mismatch')
    if not isinstance(packet, dict):
        error('invalid_packet_metadata'); return result
    packet_keys = ('run_id', 'parent_session_id', 'baseline_path', 'baseline_sha256', 'handoff_path', 'handoff_sha256')
    result['packet'] = {key: packet.get(key) for key in packet_keys}
    if (packet.get('run_id') != session.get('run_id')
            or packet.get('parent_session_id') != session.get('session_id')):
        error('parent_session_binding_mismatch')
    if (not _path(packet.get('baseline_path')) or not _path(packet.get('handoff_path'))
            or not _sha(packet.get('baseline_sha256')) or not _sha(packet.get('handoff_sha256'))):
        error('invalid_packet_pins')
    required = packet.get('required_files'); files = {}
    if not isinstance(required, list) or not required:
        error('invalid_required_file_index'); return result
    for row in required:
        if (not isinstance(row, dict) or not _path(row.get('resolved_run_path'))
                or not _sha(row.get('sha256')) or not _integer(row.get('bytes'))):
            error('invalid_required_file_index'); continue
        name = row['resolved_run_path']
        if name == packet.get('handoff_path') and row['sha256'] != packet.get('handoff_sha256'):
            error('required_handoff_pin_mismatch'); continue
        if name in files:
            error('duplicate_required_file'); continue
        files[name] = {'resolved_run_path': name, 'sha256': row['sha256'],
                       'bytes': row['bytes'], 'chunks': [], 'rejected_pages': 0}
    if errors:
        return result
    calls = {}; ended = set()
    try:
        # JSONL is delimited by LF, not by Unicode line separators that may
        # legally occur unescaped inside a JSON string returned by the tool.
        lines = raw.decode('utf-8').split('\n')
    except UnicodeError:
        error('invalid_events_encoding'); return result
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            error('malformed_event_json'); continue
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            error('unsupported_event_shape'); continue
        kind = event['type']
        if kind not in ('tool_execution_start', 'tool_execution_end'):
            if kind.startswith('tool_execution_'):
                error('unsupported_tool_event')
            continue  # Assistant prose and other normal lifecycle events are not read evidence.
        call_id = event.get('toolCallId'); tool = event.get('toolName')
        if not isinstance(call_id, str) or not call_id or not isinstance(tool, str) or not tool:
            error('unsupported_tool_event'); continue
        if 'session_id' in event and event['session_id'] != session['session_id']:
            error('event_session_mismatch'); continue
        if kind == 'tool_execution_start':
            if call_id in calls or call_id in ended:
                error('duplicate_tool_call_id'); continue
            if tool == 'rtl_remote' and not isinstance(event.get('args'), dict):
                error('unsupported_tool_arguments'); continue
            calls[call_id] = (tool, event.get('args'))
            continue
        if call_id in ended:
            error('duplicate_tool_call_id'); continue
        ended.add(call_id)
        start = calls.pop(call_id, None)
        if start is None or start[0] != tool:
            error('unpaired_tool_result'); continue
        if tool != 'rtl_remote' or start[1].get('action') != 'read':
            continue
        public = event.get('result')
        if event.get('isError') is not False or not isinstance(public, dict) or public.get('isError', False) is not False:
            rejected += 1; continue
        content = public.get('content')
        if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
                or content[0].get('type') != 'text' or not isinstance(content[0].get('text'), str)):
            error('unsupported_public_read_result'); rejected += 1; continue
        try:
            page = json.loads(content[0]['text'])
        except (ValueError, TypeError):
            error('unsupported_public_read_result'); rejected += 1; continue
        if not isinstance(page, dict) or page.get('error'):
            rejected += 1; continue
        name = page.get('resolved_run_path')
        if name is None and 'resolved_run_path' in page:
            ignored += 1; continue  # Explicitly out-of-run sources never credit packet coverage.
        if not _path(name):
            error('missing_canonical_read_path'); rejected += 1; continue
        if name not in files:
            ignored += 1; continue
        item = files[name]; span = page.get('returned_range'); body = page.get('content')
        if (page.get('sha256') != item['sha256'] or type(page.get('bytes')) is not int
                or page['bytes'] != item['bytes'] or not isinstance(span, dict)
                or not _integer(span.get('byte_start')) or not _integer(span.get('byte_end'))
                or not span['byte_start'] <= span['byte_end'] <= item['bytes']
                or not isinstance(body, str) or type(page.get('eof')) is not bool
                or page['eof'] != (span['byte_end'] == item['bytes'])):
            item['rejected_pages'] += 1; rejected += 1; continue
        try:
            body = body.encode('utf-8')
        except UnicodeError:
            item['rejected_pages'] += 1; rejected += 1; continue
        start, end = span['byte_start'], span['byte_end']
        if len(body) != end - start or (start == end and item['bytes'] != 0):
            item['rejected_pages'] += 1; rejected += 1; continue
        if any(body[max(start, a)-start:min(end, b)-start] != old[max(start, a)-a:min(end, b)-a]
               for a, b, old in item['chunks'] if max(start, a) < min(end, b)):
            error('conflicting_exposed_bytes'); item['rejected_pages'] += 1; rejected += 1; continue
        item['chunks'].append((start, end, body)); page_count += 1
    if calls:
        error('unfinished_tool_calls')
    for name in sorted(files):
        item = files[name]; chunks = item.pop('chunks'); ranges, gaps = _ranges(chunks, item['bytes'])
        complete = bool(chunks) and not gaps
        exposed_digest = None
        if complete:
            cursor = 0; digest = hashlib.sha256()
            for start, end, body in sorted(chunks):
                if end > cursor:
                    digest.update(body[max(0, cursor-start):]); cursor = end
            exposed_digest = digest.hexdigest()
            if exposed_digest != item['sha256']:
                complete = False; error('exposed_content_digest_mismatch')
        item.update(ranges=ranges, gaps=gaps, successful_pages=len(chunks),
                    covered_bytes=sum(end-start for start, end in ranges), complete=complete,
                    exposed_sha256=exposed_digest)
        result['files'].append(item)
    if any(not item['complete'] for item in result['files']):
        error('required_content_not_fully_exposed')
    result.update(successful_read_pages=page_count, rejected_read_pages=rejected,
                  ignored_unrequired_read_pages=ignored, complete=not errors and all(item['complete'] for item in result['files']))
    return result


def main():
    import sys
    value = json.load(sys.stdin)
    print(json.dumps(verify_readback(value.get('events_jsonl'), value.get('session'), value.get('packet')), sort_keys=True))


if __name__ == '__main__':
    main()
