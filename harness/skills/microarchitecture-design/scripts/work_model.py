"""Source-backed work declarations and candidate cost mapping; no project knowledge."""
import ast
import copy
import difflib
import hashlib
import json
import math
import importlib.util
from pathlib import Path

_source_spec = importlib.util.spec_from_file_location("work_source_scope", Path(__file__).with_name("source_quantities.py"))
_source_scope = importlib.util.module_from_spec(_source_spec)
_source_spec.loader.exec_module(_source_scope)


# Technical bound on one declaration's linear evaluation, not source work size.
MAX_SUM_TERMS=4096


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def citation_errors(references,sources,label,bindings=None):
    """Bind model-selected original spans or exact quotes without editing them."""
    errors=[]
    if not isinstance(references,list) or not references:return [label+' needs original-input references']
    for index,ref in enumerate(references):
        try:
            if not isinstance(ref,dict) or not _source_scope.is_original_source(ref.get('file')):
                raise ValueError('original-input reference required')
            text=sources[ref['file']]
            if not isinstance(text,str):raise ValueError('original source must be text')
            source_sha256=hashlib.sha256(text.encode()).hexdigest()
            if 'source_sha256' in ref and ref['source_sha256']!=source_sha256:
                raise ValueError('reference belongs to stale original source')
            lines=text.splitlines(keepends=True);quote=ref.get('quote')
            has_bounds='start' in ref or 'end' in ref
            if has_bounds:
                a=ref.get('start');b=ref.get('end',a)
                if type(a) is not int or type(b) is not int or not 1<=a<=b<=len(lines):
                    raise ValueError('original span needs integer bounds within frozen source')
                excerpt=''.join(lines[a-1:b]).rstrip('\r\n')
                if not excerpt:raise ValueError('original span has no nonempty excerpt')
            if 'quote' in ref:
                if not isinstance(quote,str) or not quote or quote not in text:
                    raise ValueError('exact original-input quote required')
                if has_bounds and quote not in excerpt:
                    raise ValueError('quote is outside selected original span')
                if not has_bounds:
                    if text.count(quote)!=1:raise ValueError('ambiguous quote needs correct line bounds')
                    position=text.index(quote);a=text[:position].count('\n')+1
                    b=text[:position+len(quote)-1].count('\n')+1;excerpt=quote
            elif not has_bounds:raise ValueError('original reference needs a selected span or exact quote')
            if bindings is not None:
                bindings.append({'location':label+'/references/'+str(index),'reference':copy.deepcopy(ref),
                    'resolved':{'file':ref['file'],'start':a,'end':b,'quote':excerpt,'source_sha256':source_sha256},
                    'scope':'model-selected frozen original evidence; actual excerpt and content fingerprint only, no semantic selection or model edits'})
        except (KeyError,TypeError,ValueError) as e:
            file=ref.get('file') if isinstance(ref,dict) else None
            text=sources.get(file,'') if isinstance(file,str) else ''
            hints=difflib.get_close_matches(str(ref.get('quote','')) if isinstance(ref,dict) else '',text.splitlines() if isinstance(text,str) else [],n=1,cutoff=0.35)
            errors.append(label+' reference: '+str(e)+('; possible original source line (recheck meaning): '+hints[0][:800] if hints else ''))
    return errors


def sum_terms(model):
    """Validate flat fixed-formula terms; repetition belongs to the item scope."""
    if set(model)!={'kind','unit','terms'}:raise ValueError('sum count requires exactly kind/unit/terms')
    unit=model['unit'];terms=model['terms']
    if not isinstance(unit,str) or not unit.strip():raise ValueError('sum count needs nonempty operation unit')
    if not isinstance(terms,list) or not 1<=len(terms)<=MAX_SUM_TERMS:
        raise ValueError('sum terms needs 1..'+str(MAX_SUM_TERMS)+' entries (execution-size limit)')
    fields={'matmul':{'kind','m','n','k','batch','unit'},
            'elementwise':{'kind','elements','operations_per_element','unit'},
            'reduction':{'kind','elements','groups','unit'}}
    for index,term in enumerate(terms):
        if not isinstance(term,dict) or term.get('kind') not in fields:
            raise ValueError('sum term '+str(index)+' must be matmul|elementwise|reduction, without nested/custom/unknown counts')
        if set(term)!=fields[term['kind']]:
            raise ValueError('sum term '+str(index)+' requires exactly '+','.join(sorted(fields[term['kind']]))+'; no term scope or repetition')
        if term['unit']!=unit:raise ValueError('sum term '+str(index)+' unit must match parent operation unit')
    return terms


