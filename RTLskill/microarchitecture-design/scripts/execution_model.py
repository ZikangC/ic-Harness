"""Finite physical-word and operand-ready witnesses; not a full schedule proof."""
import copy
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('execution_templates', Path(__file__).with_name('execution_templates.py'))
_templates = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_templates)


def _derive_windows(candidate, values, requirements, expression, inventory):
    errors, unknown, windows = [], [], []
    groups = {r['id']:r for r in candidate.get('cost_groups',[])}
    profiles = {r['id'] for r in candidate.get('workloads',[])}
    buffers = {r['id']:r for r in candidate.get('buffers',[])}
    rules = {r['kind']:r for r in requirements.get('storage_rules',[])}
    def integer(value, lower=0):
        result = expression(value,values)
        if type(result) not in (int,float) or result != int(result) or result < lower:
            raise ValueError('integer >= '+str(lower)+' required')
        return int(result)
    seen, complete_groups = set(), set()
    for window in candidate.get('schedule_windows',[]):
        label = str(window.get('id','?')) if isinstance(window,dict) else '?'
        try:
            if not isinstance(window,dict) or not window.get('basis') or label in seen:
                raise ValueError('unique id and basis required')
            seen.add(label)
            if set(window)&{'accesses','ports','accesses_per_port_cycle'}:
                raise ValueError('aggregate access/port claims removed; provide physical reads/writes and uses')
            if window.get('model')=='unsupported':
                if not window.get('reason'):raise ValueError('unsupported window needs reason')
                unknown.append(label+': '+window['reason']);windows.append({'id':label,'status':'unverified','reason':window['reason']});continue
            if window.get('model','macro_words')!='macro_words':raise ValueError('window model must be macro_words|unsupported')
            duration = integer(window['cycles'],1)
            if window['workload'] not in profiles or not window.get('repetition_basis'):
                raise ValueError('actual workload and repetition/tail explanation required')
            for key in ('reads','writes','uses'):
                if not isinstance(window.get(key),list):raise ValueError(key+' must be an array')
            local = window.get('local_uses', [])
            if not isinstance(local,list):raise ValueError('local_uses must be an array')
            if not window['uses'] and not local and not (window.get('service_only') is True and (window['reads'] or window['writes'])):raise ValueError('at least one concrete operand use or nonempty service-only window required')
            requests, service, output_uses = {}, {}, []
            for kind in ('reads','writes'):
                for access in window[kind]:
                    rid = access['id']
                    if not isinstance(rid,str) or not rid or rid in requests:raise ValueError('unique physical request id required')
                    macro = inventory.get(access['storage_reference'])
                    if macro is None:raise ValueError('physical access needs actual macro reference')
                    instance, address, cycle = (integer(access[k]) for k in ('instance','word_address','cycle'))
                    if instance >= macro['count'] or address >= macro['depth'] or cycle >= duration:
                        raise ValueError('physical request outside instance/depth/window bounds')
                    rule = rules.get(macro['kind'],{})
                    port_key = 'read_ports' if kind=='reads' else 'write_ports'
                    port_count = integer(rule[port_key]) if port_key in rule else None
                    latency = integer(rule['read_latency_cycles']) if 'read_latency_cycles' in rule else None
                    if port_count is None or (kind=='reads' and latency is None):
                        unknown.append(label+'/'+rid+': input port/read latency capability absent')
                    key = (access['storage_reference'],instance,cycle,kind)
                    service[key] = service.get(key,0)+1
                    if port_count is not None and service[key] > port_count:
                        raise ValueError('physical requests exceed input '+port_key+' in one instance/cycle')
                    requests[rid] = dict(access,kind=kind,width=macro['width'],ready_cycle=None if latency is None else cycle+latency)
            occupied = set()
            for use in window['uses']:
                group = groups.get(use['cost_group'])
                if group is None or group['model']!='compute' or group['workload']!=window['workload']:
                    raise ValueError('operand use needs a compute group of this workload')
                cycle,lane,offset,width = (integer(use[k],1 if k=='width' else 0) for k in ('cycle','lane','bit_offset','width'))
                if cycle >= duration or lane >= integer(group['units'],1) or not use.get('operand'):
                    raise ValueError('operand use outside window/lane bounds or unnamed operand')
                key = (use['cost_group'],cycle,lane,use['operand'])
                if key in occupied:raise ValueError('duplicate operand position')
                occupied.add(key)
                request = requests.get(use['read_id'])
                if request is None or request['kind']!='reads':raise ValueError('operand read_id must name a physical read')
                if offset+width > request['width']:raise ValueError('operand slice exceeds physical word width')
                ready = request['ready_cycle']
                if ready is not None and cycle < ready:raise ValueError('operand used before synchronous read is ready')
                if 'hold_buffer' in use:raise ValueError('holding needs explicit buffer/bit_offset/width capture, not hold_buffer')
                if ready is not None and cycle > ready:
                    holding=use.get('holding')
                    if not isinstance(holding,dict) or set(holding)!={'buffer','bit_offset','width'}:
                        raise ValueError('late operand use needs explicit register slice capture')
                    hold = buffers.get(holding['buffer'])
                    if hold is None or hold.get('kind')!='register':raise ValueError('holding buffer must be an actual register buffer')
                    capture_offset=integer(holding['bit_offset']);capture_width=integer(holding['width'],1)
                    if capture_offset+capture_width>request['width'] or offset<capture_offset or offset+width>capture_offset+capture_width:
                        raise ValueError('operand must be within the explicitly retained physical-word slice')
                    if integer(hold['count'],1)*integer(hold['depth'],1)*integer(hold['width'],1) < capture_width:
                        raise ValueError('holding buffer smaller than captured slice')
                output_uses.append(dict(use,ready_cycle=ready,supply_kind='physical_read'))
            for use in local:
                group = groups.get(use['cost_group'])
                if group is None or group['model']!='compute' or group['workload']!=window['workload']:
                    raise ValueError('local use needs a compute group of this workload')
                if use.get('kind') not in ('register','feedback') or any(not isinstance(use.get(key),str) or not use[key].strip() for key in ('basis','producer_basis')):
                    raise ValueError('local use needs register|feedback kind, basis and producer_basis')
                if use['kind']=='feedback' and (not isinstance(use.get('feedback_basis'),str) or not use['feedback_basis'].strip()):
                    raise ValueError('feedback use needs explicit producer/iteration feedback_basis')
                hold = buffers.get(use['buffer'])
                if hold is None or hold.get('kind')!='register':raise ValueError('local supply needs an actual register buffer')
                cycle,lane,offset,width,ready = (integer(use[k],1 if k=='width' else 0) for k in ('cycle','lane','bit_offset','width','ready_cycle'))
                if cycle >= duration or lane >= integer(group['units'],1) or not isinstance(use.get('operand'),str) or not use['operand']:
                    raise ValueError('local operand use outside window/lane bounds or unnamed operand')
                key = (use['cost_group'],cycle,lane,use['operand'])
                if key in occupied:raise ValueError('duplicate physical/local operand position')
                occupied.add(key)
                capacity = integer(hold['count'],1)*integer(hold['depth'],1)*integer(hold['width'],1)
                if offset+width > capacity:raise ValueError('local operand slice exceeds register buffer capacity')
                if cycle < ready:raise ValueError('local operand used before declared producer is ready')
                output_uses.append(dict(use,supply_kind=use['kind']))
            issue_rows = window.get('issues', [])
            if not isinstance(issue_rows,list) or len(issue_rows)>4096:raise ValueError('issues must be an array of at most 4096 finite issues')
            issues, issue_positions = [], set()
            supplied = {(u['cost_group'],integer(u['cycle']),integer(u['lane']),u['operand']):u for u in output_uses}
            for issue in issue_rows:
                group = groups.get(issue['cost_group'])
                if group is None or group['model']!='compute' or group['workload']!=window['workload'] or not issue.get('basis'):
                    raise ValueError('issue needs actual workload compute cost_group and basis')
                cycle,units = integer(issue['cycle']),integer(group['units'],1)
                if cycle >= duration or (issue['cost_group'],cycle) in issue_positions:
                    raise ValueError('one finite issue per cost group/cycle within window required')
                issue_positions.add((issue['cost_group'],cycle))
                lanes,roles = issue['lanes'],issue['operands']
                if not isinstance(lanes,list) or not lanes or len(lanes)>65536 or any(type(lane) is not int or lane<0 or lane>=units for lane in lanes) or len(lanes)!=len(set(lanes)):
                    raise ValueError('issue lanes must be unique actual lane indices')
                if not isinstance(roles,list) or not roles or any(not isinstance(role,str) or not role for role in roles) or len(roles)!=len(set(roles)):
                    raise ValueError('issue operand roles must be unique nonempty names')
                if len(lanes)*len(roles)>65536:raise ValueError('finite issue exceeds 65536 operand positions')
                expected = {(issue['cost_group'],cycle,lane,role) for lane in lanes for role in roles}
                missing = sorted(expected-set(supplied))
                if missing:raise ValueError('issue missing declared operand positions: '+repr([list(p) for p in missing[:16]]))
                reasons = []
                if len(lanes)!=units:
                    reasons.append('partial active lanes; '+str(issue.get('partial_issue_basis','no full-lane extension demonstrated')))
                if group.get('operation_unit')=='MAC' and len(roles)<2:
                    reasons.append('MAC issue needs at least two independently reviewed input roles')
                if any(supplied[position].get('ready_cycle') is None for position in expected):
                    reasons.append('input read readiness capability absent')
                status = 'unverified' if reasons else 'pass'
                issues.append(dict(issue,status=status,declared_positions=len(expected),scope='declared finite issue only',unverified=reasons))
                if status=='pass':complete_groups.add(issue['cost_group'])
            windows.append({'id':label,'cycles':duration,'workload':window['workload'],
                            'physical_reads':len(window['reads']),'physical_writes':len(window['writes']),
                            'uses':output_uses,'issues':issues,'issue_completeness':'pass' if issues and all(r['status']=='pass' for r in issues) else 'unverified','service': [{'storage_reference':k[0],'instance':k[1],'cycle':k[2],'kind':k[3],'requests':v} for k,v in service.items()],
                            'scope':'declared finite window/issue only; operand-role semantics, local producer provenance/readiness, source completeness, values, repeated/tail schedule, holding lifetimes and full task dependencies require audit'})
        except (KeyError,ValueError,TypeError,SyntaxError,ZeroDivisionError,OverflowError) as e:
            errors.append(label+': '+str(e))
    missing_issue_groups = [gid for gid,group in groups.items() if group.get('model')=='compute' and (group.get('operations') is None or integer(group['operations'])>0) and gid not in complete_groups]
    unknown.extend('compute group '+gid+': no complete full-lane finite issue with declared operand roles; physical-only or partial evidence does not establish issue supply' for gid in missing_issue_groups)
    return {'ok':not errors,'errors':errors,'unknown':unknown,'windows':windows,'complete_issue_groups':sorted(complete_groups),'missing_issue_groups':missing_issue_groups}


def derive(candidate, values, requirements, expression, inventory):
    expanded = _templates.expand(candidate, values, requirements, expression, inventory)
    combined = copy.deepcopy(candidate)
    combined['schedule_windows'] = list(combined.get('schedule_windows', [])) + expanded['windows']
    result = _derive_windows(combined, values, requirements, expression, inventory)
    result['errors'] = expanded['errors'] + result['errors']
    result['unknown'] = expanded['unknown'] + result['unknown']
    result['ok'] = not result['errors']
    by_id = {row['id']: row for row in result['windows']}
    for template in expanded['templates']:
        if template.get('status') != 'pass':
            continue
        window = by_id.get(template['id'])
        if window is None:
            template['status'] = 'error'
        elif any(reason.startswith(template['id'] + '/') for reason in result['unknown']):
            template['status'] = 'unverified'
        template['physical_reads'] = window.get('physical_reads', 0) if window else 0
        template['physical_writes'] = window.get('physical_writes', 0) if window else 0
    result['templates'] = expanded['templates']
    return result
