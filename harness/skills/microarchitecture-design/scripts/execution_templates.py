"""Bounded expansion of declared physical schedules, not an algorithm compiler.

Loops, values, dependencies and counts are designer declarations.  Expansion
checks their consequences; independent audit must establish source coverage.
"""
import copy
import re

MAX_EVENTS = 200000
MAX_ITERATIONS = 65536
MAX_PHASES = 256
MAX_LOOPS = 8
_ID = re.compile(r'\$\{([A-Za-z_][A-Za-z_0-9]*)\}')
_NUMERIC = {'instance', 'word_address', 'cycle', 'lane', 'bit_offset', 'width',
            'ready_cycle', 'start', 'end', 'source_bit_offset', 'min_latency'}


def expand(candidate, values, requirements, expression, inventory):
    errors, unknown, results, windows = [], [], [], []
    groups = {r['id']: r for r in candidate.get('cost_groups', [])}
    profiles = {r['id'] for r in candidate.get('workloads', [])}
    buffers = {r['id']: r for r in candidate.get('buffers', [])}
    rules = {r['kind']: r for r in requirements.get('storage_rules', [])}
    seen = set()

    def integer(value, env, minimum=0):
        value = expression(value, env)
        if type(value) not in (int, float) or value != int(value) or value < minimum:
            raise ValueError('integer >= ' + str(minimum) + ' required')
        return int(value)

    def basis(row):
        if not isinstance(row.get('basis'), str) or not row['basis'].strip():
            raise ValueError('nonempty basis required')

    rows = candidate.get('execution_templates', [])
    if not isinstance(rows, list):
        return {'ok': False, 'errors': ['execution_templates must be an array'],
                'unknown': [], 'templates': [], 'windows': []}
    for template in rows:
        label = str(template.get('id', '?')) if isinstance(template, dict) else '?'
        try:
            if not isinstance(template, dict) or not isinstance(template.get('id'), str) or not template.get('id') or label in seen:
                raise ValueError('unique nonempty template id required')
            seen.add(label); basis(template)
            if template.get('model') == 'unsupported':
                if not template.get('reason'): raise ValueError('unsupported template needs reason')
                unknown.append(label + ': ' + template['reason'])
                results.append({'id': label, 'status': 'unverified', 'reason': template['reason']})
                continue
            if template.get('workload') not in profiles or not template.get('repetition_basis'):
                raise ValueError('actual workload and source-count/repetition basis required')
            env = dict(values)
            params = template.get('parameters', {})
            if not isinstance(params, dict): raise ValueError('parameters must be an object')
            for name, value in params.items():
                if not name.isidentifier() or name in env or name == 'iteration':
                    raise ValueError('parameter must be a fresh arithmetic identifier')
                env[name] = integer(value, env)
            phases = template.get('phases')
            if not isinstance(phases, list) or not phases or len(phases) > MAX_PHASES:
                raise ValueError('phases needs 1..' + str(MAX_PHASES) + ' entries')
            expanded = {key: [] for key in ('reads', 'writes', 'uses', 'local_uses', 'issues')}
            retentions, dependencies, components = [], [], []
            phase_ids, total, event_count = set(), 0, 0

            def expand_row(row, names):
                nonlocal event_count
                if not isinstance(row, dict): raise ValueError('generator row must be an object')
                loops = row.get('for_each', [])
                if not isinstance(loops, list) or len(loops) > MAX_LOOPS:
                    raise ValueError('for_each must be a bounded loop array')
                def visit(index, scope):
                    nonlocal event_count
                    if index == len(loops):
                        event_count += 1
                        if event_count > MAX_EVENTS:
                            raise ValueError('expanded schedule exceeds ' + str(MAX_EVENTS) + ' events (tool limit; not hardware infeasibility)')
                        yield ({k: copy.deepcopy(v) for k, v in row.items() if k != 'for_each'}, scope)
                        return
                    loop = loops[index]
                    if not isinstance(loop, dict) or set(loop) - {'index', 'count', 'start', 'step'}:
                        raise ValueError('loop requires index/count and optional start/step')
                    name = loop['index']
                    if not isinstance(name, str) or not name.isidentifier() or name in scope:
                        raise ValueError('loop index must be a fresh identifier')
                    count = integer(loop['count'], scope)
                    if count > MAX_ITERATIONS: raise ValueError('loop count exceeds execution-size limit')
                    start = integer(loop.get('start', 0), scope)
                    step = integer(loop.get('step', 1), scope, 1)
                    for i in range(count):
                        nested = dict(scope); nested[name] = start + i * step
                        yield from visit(index + 1, nested)
                yield from visit(0, names)

            def render(value, names, key=None):
                if key in _NUMERIC: return integer(value, names, 1 if key == 'width' else 0)
                if isinstance(value, dict): return {k: render(v, names, k) for k, v in value.items()}
                if isinstance(value, list): return [render(v, names) for v in value]
                if isinstance(value, str):
                    def sub(match):
                        if match[1] not in names: raise ValueError('undefined interpolation index ' + match[1])
                        return str(integer(names[match[1]], names))
                    return _ID.sub(sub, value)
                return value

            def retention_rows(rows, names, prefix, offset):
                if not isinstance(rows, list): raise ValueError('retentions must be an array')
                for raw in rows:
                    for raw_row, scope in expand_row(raw, names):
                        item = render(raw_row, scope); basis(item)
                        if not isinstance(item.get('id'), str) or not item['id']:
                            raise ValueError('retention id required')
                        item['id'] = prefix + item['id']
                        item['start'] += offset; item['end'] += offset
                        if 'read_id' in item: item['read_id'] = prefix + item['read_id']
                        retentions.append(item)
                        if len(retentions) > 4096:
                            raise ValueError('retentions exceed 4096 live-value declarations (execution-size limit)')

            retention_rows(template.get('retentions', []), env, label + '/', 0)
            for phase in phases:
                if not isinstance(phase, dict): raise ValueError('phase must be an object')
                pid = phase['id']; basis(phase)
                if not isinstance(pid, str) or not pid or pid in phase_ids:
                    raise ValueError('unique phase id required')
                phase_ids.add(pid)
                body = integer(phase['cycles'], env, 1)
                repeat = integer(phase.get('repeat', 1), env, 1)
                if repeat > MAX_ITERATIONS: raise ValueError('phase repeats exceed execution-size limit')
                covered = phase.get('cost_groups', [])
                if not isinstance(covered, list) or len(covered) != len(set(covered)):
                    raise ValueError('phase cost_groups must be a unique array')
                if any(g not in groups or groups[g]['workload'] != template['workload'] for g in covered):
                    raise ValueError('phase cost group must belong to template workload')
                components.append({'id': pid, 'cycles': body * repeat, 'body_cycles': body,
                                   'iterations': repeat, 'cost_groups': covered,
                                   'first_cycle': total, 'end_cycle': total + body * repeat,
                                   'window_id': label, 'issued_operations': {}})
                phase_arrays = tuple(expanded) + ('retentions', 'dependencies')
                if any(not isinstance(phase.get(key, []), list) for key in phase_arrays):
                    raise ValueError('phase event/retention/dependency fields must be arrays')
                repeated = range(repeat) if any(phase.get(key) for key in phase_arrays) else ()
                for iteration in repeated:
                    scope = dict(env, iteration=iteration)
                    offset = total + iteration * body
                    prefix = label + '/' + pid + '/' + str(iteration) + '/'
                    retention_rows(phase.get('retentions', []), scope, prefix, offset)
                    for kind in expanded:
                        raw_rows = phase.get(kind, [])
                        if not isinstance(raw_rows, list): raise ValueError(kind + ' must be an array')
                        for raw in raw_rows:
                            for item, local in expand_row(raw, scope):
                                # lanes use a bounded arithmetic range, avoiding explicit lane arrays.
                                if kind == 'issues' and isinstance(item.get('lanes'), dict):
                                    lane = item['lanes']
                                    if set(lane) - {'count', 'start', 'step'}: raise ValueError('lane range supports count/start/step only')
                                    count = integer(lane['count'], local)
                                    if count > 65536: raise ValueError('lane range exceeds finite issue limit')
                                    start = integer(lane.get('start', 0), local)
                                    step = integer(lane.get('step', 1), local, 1)
                                    item['lanes'] = list(range(start, start + count * step, step))
                                item = render(item, local)
                                if not 0 <= item.get('cycle', -1) < body:
                                    raise ValueError('event cycle outside phase body; include fill/drain/wait cycles explicitly')
                                item['cycle'] += offset
                                if 'ready_cycle' in item: item['ready_cycle'] += offset
                                for key in ('id', 'read_id'):
                                    if key in item: item[key] = prefix + item[key]
                                if 'retention' in item:
                                    reference = item['retention']
                                    item['retention'] = (label + '/' + reference[1:] if reference.startswith('/') else prefix + reference)
                                expanded[kind].append(item)
                    raw_dependencies = phase.get('dependencies', [])
                    if not isinstance(raw_dependencies, list): raise ValueError('dependencies must be an array')
                    for raw in raw_dependencies:
                        for item, local in expand_row(raw, scope):
                            item = render(item, local); basis(item)
                            for key in ('before', 'after'):
                                reference = item[key]
                                if not isinstance(reference, str) or not reference:
                                    raise ValueError('dependency event reference must be a nonempty string')
                                item[key] = label + '/' + reference[1:] if reference.startswith('/') else prefix + reference
                            dependencies.append(item)
                total += body * repeat
            if not total: raise ValueError('nonzero finite template duration required')
            issued = {}
            for item in expanded['issues']:
                group = item['cost_group']
                operations = len(item['lanes'])
                issued[group] = issued.get(group, 0) + operations
                component = next(c for c in components if c['first_cycle'] <= item['cycle'] < c['end_cycle'])
                counted = component['issued_operations']
                counted[group] = counted.get(group, 0) + operations
                if group not in component['cost_groups']:
                    raise ValueError('issued cost group omitted from phase cost_groups: ' + str(group))

            requests = {}
            events = {}
            for kind in ('reads', 'writes', 'issues'):
                for row in expanded[kind]:
                    rid = row.get('id')
                    if rid:
                        if rid in events: raise ValueError('duplicate expanded event id ' + rid)
                        events[rid] = row['cycle']
                    if kind != 'issues': requests[rid] = dict(row, kind=kind)
            register_values = {}
            live_rows = []
            peak = {}
            for row in retentions:
                rid = row['id']
                if rid in register_values: raise ValueError('duplicate retention id ' + rid)
                hold = buffers.get(row['buffer'])
                if hold is None or hold.get('kind') != 'register': raise ValueError('retention needs actual priced register buffer')
                capacity = integer(hold['count'], env, 1) * integer(hold['depth'], env, 1) * integer(hold['width'], env, 1)
                if row['start'] > row['end'] or row['end'] >= total:
                    raise ValueError('retention live interval outside template')
                if row['bit_offset'] + row['width'] > capacity:
                    raise ValueError('retention slice exceeds actual register buffer capacity')
                if 'read_id' in row:
                    source = requests.get(row['read_id'])
                    if source is None or source['kind'] != 'reads': raise ValueError('retention source needs actual physical read')
                    macro = inventory.get(source['storage_reference'])
                    if macro is None: raise ValueError('retention read needs actual macro')
                    latency = rules.get(macro['kind'], {}).get('read_latency_cycles')
                    if latency is None: unknown.append(label + '/' + rid + ': retention read latency absent')
                    else:
                        ready = source['cycle'] + integer(latency, env)
                        if row['start'] < ready:
                            raise ValueError('retention captures physical read before ready')
                        if row['start'] > ready:
                            unknown.append(label + '/' + rid + ': late capture needs source-backed output stability/intermediate retention from ready to start; unmodeled read-output holding is not established')
                    if row.get('source_bit_offset', 0) + row['width'] > macro['width']:
                        raise ValueError('retention capture exceeds physical read word')
                elif not row.get('producer_basis'):
                    raise ValueError('non-read retention needs producer_basis for independent review')
                register_values[rid] = row
                # Inclusive lifetimes: consuming and replacing on one edge needs a distinct slot.
                for old in live_rows:
                    if old['buffer'] == row['buffer'] and max(old['start'], row['start']) <= min(old['end'], row['end']) and max(old['bit_offset'], row['bit_offset']) < min(old['bit_offset'] + old['width'], row['bit_offset'] + row['width']):
                        raise ValueError('retained register values overlap before last consumer: ' + old['id'] + ' / ' + rid)
                live_rows.append(row)
            for buffer in {r['buffer'] for r in live_rows}:
                deltas = {}
                for row in live_rows:
                    if row['buffer'] != buffer: continue
                    deltas[row['start']] = deltas.get(row['start'], 0) + row['width']
                    deltas[row['end'] + 1] = deltas.get(row['end'] + 1, 0) - row['width']
                used = 0; peak[buffer] = 0
                for _, delta in sorted(deltas.items()):
                    used += delta; peak[buffer] = max(peak[buffer], used)
            for kind in ('uses', 'local_uses'):
                for row in expanded[kind]:
                    reference = row.pop('retention', None)
                    if kind == 'uses' and 'holding' in row:
                        raise ValueError('template physical holding needs an explicit retention lifetime, not manual holding')
                    if reference is None:
                        if kind == 'local_uses':
                            raise ValueError('template local use requires an explicit retention lifetime')
                        continue
                    retained = register_values.get(reference)
                    if retained is None: raise ValueError('use references missing retention ' + reference)
                    if not retained['start'] <= row['cycle'] <= retained['end']:
                        raise ValueError('operand use outside retained lifetime')
                    if kind == 'local_uses':
                        if row['bit_offset'] + row['width'] > retained['width']:
                            raise ValueError('local operand exceeds retained value width')
                        row['buffer'] = retained['buffer']; row['bit_offset'] += retained['bit_offset']
                        row['ready_cycle'] = retained['start']
                        row.setdefault('kind', 'register'); row.setdefault('producer_basis', retained.get('producer_basis', retained.get('read_id')))
                    else:
                        if row['read_id'] != retained.get('read_id'):
                            raise ValueError('physical holding retention must capture the same read')
                        row['holding'] = {'buffer': retained['buffer'], 'bit_offset': retained.get('source_bit_offset', 0), 'width': retained['width']}
            for row in dependencies:
                if row['before'] not in events or row['after'] not in events:
                    raise ValueError('dependency references missing expanded event')
                if events[row['after']] - events[row['before']] < row['min_latency']:
                    raise ValueError('declared dependency latency/order violated: ' + row['before'] + ' -> ' + row['after'])
            # Read/write in one address and cycle has unspecified old/new semantics.
            accesses = {}
            for kind in ('reads', 'writes'):
                for row in expanded[kind]:
                    key = (row['storage_reference'], row['instance'], row['word_address'], row['cycle'])
                    previous = accesses.setdefault(key, set()); previous.add(kind)
                    if len(previous) == 2:
                        raise ValueError('same-address read/write cycle needs explicit service ordering in separate cycles')
            window = dict(expanded, id=label, workload=template['workload'], cycles=total,
                          basis=template['basis'], repetition_basis=template['repetition_basis'],
                          service_only=not expanded['uses'] and not expanded['local_uses'])
            windows.append(window)
            results.append({'id': label, 'workload': template['workload'], 'status': 'pass',
                            'cycles': total, 'components': components, 'peak_buffer_bits': peak,
                            'retentions': retentions, 'expanded_events': event_count,
                            'window_id': label, 'issued_operations': issued,
                            'scope': 'declared bounded serial phases/repetitions, explicit lanes, physical service and register lifetimes; source coverage, actual values and producer semantics require independent audit'})
        except (KeyError, ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError) as error:
            errors.append(label + ': ' + str(error))
            results.append({'id': label, 'status': 'error', 'error': str(error)})
    return {'ok': not errors, 'errors': errors, 'unknown': unknown, 'templates': results, 'windows': windows}