def count(model,names,expression):
    """Supported algebra, not discovery of reference semantics or implementation costs."""
    if not isinstance(model,dict):raise ValueError('count must be an object')
    kind=model.get('kind')
    def integer(key,minimum=1):
        v=expression(model[key],names)
        if v<int(minimum) or v!=int(v):raise ValueError(key+' needs integer >= '+str(minimum))
        return v
    if kind=='matmul':return integer('m')*integer('n')*integer('k')*integer('batch')
    if kind=='elementwise':return integer('elements')*integer('operations_per_element')
    if kind=='reduction':return max(0,integer('elements')-1)*integer('groups')
    if kind=='sum':return sum(count(term,names,expression) for term in sum_terms(model))
    if kind=='custom':
        if not model.get('basis'):raise ValueError('custom count needs derivation basis')
        return integer('operations',0)
    if kind in ('none','unsupported') and model.get('reason'):return 0 if kind=='none' else None
    raise ValueError('count kind must be matmul|elementwise|reduction|sum|custom|none|unsupported')


def exclude_workload_elements(value,label):
    """Scope owns the workload axis; distinguish names, never guess from values."""
    tree=ast.parse(str(value),mode='eval')
    if any(isinstance(node,ast.Name) and node.id=='workload_elements' for node in ast.walk(tree)):
        raise ValueError(label+' cannot use workload_elements; this scope already owns the workload axis, or choose task scope for an explicit whole-task recipe')


def scope_count(scope,quantity,facts,expression):
    """Derive occurrence counts for declared scope axes, not arbitrary Python loops."""
    if not isinstance(scope,dict):raise ValueError('scope must be an object')
    kind=scope.get('kind')
    fields={'element':{'kind','axis','multiplicity'},'task':{'kind','axis'},
            'prefix':{'kind','axis','prefix_axis','first','step','multiplicity'}}
    if kind not in fields:raise ValueError('scope kind must be element|task|prefix')
    if set(scope)!=fields[kind]:raise ValueError(kind+' scope requires exactly '+','.join(sorted(fields[kind]))+'; no extra fields or derived totals')
    def axis(key):
        value=scope[key]
        if not isinstance(value,str) or not value.strip():raise ValueError(key+' must name a nonempty source scope axis')
        return value.strip()
    def integer(key):
        exclude_workload_elements(scope[key],kind+' scope '+key)
        value=expression(scope[key],facts)
        if value<0 or value!=int(value):raise ValueError(kind+' scope '+key+' needs nonnegative integer')
        return int(value)
    if type(quantity) not in (int,float) or quantity<0 or quantity!=int(quantity):raise ValueError('scope quantity needs nonnegative integer')
    n=int(quantity);axes=[axis('axis')]
    trace={'kind':kind,'axes':axes,'quantity':n}
    if kind=='task':trace.update(factor=1,total=1);return trace
    multiplicity=integer('multiplicity');trace['multiplicity']=multiplicity
    if kind=='element':factor=n
    else:
        prefix_axis=axis('prefix_axis')
        if prefix_axis==axes[0]:raise ValueError('prefix scope axis and prefix_axis must be distinct source axes')
        axes.append(prefix_axis);first=integer('first');step=integer('step')
        factor=n*(2*first+(n-1)*step)//2
        trace.update(first=first,step=step)
    trace.update(factor=factor,total=factor*multiplicity)
    return trace


