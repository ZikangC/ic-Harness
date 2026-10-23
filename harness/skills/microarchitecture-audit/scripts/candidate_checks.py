"""Independent proposal comparisons and necessary service/supply bounds.

An executed comparison can fail while ``ok`` remains true.  This tool proves
arithmetic over explicitly bound witnesses, not source interpretation, a whole
task schedule, operand values, numerical correctness, or physical closure.
Intervals are half-open [start, end); their union permits legitimate overlap.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_source = _load('audit_candidate_source_model', Path(__file__).with_name('source_model.py'))
_evidence = _load('audit_candidate_evidence', Path(__file__).with_name('validate_review.py'))


def _identifier(value):
    text = str(value)
    safe = re.sub(r'\W', '_', text)
    if not safe or safe[0].isdigit():
        safe = '_' + safe
    return safe if safe == text else safe + '_' + hashlib.sha256(text.encode()).hexdigest()[:8]


def interval_union(intervals):
    """Duration of the union, rather than a sum that forbids legal overlap."""
    spans = sorted(intervals)
    if not spans:
        return 0
    start, end = spans[0]
    duration = 0
    for left, right in spans[1:]:
        if left <= end:
            end = max(end, right)
        else:
            duration += end - start
            start, end = left, right
    return duration + end - start


def _flow_group(row, flows, mapping, traffic_ids):
    """Validate the one flow-group protocol; no legacy single-flow alias."""
    if not isinstance(row, dict) or set(row) - {'id', 'flow_ids', 'workload', 'traffic_ids', 'basis', 'references'}:
        raise ValueError('flow group requires id, flow_ids, workload, traffic_ids, basis and optional references only')
    gid, sid = row.get('id'), row.get('workload')
    if not isinstance(gid, str) or not gid or sid not in mapping or not row.get('basis'):
        raise ValueError('flow group needs an id, existing independent workload and basis')
    for key, available in (('flow_ids', flows), ('traffic_ids', traffic_ids)):
        ids = row.get(key)
        if not isinstance(ids, list) or (key == 'flow_ids' and not ids) or any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
            raise ValueError('flow group needs unique ' + ('nonempty ' if key == 'flow_ids' else '') + key)
        if any(value not in available for value in ids):
            raise ValueError('flow group names absent ' + key)
    directions = {flows[fid].get('direction') for fid in row['flow_ids']}
    if len(directions) != 1 or not directions <= {'read', 'write'}:
        raise ValueError('flow group source flows require one common read/write direction')
    return gid, sid, next(iter(directions))


def _flow_owners(rows, flows, mapping, traffic_ids):
    """Invalid independent rows receive no credit and cannot poison valid rows."""
    owners = {}
    for row in rows:
        try:
            gid, sid, _ = _flow_group(row, flows, mapping, traffic_ids)
        except (KeyError, TypeError, ValueError):
            continue
        for key, ids in (('flow', row['flow_ids']), ('traffic', row['traffic_ids'])):
            for value in ids:
                owners.setdefault((sid, key, value), []).append(gid)
    return owners


def _count_metadata(observation):
    """Identify executed source/work objects, not a model's loose total label."""
    return {'kind': 'source_count_correspondence', 'unit': observation['unit'],
            'source_paths': copy.deepcopy(observation['source_paths']),
            'source_workload': observation['workload'],
            'candidate_workload': observation['candidate_workload'],
            'candidate_work_ids': copy.deepcopy(observation['candidate_work_ids']),
            'candidate_selection': copy.deepcopy(observation['candidate_selection']),
            'source_by_unit': copy.deepcopy(observation['source_by_unit']),
            'candidate_by_unit': copy.deepcopy(observation['candidate_by_unit']),
            'realization': observation['realization']}


def _native_metadata(observation, group, metric):
    metadata = _count_metadata(observation)
    metadata.update(kind='native_cost_' + metric,
                    unit='cycles' if metric == 'cycles' else group.get('actual_operation_unit') or 'numerical_operations',
                    operation_unit=group.get('actual_operation_unit'),
                    cost_group=group['cost_group'],
                    cost_work_ids=copy.deepcopy(group['work_ids']),
                    resource_ids=copy.deepcopy(group['resources']),
                    schedule_references=copy.deepcopy(group['schedule_references']))
    return metadata


class _UnsupportedABI(ValueError):
    pass