def scoped_count(model,scope,quantity,facts,expression):
    trace=scope_count(scope,quantity,facts,expression)
    if not isinstance(model,dict):raise ValueError('count must be an object')
    numeric={'matmul':('m','n','k','batch'),'elementwise':('elements','operations_per_element'),
             'reduction':('elements','groups'),'custom':('operations',)}
    recipes=sum_terms(model) if model.get('kind')=='sum' else [model]
    if trace['kind']!='task':
        for recipe in recipes:
            for key in numeric.get(recipe.get('kind'),()):
                exclude_workload_elements(recipe[key],trace['kind']+' count '+key)
    names=dict(facts)
    if trace['kind']=='task':names['workload_elements']=quantity
    operations=count(model,names,expression)
    return operations,trace


def compact_summary(counts,items,profiles,profile_units,unknown):
    """Summarize executed work counts only; never infer or assign implementation costs."""
    unresolved=set(unknown);rows=[]
    for profile,values in counts.items():
        units={};categories={};active=[];missing=[]
        for wid,value in values.items():
            if not value['active']:continue
            active.append(wid)
            is_unknown=profile+'/'+wid in unresolved
            if is_unknown:missing.append(wid)
            unit=value['unit'];category=items[wid]['category']
            group=units.setdefault(unit,{'unit':unit,'work_ids':[],'operations':0,'occurrences':0,'unknown_work_ids':[]})
            if isinstance(unit,str) and unit.strip():group['work_ids_selector']={'unit':unit}
            group['work_ids'].append(wid);group['occurrences']+=value['repetitions']
            if group['operations'] is None or value['operations'] is None:group['operations']=None
            else:group['operations']+=value['operations']
            if is_unknown:group['unknown_work_ids'].append(wid)
            cat=categories.setdefault(category,{'category':category,'work_ids':[],'occurrences':0,'unknown_work_ids':[]})
            cat['work_ids'].append(wid);cat['occurrences']+=value['repetitions']
            if is_unknown:cat['unknown_work_ids'].append(wid)
        rows.append({'id':profile,'quantity':profiles[profile],'unit':profile_units[profile],
                     'active_work_ids':active,'by_unit':list(units.values()),
                     'by_category':list(categories.values()),'unknown_work_ids':missing})
    return {'profiles':rows,'scope':'aggregation of executed declared work counts only; occurrences sum program-derived scope totals, not workload quantity; no implementation cost or proof of source coverage, source interpretation or semantic completeness'}


def resolve_work_ids(declaration,profile,work):
    """Expand a deterministic selector against active source work in one workload."""
    if isinstance(declaration,list):
        if any(not isinstance(wid,str) or not wid for wid in declaration) or len(declaration)!=len(set(declaration)):
            raise ValueError('unique nonempty work_ids array required; empty only for explicit extra overhead')
        return list(declaration)
    if not isinstance(declaration,dict):raise ValueError('work_ids must be an explicit array or unit/category selector')
    keys=set(declaration)
    selectors=keys&{'unit','category'}
    if len(selectors)!=1 or keys-(selectors|{'exclude'}):raise ValueError('work_ids selector needs exactly one unit or category and optional exclude; no other keys')
    key=next(iter(selectors));selected_value=declaration[key]
    if not isinstance(selected_value,str) or not selected_value.strip():raise ValueError('work_ids selector '+key+' needs an actual nonempty source value')
    if key=='category' and selected_value not in ('compute','state','movement','control'):raise ValueError('work_ids selector category must be compute|state|movement|control')
    excluded=declaration.get('exclude',[])
    if not isinstance(excluded,list) or any(not isinstance(wid,str) or not wid for wid in excluded) or len(excluded)!=len(set(excluded)):
        raise ValueError('work_ids selector exclude must be a unique array of actual selected active work_ids')
    selected=[]
    for wid,item in work['items'].items():
        value=work['counts'][profile].get(wid)
        if value is None or not value['active']:continue
        source_value=value['unit'] if key=='unit' else item['category']
        if source_value==selected_value:selected.append(wid)
    if not selected:raise ValueError('work_ids selector has no active source matches for '+profile+' '+key+'='+selected_value)
    if not set(excluded)<=set(selected):raise ValueError('work_ids selector exclude contains unknown, inactive or nonmatching work_ids: '+str(sorted(set(excluded)-set(selected))))
    selected=[wid for wid in selected if wid not in set(excluded)]
    if not selected:raise ValueError('work_ids selector is empty after exclude; use explicit [] only for extra overhead')
    return selected


def validate(model,facts,sources,expression):
    errors=[];counts={};profiles={};profile_units={};items={};unknown=[];bindings=[]
    if not isinstance(model,dict):return {'ok':False,'errors':['work_model must be an object'],'counts':{},'items':{},'workloads':{},'unknown':[]}
    rows=model.get('items');workloads=model.get('workloads')
    if not isinstance(rows,list) or not rows:errors.append('work_model.items needs nonempty array');rows=[]
    if not isinstance(workloads,list) or not workloads:errors.append('work_model.workloads needs nonempty array');workloads=[]
    for w in workloads:
        try:
            wid=w['id'];n=expression(w['quantity'],facts)
            if 'items' in w:raise ValueError('profile items was removed; use quantity and unit')
            if not isinstance(w.get('unit'),str) or not w['unit'].strip():raise ValueError('workload element unit required')
            if not isinstance(wid,str) or not wid or wid in profiles or n<=0 or n!=int(n) or not w.get('basis'):raise ValueError('unique id, positive integer quantity and basis required')
            profiles[wid]=n;profile_units[wid]=w['unit'];counts[wid]={}
        except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError) as e:errors.append('work_model workload: '+str(e))
    if errors:return {'ok':False,'errors':errors,'counts':counts,'items':items,'workloads':profiles,'unknown':unknown,'scope':'workload root errors; item checks deferred until profiles are valid'}
    for row in rows:
        try:
            wid=row['id']
            if not isinstance(wid,str) or not wid or wid in items:raise ValueError('unique work id required')
            if row.get('category') not in ('compute','state','movement','control'):raise ValueError('invalid work category')
            if row.get('status') not in ('complete','unresolved'):raise ValueError('status must be complete|unresolved; no placeholder status')
            if row['category'] in ('movement','control') and row.get('count',{}).get('kind') not in ('none','unsupported'):raise ValueError('movement/control uses none for nonarithmetic work; bytes/elements/events belong to later traffic/schedule, and arithmetic effects need compute/state work')
            for key in ('semantics','condition','state_lifetime'):
                if not isinstance(row.get(key),str) or not row[key].strip():raise ValueError(key+' must explain the source semantics')
            if not isinstance(row.get('dependencies'),list) or any(not isinstance(x,str) for x in row['dependencies']):raise ValueError('dependencies must be work-id array')
            refs=citation_errors(row.get('references'),sources,'work/'+wid,bindings);errors.extend(refs)
            if 'repetitions' in row:raise ValueError('source repetitions was removed; declare scopes only, occurrence totals are program-derived')
            scopes=row['scopes']
            if not isinstance(scopes,dict) or set(scopes)!=set(profiles):raise ValueError('scopes must map every workload id exactly once')
            operation_unit=row['count'].get('unit')
            if row['count'].get('kind') not in ('none','unsupported') and (not isinstance(operation_unit,str) or not operation_unit.strip()):raise ValueError('operation count needs nonempty string unit')
            if operation_unit is not None and (not isinstance(operation_unit,str) or not operation_unit.strip()):raise ValueError('declared operation unit must be nonempty string')
            items[wid]=row
            for profile,n in profiles.items():
                operations,trace=scoped_count(row['count'],scopes[profile],n,facts,expression)
                repeat=trace['total']
                counts[profile][wid]={'repetitions':repeat,'operations_per_occurrence':operations,'operations':None if operations is None else operations*repeat,'scope_trace':trace,'unit':row['count'].get('unit'),'active':repeat>0}
                if repeat and (operations is None or row['status']=='unresolved'):unknown.append(profile+'/'+wid)
                if repeat and row['category']=='compute' and row['count'].get('kind')=='none':
                    raise ValueError('active compute work cannot have no operation count; workload='+profile+'; derived repetitions='+str(repeat))
        except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as e:errors.append('work/'+str(row.get('id','?') if isinstance(row,dict) else '?')+': '+str(e))
    for wid,row in items.items():
        for dep in row['dependencies']:
            if dep not in items or dep==wid:errors.append('work/'+wid+' dependency absent/self-referential: '+dep+'; prior-state recurrence belongs in state_lifetime')
    visiting=set();done=set()
    def visit(wid):
        if wid in visiting:raise ValueError('current-task dependency cycle at '+wid)
        if wid in done:return
        visiting.add(wid)
        for dep in items[wid]['dependencies']:
            if dep in items:visit(dep)
        visiting.remove(wid);done.add(wid)
    try:
        for wid in items:visit(wid)
    except ValueError as e:errors.append(str(e))
    return {'ok':not errors,'errors':errors,'counts':counts,'items':items,'workloads':profiles,'workload_units':profile_units,'unknown':sorted(set(unknown)),
            'citation_bindings':bindings,
            'compact_summary':compact_summary(counts,items,profiles,profile_units,unknown),
            'work_model_sha256':fingerprint(model),'scope':'declared source references, count algebra and graph only; not semantic completeness'}