def _abi_comparisons(row,baseline,computed,sources,citations,engine):
    """Independent geometry and actual proposal claims, with finite probe identity."""
    if not isinstance(row,dict) or set(row)!={'id','geometry','claims','references'}:
        raise ValueError('ABI check requires id, geometry, claims and references')
    if not isinstance(row['id'],str) or not row['id'].strip():raise ValueError('ABI check id must be a nonempty string')
    rid=_identifier(row['id']);bindings=[]
    def refs(references,label,both=False):
        _source.references(references,sources,label,bindings,original_only=False,citations=citations)
        files={_evidence.resolve_citation(ref,sources,citations).get('file') for ref in references}
        if not files&{'architecture.md','design.json'}:raise ValueError('ABI declaration needs actual proposal references')
        if both and not any(_evidence.is_original_source(file) for file in files):raise ValueError('ABI geometry needs original-input references')
    refs(row['references'],'abi/'+rid,both=True)
    names={'source__'+key:value for key,value in baseline['fact_values'].items()}
    names.update({'proposal__'+key:value for key,value in computed['values'].items()})
    geometry=row['geometry']
    if not isinstance(geometry,dict) or not geometry.get('id'):
        raise ValueError('abi/'+str(row['id'])+': geometry.id required')
    derived=engine.derive_abi_geometry(geometry,names)
    if derived['unknown']:raise _UnsupportedABI('; '.join(derived['unknown']))
    if not derived['ok']:raise ValueError('; '.join(derived['errors']))
    claims=row['claims']
    if not isinstance(claims,list) or not 1<=len(claims)<=4096:raise ValueError('ABI claims must be a nonempty bounded array')
    checks=[];seen=set();traffic={item['id']:item for item in computed['ledger']['traffic']}
    for claim in claims:
        if not isinstance(claim,dict) or set(claim)-{'id','result','probe','declared','relation','references'} or not {'id','result','declared','relation','references'}<=set(claim):
            raise ValueError('ABI claim requires id, result, declared, relation and proposal references')
        if not isinstance(claim['id'],str) or not claim['id'].strip():raise ValueError('ABI claim id must be a nonempty string')
        cid=_identifier(claim['id'])
        if cid in seen:raise ValueError('duplicate ABI claim id')
        seen.add(cid);metric=claim['result'];pid=claim.get('probe')
        if not isinstance(metric,str):raise ValueError('ABI result must name a generated metric')
        key='p_'+pid+'__'+metric if isinstance(pid,str) else metric
        invalid_probe=pid is not None and (not isinstance(pid,str) or pid not in derived['probes'])
        if invalid_probe or key not in derived['values']:
            probe_metrics={probe:sorted(value[len('p_'+probe+'__'):] for value in derived['values']
                                       if value.startswith('p_'+probe+'__')) for probe in derived['probes']}
            recipe_metrics=sorted(value for value in derived['values']
                                  if not any(value.startswith('p_'+probe+'__') for probe in derived['probes']))
            if invalid_probe:
                raise ValueError('ABI result must belong to the actual selected probe; supplied probe '+repr(pid)
                                 +'; available probe metrics: '+repr(probe_metrics))
            raise ValueError('unknown generated ABI result '+repr(metric)+'; recipe results: '+repr(recipe_metrics)
                             +'; select probe and result separately; available probe metrics: '+repr(probe_metrics))
        point=None;local=dict(names)
        if pid is not None:
            point=derived['probes'][pid]
            if geometry['kind']=='axis_pack':
                local.update({'i'+str(index):value for index,value in enumerate(point['logical_index'])})
                local.update({'p'+str(index):value for index,value in enumerate(point['packed_index'])});local['lane']=point['lane']
            else:local.update(point)
        elif metric.startswith('p_'):raise ValueError('probe result requires its explicit probe id')
        refs(claim['references'],'abi/'+rid+'/'+cid)
        declared=claim['declared']
        if isinstance(declared,dict):
            if set(declared)!={'traffic_id','metric'} or declared['metric'] not in ('wire_bytes','total_bytes','transactions'):
                raise ValueError('ABI traffic claim selects actual traffic_id and a fresh ledger metric only')
            if declared['traffic_id'] not in traffic:raise ValueError('ABI traffic claim names absent selected-candidate traffic')
            actual=traffic[declared['traffic_id']][declared['metric']];target=dict(declared)
        else:
            actual=engine.expression(declared,local)
            proposal_refs=[]
            for ref in claim['references']:
                resolved=_evidence.resolve_citation(ref,sources,citations);file=resolved.get('file')
                if file in ('architecture.md','design.json'):
                    quote=resolved['quote'];text=sources[file]
                    if text.count(quote)==1:
                        start=text[:text.index(quote)].count('\n')+1
                        end=start+quote.rstrip('\r\n').count('\n')
                    else:start,end=resolved['start'],resolved.get('end',resolved['start'])
                    proposal_refs.append((file,start,end))
            target={'kind':'proposal_formula','references':[
                {'file':file,'start':start,'end':end} for file,start,end in sorted(set(proposal_refs))]}
        if type(actual) not in (int,float) or not math.isfinite(actual) or actual!=int(actual):raise ValueError('declared ABI claim must resolve to a finite integer')
        actual=int(actual);expected=derived['values'][key];relation=claim['relation']
        if relation not in ('==','<=','>='):raise ValueError('ABI relation must be ==|<=|>=, with generated result on the left')
        passed={'==':expected==actual,'<=':expected<=actual,'>=':expected>=actual}[relation]
        checks.append({'id':'abi__'+rid+'__'+cid,'topic':'abi','kind':'abi_geometry','status':'pass' if passed else 'fail',
            'expected':expected,'actual':actual,'basis':'Fixed integer ABI geometry against independently selected actual proposal claim; finite probes, not semantic completeness.',
            'geometry_id':geometry['id'],'geometry_kind':geometry['kind'],'geometry_result':metric,
            'geometry_probe':copy.deepcopy(point.get('input',point) if point is not None else None),'abi_target':target,'relation':relation,
            'geometry_sha256':_source.fingerprint(geometry)})
    return checks,bindings