def service_capacity(candidate, identifier, names, expression):
    """Resolve an explicitly selected existing byte/bit service, never infer one."""
    matches = [r for r in candidate.get('resource_checks', []) if r.get('id') == identifier]
    if len(matches) != 1:
        raise ValueError('service resource_check must name one actual resource check')
    resource = matches[0]
    # Cycles are semantically distinct from seconds even though the unit helper
    # treats a cycle count as dimensionless in ordinary engineering formulas.
    unit = ''.join(str(resource.get('unit', '')).split())
    parts = unit.split('/')
    if len(parts) != 2 or parts[1] not in ('cycle', 'cycles'):
        raise ValueError('bound service resource must use storage bits or bytes per cycle')
    scales = {'bit': 1, 'bits': 1, 'b': 1, 'byte': 8, 'bytes': 8, 'B': 8}
    if parts[0] not in scales:
        raise ValueError('bound service resource unit must be bits/cycle or bytes/cycle')
    capacity = expression(resource['supply_per_cycle'], names) * scales[parts[0]]
    if type(capacity) not in (int, float) or not math.isfinite(capacity) or capacity <= 0:
        raise ValueError('bound service supply must be positive finite')
    if not resource.get('basis'):
        raise ValueError('bound service needs a capability basis')
    return {'resource_check': identifier, 'bits_per_cycle': capacity,
            'supply_expression': resource['supply_per_cycle'], 'unit': resource['unit'],
            'basis': resource['basis']}


def operand_service_bounds(candidate, group, names, expression):
    """Cost actual distinct declared values, including legal packing and reuse."""
    declarations = group.get('operand_service', [])
    if not isinstance(declarations, list):
        raise ValueError('operand_service must be an array')
    bounds, seen = [], set()
    def integer(value, minimum=0):
        result = expression(value, names)
        if type(result) not in (int, float) or not math.isfinite(result) or result != int(result) or result < minimum:
            raise ValueError('operand service requires integer >= ' + str(minimum))
        return int(result)
    for declaration in declarations:
        required = {'resource_check', 'operations_per_batch', 'operands', 'basis'}
        if not isinstance(declaration, dict) or not required <= set(declaration) or set(declaration) - required - {'transfer_bits'} or not declaration.get('basis'):
            raise ValueError('operand service needs resource_check/operations_per_batch/operands/basis and optional transfer_bits')
        rid = declaration['resource_check']
        if rid in seen or rid not in group['resources']:
            raise ValueError('operand service resource must be unique and included in group resources')
        seen.add(rid)
        capacity = service_capacity(candidate, rid, names, expression)
        batch = integer(declaration['operations_per_batch'], 1)
        operands = declaration['operands']
        if not isinstance(operands, list) or not operands:
            raise ValueError('operand service needs actual distinct operand values')
        roles, payload, details = set(), 0, []
        for operand in operands:
            if not isinstance(operand, dict) or set(operand) != {'role', 'distinct_values', 'element_bits'} or not isinstance(operand['role'], str) or not operand['role'] or operand['role'] in roles:
                raise ValueError('operand role needs unique role/distinct_values/element_bits')
            roles.add(operand['role'])
            distinct, bits = integer(operand['distinct_values']), integer(operand['element_bits'], 1)
            payload += distinct * bits
            details.append({'role': operand['role'], 'distinct_values': distinct, 'element_bits': bits})
        transfer = integer(declaration['transfer_bits'], 1) if 'transfer_bits' in declaration else None
        charged = math.ceil(payload / transfer) * transfer if transfer else payload
        operations = group['operations']
        batches = None if operations is None else math.ceil(operations / batch)
        demand = None if batches is None else batches * charged
        minimum = None if demand is None else math.ceil(demand / capacity['bits_per_cycle'])
        bounds.append(dict(capacity, operations_per_batch=batch, operands=details,
            payload_bits_per_batch=payload, transfer_bits=transfer, charged_bits_per_batch=charged,
            batches=batches, demand_bits=demand, minimum_cycles=minimum, basis=declaration['basis'],
            scope='declared whole-task distinct-value service lower bound; final partial batch conservatively charged in full; packing/value reuse, actual capability and dependencies require audit'))
    return bounds


def bind_candidate(candidate,work,facts,expression):
    """One authoritative work-to-cost relation; derive counts without editing proposal."""
    c=copy.deepcopy(candidate);errors=[];cid=c['id'];mapped={};unknown=[];seen=set()
    def fail(message):errors.append(cid+' cost groups: '+message)
    if 'work_mapping' in c or 'compute_bounds' in c:fail('old work_mapping/compute_bounds were removed; declare cost_groups only')
    rows=c.get('cost_groups')
    if not isinstance(rows,list):fail('cost_groups must be an array');rows=[]
    resources={m['name'] for m in c.get('macros',[])}|{r['id'] for key in ('resource_checks','area_components') for r in c.get(key,[])}
    def unit_breakdown(ids,profile):
        groups={};non_counted=[];unresolved=set(work.get('unknown',[]))
        for wid in ids:
            value=work['counts'][profile][wid];unit=value['unit'];operations=value['operations']
            if unit is None and operations==0:
                non_counted.append(wid);continue
            group=groups.setdefault(unit,{'unit':unit,'work_ids':[],'operations':0,'unknown_work_ids':[]})
            group['work_ids'].append(wid)
            group['operations']=None if group['operations'] is None or operations is None else group['operations']+operations
            if operations is None or profile+'/'+wid in unresolved:group['unknown_work_ids'].append(wid)
        return json.dumps({'workload':profile,'by_unit':list(groups.values()),'non_counted_work_ids':non_counted},ensure_ascii=False)
    for row in rows:
        try:
            gid=row['id'];profile=row['workload'];model=row['model']
            if not isinstance(gid,str) or not gid or gid in seen:raise ValueError('unique cost group id required')
            seen.add(gid)
            if profile not in work['counts']:raise ValueError('unknown workload '+str(profile)+'; available_workloads='+json.dumps(list(work['counts']),ensure_ascii=False))
            declaration=copy.deepcopy(row['work_ids']);ids=resolve_work_ids(declaration,profile,work)
            row['work_ids']=ids;row['_work_binding']={'declaration':declaration,'resolved_work_ids':list(ids)}
            if not row.get('basis'):raise ValueError('whole-task cost basis required')
            if not isinstance(row.get('resources'),list) or not row['resources'] or len(row['resources'])!=len(set(row['resources'])) or not set(row['resources'])<=resources:raise ValueError('resources must name actual macros/resource_checks/area_components; available='+str(sorted(resources)))
            parts=[]
            for wid in ids:
                value=work['counts'][profile].get(wid)
                if value is None or not value['active']:raise ValueError('unknown or inactive work '+profile+'/'+wid)
                key=(profile,wid)
                if key in mapped:raise ValueError('duplicate work cost '+profile+'/'+wid+' already in '+mapped[key])
                mapped[key]=gid;parts.append(value)
            old=set(row)&{'operations','scheduled_cycles','minimum_cycles','compute_minimum_cycles','service_minimum_cycles','operand_service_bounds'}
            if old:raise ValueError(','.join(sorted(old))+' is program-derived; omit field')
            counted=[x for x in parts if x['unit'] is not None or x['operations'] is None or x['operations']>0]
            if model=='compute':
                if not counted:raise ValueError('compute cost needs counted source work')
                realization=row.get('realization','direct')
                if realization=='transformed':
                    if not row.get('equivalence'):raise ValueError('transformed whole-group count needs equivalence covering all work_ids')
                    row['operations']=count(row['count'],dict(facts,workload_elements=work['workloads'][profile]),expression)
                    if not isinstance(row['count'].get('unit'),str) or not row['count']['unit'].strip() or row['count'].get('kind')=='none':raise ValueError('transformed count needs nonempty string operation unit and non-none recipe')
                    row['operation_unit']=row['count']['unit']
                elif realization=='direct':
                    if 'count' in row:raise ValueError('direct count comes from source work; omit count')
                    op_units={x['unit'] for x in counted}
                    if None in op_units or len(op_units)!=1:raise ValueError('direct group needs commensurate operation units; split groups or provide whole-group transformed count; unit_breakdown='+unit_breakdown(ids,profile))
                    row['operation_unit']=next(iter(op_units));row['operations']=None if any(x['operations'] is None for x in counted) else sum(x['operations'] for x in counted)
                else:raise ValueError('realization must be direct|transformed')
                units=expression(row['units'],facts);rate=expression(row['operations_per_unit_cycle'],facts);overhead=expression(row['overhead_cycles'],facts)
                if units<=0 or units!=int(units) or rate<=0 or overhead<0 or overhead!=int(overhead):raise ValueError('physical units/rate and nonnegative integer overhead required')
                if row['operations'] is None:
                    unknown.append(gid);row['minimum_cycles']=None;row['scheduled_cycles']=overhead
                    row['compute_minimum_cycles']=None;row['_operand_service']=operand_service_bounds(c,row,facts,expression);row['service_minimum_cycles']=None
                else:
                    row['compute_minimum_cycles']=math.ceil(row['operations']/(units*rate))
                    row['_operand_service']=operand_service_bounds(c,row,facts,expression)
                    row['service_minimum_cycles']=max((r['minimum_cycles'] for r in row['_operand_service']),default=0)
                    row['minimum_cycles']=max(row['compute_minimum_cycles'],row['service_minimum_cycles']);row['scheduled_cycles']=row['minimum_cycles']+overhead
                if 'cycles' in row:raise ValueError('compute cycles are derived; omit cycles')
            elif model in ('cycles','unsupported'):
                if row.get('operand_service'):raise ValueError('operand_service belongs to compute cost groups only')
                if model=='cycles' and counted:raise ValueError('counted work requires compute cost, not arbitrary cycles; unit_breakdown='+unit_breakdown(ids,profile))
                if model=='unsupported':
                    if not row.get('reason'):raise ValueError('unsupported cost needs reason and explicit estimated cycles')
                    unknown.append(gid)
                cycles=expression(row['cycles'],facts)
                if cycles<0 or cycles!=int(cycles):raise ValueError('whole-task cycles need nonnegative integer')
                row['scheduled_cycles']=cycles
            else:raise ValueError('cost group model must be compute|cycles|unsupported')
        except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as e:fail(str(row.get('id','?'))+': '+str(e))
    for profile,counts in work['counts'].items():
        for wid,value in counts.items():
            if value['active'] and (profile,wid) not in mapped:fail('unmapped active work '+profile+'/'+wid)
    c['_unknown_cost']=unknown
    c['_work_trace']=[{'workload':p,'work_id':wid,'cost_group':gid} for (p,wid),gid in sorted(mapped.items())]
    c['_work_quantities']=work['workloads'];c['_work_units']=work.get('workload_units',{})
    return c,errors