def _analyze_one(request, engine, fresh_report=None, flow_owners=None):
    errors, checks, values, bindings = [], [], {}, []
    result = {
        'ok': False, 'errors': errors, 'values': values, 'calculation_ids': [],
        'checks': checks, 'work_result': {}, 'missing_flow_checks': [],
        'missing_service_checks': [], 'missing_operand_checks': [],
        'citation_bindings': bindings,
        'scope': 'independently selected source/proposal correspondence and necessary '
                 'resource bounds over declared whole-traffic intervals and finite '
                 'operand issues; no proof of source interpretation, values, '
                 'initialization mechanism, repeated/tail schedule or RTL feasibility',
    }

    def emit(label, topic, status, expected, actual, basis, **details):
        if any(row['id'] == label for row in checks):
            raise ValueError('duplicate generated check id ' + label)
        checks.append({'id': label, 'topic': topic, 'status': status,
                       'expected': expected, 'actual': actual, 'basis': basis, **details})
        for suffix, value in (('expected', expected), ('actual', actual)):
            if type(value) in (int, float) and math.isfinite(value):
                key = label + '__' + suffix
                values[key] = value
                result['calculation_ids'].append(key)

    try:
        if not isinstance(request, dict) or request.get('mode') != 'proposal':
            raise ValueError('candidate_checks requires mode proposal')
        sources = request['sources']
        original = _evidence.original_sources(sources)
        proposal, budget, baseline = request['proposal'], request['budget'], request['baseline']
        if not baseline.get('source_receipts_verified'):
            raise ValueError('binding-verified independent source transactions required')
        model = copy.deepcopy(request['model'])
        candidate_id = model['candidate']
        if candidate_id != proposal.get('selected'):
            raise ValueError('formal proposal review must bind the selected candidate')
        candidates = [row for row in proposal['candidates'] if row['id'] == candidate_id]
        if len(candidates) != 1:
            raise ValueError('candidate must name one actual proposal candidate')
        candidate = candidates[0]
        identity = _source.fingerprint(engine.design_data(proposal))
        if budget.get('design_sha256') != identity:
            raise ValueError('budget belongs to a different proposal')
        # A document hash does not authenticate a separately supplied ledger.
        # Recompute using the shared calculator and use that fresh result.
        fresh = engine.evaluate(proposal, original) if fresh_report is None else fresh_report
        computed = fresh.get('candidates', {}).get(candidate_id)
        if computed is None:
            raise ValueError('proposal calculator could not resolve candidate')
        if budget.get('candidates', {}).get(candidate_id) != computed:
            raise ValueError('supplied candidate budget differs from fresh calculation')
        if not fresh.get('ok'):
            emit('proposal_calculation', 'resource_service', 'fail', [],
                 fresh.get('errors', []), 'Shared calculator found contradictory proposal declarations; comparison execution succeeded.')
        names = computed['values']
        mapping = model['workload_map']
        if not isinstance(mapping, dict):
            raise ValueError('workload_map must be an object')
        work = _source.compare_work(model, baseline, proposal, sources, engine,request.get('citations',[]))
        result['work_result'] = work
        if not work.get('ok'):
            errors.extend(work.get('errors', ['work comparison failed']))
        values.update(work.get('values', {}))
        result['calculation_ids'].extend(work.get('calculation_ids', []))
        bindings.extend(work.get('citation_bindings', []))
        for row in work.get('observations', []):
            status = ('pass' if row['matches'] else 'fail') if row['realization'] == 'direct' else 'unverified'
            emit('count__' + _identifier(row['id']), 'cycle_accounting', status,
                 row['expected'], row['actual'],
                 'Direct source/proposal count correspondence.' if status != 'unverified'
                 else 'Transformed computation needs independent equivalence review; unequal counts alone are not a contradiction.',
                 **_count_metadata(row))

        def number(expression, lower=0, integer=False):
            value = engine.expression(expression, names)
            if type(value) not in (int, float) or not math.isfinite(value) or value < lower or (integer and value != int(value)):
                raise ValueError('finite ' + ('integer ' if integer else '') + 'value >= ' + str(lower) + ' required')
            return int(value) if integer else value

        def declarations(key):
            rows = model.get(key, [])
            if not isinstance(rows, list) or len(rows) > 400:
                raise ValueError(key + ' must be a bounded array')
            return rows

        def refs(row, label):
            _source.references(row.get('references'), sources, label, bindings, original_only=False,citations=request.get('citations',[]))

        traffic = {row['id']: row for row in candidate.get('traffic', [])}
        derived_traffic = {row['id']: row for row in computed['ledger'].get('traffic', [])}
        groups = {row['id']: row for row in computed['ledger'].get('cost_groups', [])}
        profiles = computed['workloads']
        flows = baseline.get('flows', {})

        # Whole fixed flows may be packed into one group, but a source flow or
        # actual traffic row receives credit only once per source profile.
        flow_rows = declarations('flow_bindings')
        owners = _flow_owners(flow_rows, flows, mapping, traffic) if flow_owners is None else flow_owners
        covered_flows, seen_groups = set(), set()
        for row in flow_rows:
            gid, sid, direction = _flow_group(row, flows, mapping, traffic)
            if gid in seen_groups:
                raise ValueError('duplicate flow group id')
            seen_groups.add(gid)
            for kind, ids in (('flow', row['flow_ids']), ('traffic', row['traffic_ids'])):
                if any(len(owners.get((sid, kind, value), [])) != 1 for value in ids):
                    raise ValueError('duplicate whole ' + kind + '/profile credit; combine packed flows in one group')
            if row.get('references') is not None:
                refs(row, 'flow/' + gid)
            label = 'flow__' + _identifier(gid) + '__' + _identifier(sid)
            detail = {'kind': 'flow_binding', 'unit': 'bytes', 'flow_ids': list(row['flow_ids']),
                      'traffic_ids': list(row['traffic_ids']), 'source_workload': sid,
                      'candidate_workload': mapping[sid], 'direction': direction}
            fixed = all(flows[fid].get('encoding') == 'fixed_external' for fid in row['flow_ids'])
            if not fixed:
                emit(label, 'abi', 'unverified', None, None,
                     row['basis'] + '; source ABI-dependent or unknown encoding has no fixed physical-byte lower bound.',
                     required_bytes=None, available_bytes=None, deficit_bytes=None, **detail)
                continue
            task_bits, aligned_bytes, byte_aligned = 0, 0, True
            for fid in row['flow_ids']:
                flow = flows[fid]
                total = flow['totals'][sid]
                bits, payload = total['bits'], total['bytes']
                if type(bits) is not int or bits < 0 or type(payload) is not int or payload < 0:
                    raise ValueError('flow source totals require nonnegative integer bits and bytes')
                task_bits += bits
                aligned_bytes += payload
                occurrence_bits = flow.get('per_occurrence_bits', {}).get(sid)
                # Equal exact bit/byte totals also establish an aligned total
                # bound when occurrence metadata is absent.
                byte_aligned = byte_aligned and (occurrence_bits % 8 == 0 if type(occurrence_bits) is int
                                                else payload * 8 == bits)
            expected = aligned_bytes if byte_aligned else math.ceil(task_bits / 8)
            actual, unsupported, wrong_direction = 0, [], []
            for tid in row['traffic_ids']:
                tr = traffic[tid]
                actual += number(tr['payload_bytes'], 1, True) * number(tr['transfers'], 1, True)
                if tr.get('model') not in ('continuous_reserved', 'posted_write') or tid not in derived_traffic:
                    unsupported.append('unsupported or uncomputed traffic ' + tid)
                elif (direction == 'read') != (tr.get('model') == 'continuous_reserved'):
                    wrong_direction.append(tid)
                if tr.get('workload') is not None and tr['workload'] != mapping[sid]:
                    wrong_direction.append(tid)
            status = 'fail' if wrong_direction else 'unverified' if unsupported else 'fail' if actual < expected else 'pass'
            quantity_scope = ('sum of byte-aligned complete-task fixed payloads' if byte_aligned else
                              'ceil(sum of complete-task fixed bits/8), optimistic necessary bound only; field/occurrence alignment and ABI allocation are not predicted')
            emit(label, 'abi', status, expected, actual,
                 row['basis'] + '; ' + quantity_scope + '; unpadded semantic payload only; '
                 + ('; '.join(unsupported) if unsupported else 'direction/profile mismatch: ' + str(wrong_direction) if wrong_direction else 'actual field mapping and any extra bytes still require ABI review.'),
                 required_bytes=expected, available_bytes=actual, deficit_bytes=max(expected-actual, 0),
                 required_bits=task_bits, byte_aligned=byte_aligned, mapping_conflicts=wrong_direction,
                 **detail)
            if status != 'unverified':
                covered_flows.update((fid, sid) for fid in row['flow_ids'])

            # A model-selected traffic subset can be wrong. Independently inspect
            # the entire actual inventory in this profile/direction, including
            # unbound rows, before presenting an inventory deficit as necessary.
            traffic_coverage = candidate.get('coverage', {}).get('traffic', {})
            raw_traffic = candidate.get('traffic', [])
            coverage_basis = traffic_coverage.get('basis')
            complete = (isinstance(coverage_basis, str) and bool(coverage_basis.strip()) and
                        ((bool(raw_traffic) and traffic_coverage.get('status') == 'covered') or
                         (raw_traffic == [] and traffic_coverage.get('status') == 'not_applicable')))
            unknown, eligible, inventory_bytes = [], [], 0
            if not complete:
                unknown.append('selected candidate traffic coverage is incomplete or inconsistent with the actual inventory')
            if len(traffic) != len(candidate.get('traffic', [])):
                unknown.append('actual traffic inventory contains duplicate IDs')
            for tid, tr in traffic.items():
                if tr.get('workload') is not None and tr['workload'] != mapping[sid]:
                    continue
                model_kind = tr.get('model')
                if model_kind not in ('continuous_reserved', 'posted_write') or tid not in derived_traffic:
                    unknown.append('direction/model or arithmetic unresolved for actual traffic ' + tid)
                    continue
                if (direction == 'read') != (model_kind == 'continuous_reserved'):
                    continue
                eligible.append(tid)
                inventory_bytes += number(tr['payload_bytes'], 1, True) * number(tr['transfers'], 1, True)
            inventory_status = 'unverified' if unknown else 'fail' if inventory_bytes < expected else 'pass'
            inventory_detail = dict(detail, kind='candidate_inventory_bound', traffic_ids=eligible,
                                    bound_traffic_ids=list(row['traffic_ids']), coverage_complete=complete and not unknown)
            emit('flow_inventory__' + _identifier(gid) + '__' + _identifier(sid), 'abi',
                 inventory_status, expected, inventory_bytes,
                 quantity_scope + '; all same-direction actual eligible traffic in the selected profile; '
                 + ('Unverified: ' + '; '.join(unknown) if unknown else
                    'a deficit contradicts the selected complete inventory, not every possible architecture; sufficient aggregate bytes do not prove field placement or completion.'),
                 required_bytes=expected, available_bytes=inventory_bytes,
                 deficit_bytes=max(expected-inventory_bytes, 0), required_bits=task_bits,
                 byte_aligned=byte_aligned, **inventory_detail)
        result['missing_flow_checks'] = [
            {'flow_id': fid, 'workload': sid}
            for fid, flow in flows.items() if flow.get('encoding') == 'fixed_external'
            for sid in baseline['workloads'] if (fid, sid) not in covered_flows]

        inventory = {}
        for macro in candidate.get('macros', []):
            inventory[macro['name']] = dict(kind=macro['kind'], **{
                key: number(macro[key], 1, True) for key in ('count', 'depth', 'width')})
        rules = {row['kind']: row for row in proposal['requirements'].get('storage_rules', [])}
        service_groups, service_seen, serviced = {}, set(), set()
        window_ids = set()
        for row in declarations('service_windows'):
            wid, sid = row['id'], row['workload']
            if sid not in mapping:
                raise ValueError('service workload '+repr(sid)+' is not mapped in this request; mapped workloads: '
                                 +repr(sorted(mapping))+'; independent source workloads: '+repr(sorted(baseline['workloads'])))
            if not isinstance(wid, str) or not wid or wid in window_ids or not row.get('basis'):
                raise ValueError('service window needs unique id, independent workload and basis')
            window_ids.add(wid)
            refs(row, 'service/' + wid)
            pid = mapping[sid]
            task = profiles[pid]['task_cycles']
            names = dict(computed['values'], task_cycles=task)
            spans = row['intervals']
            if not isinstance(spans, list) or not spans:
                raise ValueError('service window needs nonempty intervals')
            intervals, outside = [], []
            for span in spans:
                start, end = number(span['start'], 0, True), number(span['end'], 0, True)
                if end <= start:
                    raise ValueError('service intervals require start < end')
                if end > task:
                    outside.append({'start': start, 'end': end})
                if start < task:
                    intervals.append((start, min(end, task)))
            emit('service_window__' + _identifier(wid) + '__bounds', 'resource_service',
                 'fail' if outside else 'pass', task, max(number(span['end'], 0, True) for span in spans),
                 row['basis'] + '; half-open intervals must lie inside derived whole-task duration; boundaries/dependencies still need interpretation review.')
            resource = row['resource']
            kind = resource['kind']
            if kind not in ('external_requests', 'macro_reads', 'macro_writes'):
                raise ValueError('resource kind external_requests|macro_reads|macro_writes required')
            reference = 'shared_external_requests' if kind == 'external_requests' else resource['reference']
            key = (sid, kind, reference)
            group = service_groups.setdefault(key, {'intervals': [], 'demand': 0,
                'traffic_ids': set(), 'unknown': [], 'capacity': None, 'basis': [], 'windows': []})
            group['intervals'].extend(intervals)
            group['basis'].append(row['basis'])
            group['windows'].append(wid)
            if outside:
                group['unknown'].append('window extends outside task')
            if kind == 'external_requests':
                fact_id = resource.get('capacity_fact')
                capacity = baseline.get('fact_values', {}).get(fact_id)
                unit = str(baseline.get('fact_units', {}).get(fact_id, '')).lower().replace(' ', '')
                if 'capacity_source' in resource:
                    if fact_id is not None:raise ValueError('external capacity chooses a baseline fact OR original-source selector, not both')
                    fact={'id':'audit_external_capacity','source':resource['capacity_source']}
                    if 'unit' in resource:fact['unit']=resource['unit']
                    declared=engine.prepare_requirements({'facts':[fact],'constraints':[],'runtime_parameters':[]},original)
                    if declared['errors']:raise ValueError('external source capacity: '+'; '.join(declared['errors']))
                    bound=declared['requirements']['facts'][0]
                    capacity=bound['value'];unit=str(bound['unit']).lower().replace(' ','')
                    bindings.extend(declared['bindings']);fact_id=_source.fingerprint(fact)
                if capacity is None or not re.fullmatch(r'(requests?|transactions?|beats?)/cycles?', unit):
                    group['unknown'].append('original-input external requests/cycle capability missing or unsupported')
                    capacity = None
                elif type(capacity) not in (int, float) or not math.isfinite(capacity) or capacity <= 0:
                    raise ValueError('external capacity fact must be a positive finite rate')
                old_fact = group.get('capacity_fact')
                if old_fact is not None and old_fact != fact_id:
                    group['unknown'].append('multiple external capacity facts require an explicit interface/resource binding')
                group['capacity_fact'] = fact_id
            else:
                macro = inventory.get(reference)
                if macro is None:
                    raise ValueError('macro service names absent macro')
                port = 'read_ports' if kind == 'macro_reads' else 'write_ports'
                rule = rules.get(macro['kind'], {})
                capacity = number(rule[port], 0, True) * macro['count'] if port in rule else None
                if capacity is None:
                    group['unknown'].append('original-input ' + port + ' capability missing')
            if group['capacity'] is not None and capacity is not None and group['capacity'] != capacity:
                group['unknown'].append('resource capacities disagree')
            if capacity is not None:
                group['capacity'] = capacity
            window_demand, window_unknown = 0, []
            demands = row['demands']
            if not isinstance(demands, list) or not demands:
                raise ValueError('service window needs nonempty whole-traffic demands')
            for demand in demands:
                tid = demand['traffic_id']
                if tid not in traffic or tid not in derived_traffic:
                    raise ValueError('service demand names unresolved traffic')
                credit = (sid, kind, reference, tid)
                if credit in service_seen:
                    raise ValueError('duplicate whole-traffic service demand on the same resource/profile')
                service_seen.add(credit)
                group['traffic_ids'].add(tid)
                tr = traffic[tid]
                if tr.get('workload') is not None and tr['workload'] != pid:
                    raise ValueError('service demand has a different proposal workload')
                if tr.get('model') == 'unsupported':
                    group['unknown'].append('unsupported traffic ' + tid)
                    window_unknown.append('unsupported traffic ' + tid)
                if kind == 'external_requests':
                    if 'representation' in demand:
                        raise ValueError('external request encoding cannot be changed by an internal representation')
                    group['demand'] += derived_traffic[tid]['transactions']
                    window_demand += derived_traffic[tid]['transactions']
                else:
                    representation = demand.get('representation')
                    if not isinstance(representation, dict) or set(representation) != {'input_bits', 'stored_bits'}:
                        group['unknown'].append('explicit input/stored representation binding absent for ' + tid)
                        window_unknown.append('explicit representation missing for ' + tid)
                        continue
                    input_bits = number(representation['input_bits'], 1, True)
                    stored_bits = number(representation['stored_bits'], 1, True)
                    payload_bits = number(tr['payload_bytes'], 1, True) * 8
                    transfers = number(tr['transfers'], 1, True)
                    # This is an optimistic aggregate packing lower bound. Each
                    # independent payload rounds separately; no implicit padding,
                    # metadata, fixed FP32 representation or reuse multiplier.
                    if payload_bits % input_bits:
                        group['unknown'].append('payload is not an integral represented element count for ' + tid)
                        window_unknown.append('nonintegral represented payload for ' + tid)
                        continue
                    words = math.ceil((payload_bits // input_bits) * stored_bits / inventory[reference]['width']) * transfers
                    group['demand'] += words
                    window_demand += words
            window_capacity = None if capacity is None else capacity * interval_union(intervals)
            window_status = 'unverified' if window_unknown or capacity is None or outside else 'pass' if window_demand <= window_capacity else 'fail'
            emit('service_window__' + _identifier(wid) + '__capacity', 'resource_service',
                 window_status, window_capacity, window_demand,
                 row['basis'] + '; whole demands must also fit their own allowed interval union; aggregate resource windows cannot lend time outside this window.')
        for (sid, kind, reference), group in service_groups.items():
            duration = interval_union(group['intervals'])
            capacity = None if group['capacity'] is None else group['capacity'] * duration
            status = 'unverified' if group['unknown'] or capacity is None else 'pass' if group['demand'] <= capacity else 'fail'
            emit('service__' + _identifier(sid) + '__' + kind + '__' + _identifier(reference),
                 'resource_service', status, capacity, group['demand'],
                 '; '.join(group['basis']) + '; uses the union of declared service intervals; shared resource demands add, compute and independent resources do not; optimistic aggregate bound, not bank/arbitration or full-schedule proof.'
                 + (' Unverified: ' + '; '.join(sorted(set(group['unknown']))) if group['unknown'] else ''))
            if status != 'unverified':
                serviced.update((sid, kind, reference, tid) for tid in group['traffic_ids'])
        # External acceptance service is necessary for every actual traffic row
        # in every corresponding profile unless the row explicitly limits scope.
        result['missing_service_checks'] = [tid for tid, row in traffic.items()
            if any((sid, 'external_requests', 'shared_external_requests', tid) not in serviced
                   for sid, pid in mapping.items() if row.get('workload', pid) == pid)]
        result['missing_service_declarations'] = [tid for tid, row in traffic.items()
            if any((sid, 'external_requests', 'shared_external_requests', tid) not in service_seen
                   for sid, pid in mapping.items() if row.get('workload', pid) == pid)]

        windows = {row['id']: row for row in candidate.get('schedule_windows', [])}
        buffers = {row['id']: row for row in candidate.get('buffers', [])}
        inventory_for_execution = {name: dict(row, bits=row['count'] * row['depth'] * row['width'])
                                   for name, row in inventory.items()}
        physical = engine._helpers['execution_model'].derive(candidate, names,
                     proposal['requirements'], engine.expression, inventory_for_execution)
        physical_windows = {row['id']: row for row in physical['windows']}
        # Expanded templates are real proposal windows too. Reuse the shared
        # kernel's normalized windows, never synthesize an audit issue list from
        # them. Only unambiguous, successfully expanded declarations are usable.
        template_ids = [row.get('id') for row in candidate.get('execution_templates', [])
                        if isinstance(row, dict)]
        collisions = set(windows) & set(template_ids)
        collisions.update(wid for wid in template_ids if template_ids.count(wid) > 1)
        actual_templates = {row['id'] for row in physical.get('templates', [])
                            if row.get('status') in ('pass', 'unverified')}
        for wid in actual_templates - collisions:
            if wid in template_ids and wid in physical_windows:
                windows[wid] = physical_windows[wid]
        if physical['errors']:
            emit('physical_window_validation', 'resource_service', 'fail', [], physical['errors'],
                 'Existing calculator validates concrete word accesses, instance ports, read readiness, slices and holding registers within finite windows.')
        covered_groups, operand_rows_seen = set(), set()
        for row in declarations('operand_checks'):
            wid, gid = row['window_id'], row['cost_group']
            if (wid, gid) in operand_rows_seen:
                raise ValueError('duplicate window/cost-group completeness declaration')
            operand_rows_seen.add((wid, gid))
            if wid not in windows or gid not in groups or groups[gid].get('model') != 'compute' or not row.get('basis'):
                raise ValueError('operand check needs actual window/compute group and basis')
            window, group = windows[wid], groups[gid]
            if window['workload'] != group['workload']:
                raise ValueError('operand window and group workloads differ')
            physical_window = physical_windows.get(wid, {})
            if physical_window.get('status') == 'unverified':
                emit('operands__' + _identifier(wid) + '__' + _identifier(gid),
                     'resource_service', 'unverified', None, physical_window.get('reason'),
                     row['basis'] + '; unsupported physical window cannot establish completeness or a physical contradiction.')
                continue
            duration, units = number(window['cycles'], 1, True), number(group['units'], 1, True)
            issues = row['issues']
            if not isinstance(issues, list) or not issues or len(issues) > 4096:
                raise ValueError('operand checks need 1..4096 concrete issues')
            supplied, unknown_supply, local_failures = set(), set(), []
            physical_unknown = [item for item in physical['unknown']
                                if item.startswith(wid + '/') or item.startswith(wid + ':')]
            for use in physical_window.get('uses', []):
                if use['cost_group'] == gid:
                    position = (number(use['cycle'], 0, True), number(use['lane'], 0, True), use['operand'])
                    supplied.add(position)
                    if use.get('ready_cycle') is None:
                        unknown_supply.add(position)
            local = row.get('local_uses', [])
            if not isinstance(local, list):
                raise ValueError('local_uses must be an array')
            for use in local:
                kind = use['kind']
                if kind not in ('register', 'feedback') or not use.get('basis'):
                    raise ValueError('local use needs register|feedback kind and basis')
                refs(use, 'operand/' + wid + '/' + gid + '/local')
                if not any(_evidence.resolve_citation(ref,sources,request.get('citations',[])).get('file') in ('architecture.md', 'design.json') for ref in use['references']):
                    raise ValueError('local use must bind its candidate value/timing explanation in proposal evidence')
                if kind == 'feedback' and not use.get('feedback_basis'):
                    raise ValueError('feedback use needs explicit producer/iteration basis')
                buffer = buffers.get(use['buffer'])
                if buffer is None or buffer.get('kind') != 'register':
                    raise ValueError('local supply requires an actual register buffer')
                cycle, lane = number(use['cycle'], 0, True), number(use['lane'], 0, True)
                width, offset = number(use['width'], 1, True), number(use['bit_offset'], 0, True)
                ready = number(use['ready_cycle'], 0, True)
                if cycle >= duration or lane >= units or not use.get('operand'):
                    raise ValueError('local use outside finite window/lane or unnamed operand')
                capacity = math.prod(number(buffer[key], 1, True) for key in ('count', 'depth', 'width'))
                position = (cycle, lane, use['operand'])
                if position in supplied:
                    raise ValueError('duplicate physical/local operand position')
                if offset + width > capacity or cycle < ready:
                    local_failures.append({'position': list(position), 'width': width,
                                           'bit_offset': offset, 'capacity': capacity, 'ready_cycle': ready})
                else:
                    supplied.add(position)
            expected_positions, partial, weak = set(), [], []
            issue_cycles, full_issue_cycles = set(), set()
            for issue in issues:
                cycle = number(issue['cycle'], 0, True)
                lanes, roles = issue['lanes'], issue['operands']
                if cycle >= duration or cycle in issue_cycles:
                    raise ValueError('one issue declaration per actual finite-window cycle required')
                issue_cycles.add(cycle)
                if not isinstance(lanes, list) or not lanes or any(type(lane) is not int or lane < 0 or lane >= units for lane in lanes) or len(lanes) != len(set(lanes)):
                    raise ValueError('issue lanes must be unique actual lane indices')
                if not isinstance(roles, list) or not roles or any(not isinstance(role, str) or not role for role in roles) or len(roles) != len(set(roles)):
                    raise ValueError('issue operand roles must be unique nonempty names')
                if set(lanes) != set(range(units)):
                    partial.append({'cycle': cycle, 'lanes': lanes,
                                    'reason': issue.get('partial_issue_basis', row.get('partial_issue_basis'))})
                else:
                    full_issue_cycles.add(cycle)
                if group.get('operation_unit') == 'MAC' and len(roles) < 2:
                    weak.append(cycle)
                expected_positions.update((cycle, lane, role) for lane in lanes for role in roles)
            missing = sorted(expected_positions - supplied)
            unknown = sorted(expected_positions & unknown_supply)
            unjustified_partial = [item for item in partial
                                   if not full_issue_cycles or not isinstance(item['reason'], str)
                                   or not item['reason'].strip()]
            status = 'fail' if missing or local_failures else 'unverified' if unjustified_partial or weak or unknown or physical_unknown or not physical_window else 'pass'
            detail = {'missing_operand_positions': [list(position) for position in missing],
                      'unverified_ready_positions': [list(position) for position in unknown],
                      'local_contradictions': local_failures, 'partial_issues': partial,
                      'unjustified_partial_issues': unjustified_partial,
                      'full_issue_cycles': sorted(full_issue_cycles),
                      'MAC_issues_with_fewer_than_two_input_roles': weak,
                      'unverified_physical_capabilities': physical_unknown,
                      'declared_positions': len(expected_positions), 'supplied_positions': len(expected_positions & supplied)}
            emit('operands__' + _identifier(wid) + '__' + _identifier(gid),
                 'resource_service', status, len(expected_positions), detail,
                 row['basis'] + '; validates independently declared active lanes/roles against actual finite physical supply. An explicitly justified partial issue may accompany a full-lane checked issue; a partial-only or one-input MAC witness cannot establish full resource supply. Local producer semantics, source coverage, repetition and complete-task interpretation still require independent review.')
            if status != 'unverified':
                covered_groups.add(gid)
        result['missing_operand_checks'] = [gid for gid, row in groups.items()
            if row.get('model') == 'compute' and row.get('operations', 1) > 0 and gid not in covered_groups]
    except (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
        errors.append(type(error).__name__ + ': ' + str(error))
    result['ok'] = not errors
    return result



def _script_files(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob('*')) if path.is_file()
            and '__pycache__' not in path.parts and path.suffix != '.pyc' and path.name != '.DS_Store'}


def execution_binding(model, baseline, proposal, budget, sources, engine):
    """Pure frozen context binding, with the same non-cache closures as the host."""
    calculator = _script_files(Path(engine.__file__).parent)
    checker = _script_files(Path(__file__).parent)
    return {'source_analysis_sha256': _source.fingerprint(baseline),
            'source_sha256s': {key: hashlib.sha256(value.encode()).hexdigest() for key, value in sources.items()},
            'design_sha256': _source.fingerprint(engine.design_data(proposal)),
            'model_sha256': _source.fingerprint(model), 'budget_sha256': _source.fingerprint(budget),
            'calculator_files_sha256': calculator, 'calculator_bundle_sha256': _source.fingerprint(calculator),
            'checker_files_sha256': checker, 'checker_bundle_sha256': _source.fingerprint(checker)}


def analyze(request, engine):
    """Commit independent executed witnesses, never values from failed declarations."""
    result = {'ok': False, 'errors': [], 'values': {}, 'calculation_ids': [],
              'checks': [], 'observations': [], 'subreceipts': [], 'work_result': {},
              'missing_profiles': [], 'missing_count_checks': [], 'missing_flow_checks': [],
              'missing_service_checks': [], 'missing_service_declarations': [], 'missing_operand_checks': [],
              'unverified_transformations': [], 'citation_bindings': [],
              'coverage_complete': False, 'execution_status': 'failed',
              'scope': 'independent local executed receipts; partial coverage and failed declarations do not establish full delivery. Resource bounds and finite issues do not prove equivalence or full task feasibility.'}
    try:
        if not isinstance(request, dict) or request.get('mode') != 'proposal':
            raise ValueError('candidate_checks requires mode proposal')
        model, baseline, proposal, budget, sources = (request[key] for key in ('model', 'baseline', 'proposal', 'budget', 'sources'))
        if not isinstance(model, dict):
            raise ValueError('object model required')
        if model.get('candidate') != proposal.get('selected'):
            raise ValueError('formal proposal review must bind selected candidate')
        binding = execution_binding(model, baseline, proposal, budget, sources, engine)
        result['binding'] = binding
        result['model_sha256'] = binding['model_sha256']
        baseline = _source.source_view(baseline, request['source_declaration'], sources, engine, request.get('execution_dependencies'))
        result['source_coverage_complete'] = baseline['source_coverage_complete']
        result['missing_source_declarations'] = baseline['missing_source_declarations']
        if not baseline['source_receipts_verified']:
            raise ValueError('source transaction integrity failed: ' + '; '.join(baseline['source_integrity_errors']))
        if budget.get('design_sha256') != binding['design_sha256']:
            raise ValueError('budget belongs to a different proposal')
        fresh = engine.evaluate(proposal, _evidence.original_sources(sources))
        selected = fresh.get('candidates', {}).get(proposal['selected'])
        if selected is None or budget.get('candidates', {}).get(proposal['selected']) != selected:
            raise ValueError('supplied candidate budget differs from fresh calculation')
        raw_mapping = model.get('workload_map', {})
        if not isinstance(raw_mapping, dict):
            raise ValueError('workload_map must be an object')
        mapping, mapping_errors = {}, {}
        for sid, pid in raw_mapping.items():
            if (not isinstance(sid, str) or not isinstance(pid, str)
                    or sid not in baseline['workloads'] or pid not in selected['workloads']):
                mapping_errors[sid] = 'profile mapping needs existing source and candidate workloads'
                continue
            profile = selected['workloads'][pid]
            error = _source.profile_mapping_error(baseline['workloads'][sid], baseline['workload_units'][sid],
                                                   profile['quantity'], profile['unit'])
            if error:
                mapping_errors[sid] = 'profile/' + sid + ' -> ' + pid + ': ' + error
            else:
                mapping[sid] = pid
        result['missing_profiles'] = ([{'side': 'source', 'workload': sid} for sid in baseline['declared_workloads'] if sid not in mapping]
            + [{'side': 'candidate', 'workload': pid} for pid in selected['workloads'] if pid not in mapping.values()])

        def record(section, rid, declaration, checks=(), observations=(), values=None,
                   errors=(), citation_bindings=(),unknown=()):
            receipt = {'id': rid, 'section': section,
                       'status': 'declaration_failed' if errors else 'executed',
                       'errors': list(errors), 'checks': [], 'observations': [],
                       'values': {}, 'calculation_ids': [], 'citation_bindings': [],
                       'binding': binding, 'declaration_sha256': _source.fingerprint(declaration)}
            if unknown:receipt['unknown']=list(unknown)
            if not errors:
                receipt['checks'] = [dict(copy.deepcopy(row), receipt_id=rid) for row in checks]
                receipt['observations'] = [dict(copy.deepcopy(row), receipt_id=rid) for row in observations]
                receipt['values'] = {rid + '::' + key: value for key, value in (values or {}).items()}
                receipt['calculation_ids'] = list(receipt['values'])
                receipt['citation_bindings'] = copy.deepcopy(list(citation_bindings))
                result['checks'].extend(receipt['checks'])
                result['observations'].extend(receipt['observations'])
                result['citation_bindings'].extend(receipt['citation_bindings'])
            else:
                result['errors'].extend(str(error) for error in errors)
            receipt['receipt_sha256'] = _source.fingerprint(receipt)
            result['subreceipts'].append(receipt)
            return receipt

        def numeric(checks):
            values = {}
            for row in checks:
                for suffix in ('expected', 'actual'):
                    value = row[suffix]
                    if type(value) in (int, float) and math.isfinite(value):
                        values[row['id'] + '__' + suffix] = value
            return values

        def rows(key):
            value = model.get(key, [])
            if not isinstance(value, list) or len(value) > 400:
                record(key, key + '/declaration', value, errors=[key + ' must be a bounded array'])
                return []
            return value

        allowed_model_keys = {'candidate', 'workload_map', 'count_checks',
                              'flow_bindings', 'service_windows', 'operand_checks','abi_checks'}
        unknown_model_keys = sorted(set(model) - allowed_model_keys)
        if unknown_model_keys:
            errors = ['unknown proposal model field: ' + key for key in unknown_model_keys]
            if 'physical_realization' in unknown_model_keys:
                errors.append('physical_realization belongs at count_checks[].physical_realization; '
                              'top-level recipes are not moved or applied')
            record('model', 'model/declaration',
                   {key: model[key] for key in unknown_model_keys}, errors=errors)

        def child(section, declarations, owners=None):
            isolated = dict(model, workload_map=mapping, count_checks=[], flow_bindings=[], service_windows=[], operand_checks=[],abi_checks=[])
            if section is not None:
                isolated[section] = declarations
            call = dict(request, model=isolated, baseline=baseline)
            return _analyze_one(call, engine, fresh, owners)

        setup = child(None, [])
        if not setup.get('ok'):
            raise ValueError('; '.join(setup.get('errors', ['frozen setup failed'])))
        setup_checks = [row for row in setup['checks'] if row['id'] in ('proposal_calculation', 'physical_window_validation')]
        record('input', 'input/frozen', {'candidate': model['candidate']},
               checks=setup_checks, values=numeric(setup_checks))
        for sid, pid in raw_mapping.items():
            if sid not in mapping:
                record('profile', 'profile/' + _identifier(sid), {sid: pid},
                       errors=[mapping_errors[sid]])

        # Count declarations have their own transactions and cross-row credit
        # checks. An unrelated bad row cannot erase an executed comparison.
        count_model = dict(model, workload_map=mapping, count_checks=rows('count_checks'))
        work = _source.compare_work(count_model, baseline, proposal, sources, engine, request.get('citations', []))
        result['work_result'] = work
        result['missing_count_checks'] = work.get('missing_count_checks', [
            {'workload': sid, 'source_path': path} for sid in baseline['workloads'] for path in baseline['computations']])
        for index, sub in enumerate(work.get('subreceipts', [])):
            declaration = count_model['count_checks'][index]
            rid = 'count/' + str(index) + '/' + _identifier(declaration.get('id', '?') if isinstance(declaration, dict) else '?')
            if sub['status'] != 'executed':
                record('count', rid, declaration, errors=sub['errors'])
                continue
            observations = sub['observations']
            checks = []
            for observation in observations:
                status = ('pass' if observation['matches'] else 'fail') if observation['realization'] == 'direct' else 'unverified'
                label = 'count__' + _identifier(observation['id'])
                checks.append({'id': label, 'topic': 'cycle_accounting', 'status': status,
                               'expected': observation['expected'], 'actual': observation['actual'],
                               'basis': 'Executed direct count correspondence.' if status != 'unverified' else 'Transformed source equivalence remains unverified; physical cost is checked separately.',
                               **_count_metadata(observation)})
                physical = observation.get('physical_realization', {})
                for group in physical.get('groups', []):
                    for metric, expected, actual in (
                            ('operations', group.get('physical_operations', 0 if not group.get('physical_by_unit') and group.get('status')=='pass' else None), group.get('actual_cost_operations')),
                            ('cycles', group.get('required_cycles'), group.get('scheduled_cycles'))):
                        if expected is not None:
                            match = expected == actual if metric == 'operations' else expected <= actual
                            checks.append({'id': label + '__physical__' + _identifier(group['cost_group']) + '__' + metric,
                                           'topic': 'cycle_accounting', 'status': 'pass' if match else 'fail',
                                           'expected': expected, 'actual': actual,
                                           'basis': 'Typed physical recipe against actual selected group, units/rate, resources and schedule; no source operation universal lower bound.',
                                           **_native_metadata(observation, group, metric)})
                if observation['realization'] == 'transformed':
                    result['unverified_transformations'].append(observation['id'])
            local_values = dict(sub['values']); local_values.update(numeric(checks))
            record('count', rid, declaration, checks, observations, local_values,
                   citation_bindings=sub.get('citation_bindings', []))
        if not work.get('subreceipts') and not work.get('ok'):
            record('count', 'count/declaration', count_model['count_checks'], errors=work.get('errors', ['count declarations failed']))

        flow_rows = rows('flow_bindings')
        traffic_ids = {row['id'] for row in next(c for c in proposal['candidates'] if c['id'] == proposal['selected']).get('traffic', [])}
        owners = _flow_owners(flow_rows, baseline.get('flows', {}), mapping, traffic_ids)
        flow_seen, flow_covered = set(), set()
        for index, row in enumerate(flow_rows):
            rid = 'flow/' + str(index)
            gid = row.get('id') if isinstance(row, dict) else None
            if isinstance(gid, str) and gid in flow_seen:
                record('flow', rid, row, errors=['duplicate flow group id'])
                continue
            if isinstance(gid, str):
                flow_seen.add(gid)
            sub = child('flow_bindings', [row], owners)
            if not sub['ok']:
                record('flow', rid, row, errors=sub['errors'])
                continue
            checks = [value for value in sub['checks']
                      if value['id'].startswith(('flow__', 'flow_inventory__'))]
            record('flow', rid, row, checks=checks, values=numeric(checks), citation_bindings=sub['citation_bindings'])
            if checks and checks[0]['status'] != 'unverified':
                flow_covered.update((fid, row['workload']) for fid in row['flow_ids'])
        result['missing_flow_checks'] = [{'flow_id': fid, 'workload': sid}
            for fid, flow in baseline.get('flows', {}).items() if flow.get('encoding') == 'fixed_external'
            for sid in baseline['workloads'] if (fid, sid) not in flow_covered]

        service_rows, valid_service = rows('service_windows'), []
        for index, row in enumerate(service_rows):
            sub = child('service_windows', [row])
            if not sub['ok']:
                record('service', 'service/' + str(index), row, errors=sub['errors'])
                continue
            checks = [value for value in sub['checks'] if value['id'].startswith('service_window__')]
            record('service', 'service/' + str(index), row, checks=checks,
                   values=numeric(checks), citation_bindings=sub['citation_bindings'])
            valid_service.append(row)
        service = child('service_windows', valid_service)
        if service['ok']:
            checks = [value for value in service['checks'] if value['id'].startswith('service__')]
            record('service_aggregate', 'service/aggregate', valid_service,
                   checks=checks, values=numeric(checks), citation_bindings=service['citation_bindings'])
            result['missing_service_checks'] = service['missing_service_checks']
            result['missing_service_declarations'] = service.get('missing_service_declarations', [])
        else:
            record('service_aggregate', 'service/aggregate', valid_service, errors=service['errors'])
            result['missing_service_checks'] = list(traffic_ids)
            result['missing_service_declarations'] = list(traffic_ids)

        operand_covered, operand_seen = set(), set()
        for index, row in enumerate(rows('operand_checks')):
            rid = 'operand/' + str(index)
            key = (row.get('window_id'), row.get('cost_group')) if isinstance(row, dict) and isinstance(row.get('window_id'), str) and isinstance(row.get('cost_group'), str) else None
            if key in operand_seen:
                record('operand', rid, row, errors=['duplicate window/cost-group completeness declaration'])
                continue
            if key is not None:
                operand_seen.add(key)
            sub = child('operand_checks', [row])
            if not sub['ok']:
                record('operand', rid, row, errors=sub['errors'])
                continue
            checks = [value for value in sub['checks'] if value['id'].startswith('operands__')]
            record('operand', rid, row, checks=checks, values=numeric(checks), citation_bindings=sub['citation_bindings'])
            if isinstance(row, dict) and row.get('cost_group') not in sub['missing_operand_checks']:
                operand_covered.add(row['cost_group'])
        result['missing_operand_checks'] = [gid for gid in setup['missing_operand_checks'] if gid not in operand_covered]

        for index,row in enumerate(rows('abi_checks')):
            rid='abi/'+str(index)
            claims=row.get('claims') if isinstance(row,dict) else None
            if not isinstance(claims,list) or not 1<=len(claims)<=4096:
                record('abi',rid+'/declaration',row,errors=['ABI claims must be a nonempty bounded array']);continue
            claim_ids=set()
            for claim_index,claim in enumerate(claims):
                declaration=dict(row,claims=[claim]);child_id=rid+'/claim/'+str(claim_index)
                claim_id=claim.get('id') if isinstance(claim,dict) else None
                if isinstance(claim_id,str) and claim_id in claim_ids:
                    record('abi',child_id,declaration,errors=['duplicate ABI claim id']);continue
                if isinstance(claim_id,str):claim_ids.add(claim_id)
                try:
                    checks,bindings=_abi_comparisons(declaration,baseline,selected,sources,request.get('citations',[]),engine)
                    record('abi',child_id,declaration,checks=checks,values=numeric(checks),citation_bindings=bindings)
                except _UnsupportedABI as error:
                    record('abi',child_id,declaration,errors=[str(error)],unknown=[str(error)])
                except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as error:
                    record('abi',child_id,declaration,errors=[str(error)])

        # Qualified IDs are owned by the script, not supplied by the model.
        # Top-level values are exactly the union of executed subreceipt values.
        for receipt in result['subreceipts']:
            if receipt['status'] == 'executed':
                result['values'].update(receipt['values'])
        result['calculation_ids'] = list(result['values'])
        result['coverage_complete'] = result['source_coverage_complete'] and not result['errors'] and not any(result[key] for key in (
            'missing_profiles', 'missing_count_checks', 'missing_flow_checks', 'missing_service_declarations', 'missing_operand_checks'))
        result['ok'] = not result['errors']
        result['execution_status'] = 'executed' if result['coverage_complete'] else 'partial'
    except (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
        result['errors'].append(type(error).__name__ + ': ' + str(error))
        result['values'] = {}; result['calculation_ids'] = []
        result['checks'] = []; result['observations'] = []; result['subreceipts'] = []
        result['coverage_complete'] = False; result['execution_status'] = 'failed'
    return result


def check(model, baseline, proposal, budget, sources, engine, citations=(), *, source_declaration, execution_dependencies=None):
    """Host API; no model calls, filesystem mutation, or implicit source selection."""
    return analyze({'mode': 'proposal', 'model': model, 'baseline': baseline,
                    'proposal': proposal, 'budget': budget, 'sources': sources,
                    'citations': list(citations), 'source_declaration':source_declaration,
                    'execution_dependencies':execution_dependencies}, engine)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calculator', required=True)
    args = parser.parse_args()
    request = json.load(sys.stdin)
    if request.get('mode') != 'proposal':
        result = {'ok': False, 'errors': ['candidate_checks requires mode proposal']}
    else:
        result = check(request['model'], request['baseline'], request['proposal'],
                       request['budget'], request['sources'],
                       _source.load_engine(args.calculator), request.get('citations', ()),
                       source_declaration=request['source_declaration'],
                       execution_dependencies=request.get('execution_dependencies'))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
