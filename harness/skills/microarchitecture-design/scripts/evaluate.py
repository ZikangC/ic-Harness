"""Architecture evidence checks; no model calls or project-specific formulas."""
import ast
import math
import operator
import re
import importlib.util
import copy
from pathlib import Path

_unit_spec=importlib.util.spec_from_file_location("budget_units",Path(__file__).with_name("budget_units.py"))
_units=importlib.util.module_from_spec(_unit_spec);_unit_spec.loader.exec_module(_units)

_quantity_spec=importlib.util.spec_from_file_location("source_quantities",Path(__file__).with_name("source_quantities.py"))
_quantities=importlib.util.module_from_spec(_quantity_spec);_quantity_spec.loader.exec_module(_quantities)

_work_spec=importlib.util.spec_from_file_location("work_model",Path(__file__).with_name("work_model.py"))
_work=importlib.util.module_from_spec(_work_spec);_work_spec.loader.exec_module(_work)

_helpers={}
for _name in ('storage_model','execution_model','execution_budget','abi_geometry','numeric_model'):
    _spec=importlib.util.spec_from_file_location(_name,Path(__file__).with_name(_name+'.py'))
    _module=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_module);_helpers[_name]=_module

def expression(text, names):
    """Arithmetic only: no Python execution, indexing, attributes or comprehensions."""
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
              ast.Mod: operator.mod, ast.Pow: operator.pow}
    calls = {'ceil': math.ceil, 'floor': math.floor, 'min': min, 'max': max, 'abs': abs}
    def visit(n):
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return n.value
        if isinstance(n, ast.Name) and n.id in names:
            return names[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in binary:
            a, b = visit(n.left), visit(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 64:
                raise ValueError('exponent too large')
            return binary[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -visit(n.operand) if isinstance(n.op, ast.USub) else visit(n.operand)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in calls and not n.keywords:
            return calls[n.func.id](*(visit(a) for a in n.args))
        if isinstance(n, ast.Name):
            raise ValueError('undefined identifier '+n.id+' in expression: '+str(text))
        raise ValueError('unsupported expression: ' + str(text))
    value = visit(ast.parse(str(text), mode='eval').body)
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e30:
        raise ValueError('non-finite or excessive result')
    return value


def derive_abi_geometry(row,names):
    return _helpers['abi_geometry'].derive(row,names,expression)


def derive_numeric_probes(rows, names):
    return _helpers['numeric_model'].derive(rows, names, expression)


def bind_unique_citations(requirements, sources):
    """Repair only unique excerpt locations; never invent/change quote or values."""
    bindings = []
    if not isinstance(requirements.get('facts'),list):
        return bindings
    for fact in requirements['facts']:
        if not isinstance(fact,dict) or not isinstance(fact.get('citations'),list):
            continue
        for citation in fact['citations']:
            if not isinstance(citation,dict) or not isinstance(citation.get('file'),str) or not isinstance(citation.get('quote'),str):
                continue
            source = sources.get(citation.get('file'), '')
            quote = citation.get('quote', '')
            if not quote or source.count(quote) != 1:
                continue
            position = source.index(quote)
            start = source[:position].count('\n') + 1
            end = start + quote.count('\n')
            if citation.get('start') != start or citation.get('end', citation.get('start')) != end:
                bindings.append({'fact': fact.get('id'), 'file': citation['file'], 'quote': quote,
                    'original_start': citation.get('start'), 'original_end': citation.get('end'),
                    'bound_start': start, 'bound_end': end, 'scope': 'unique exact excerpt; no value or quote changes'})
                citation['start'], citation['end'] = start, end
    return bindings


def dependency_order(facts):
    """Resolve source fact formulas independent of declaration order, without edits."""
    class Names(ast.NodeVisitor):
        def __init__(self):self.names=set()
        def visit_Name(self,node):self.names.add(node.id)
        def visit_Call(self,node):
            for arg in node.args:self.visit(arg)
            for arg in node.keywords:self.visit(arg.value)
    pending=[];errors=[]
    for row in facts:
        deps=set()
        if isinstance(row,dict) and 'formula' in row:
            try:
                visitor=Names();visitor.visit(ast.parse(str(row['formula']),mode='eval'));deps=visitor.names
            except SyntaxError as e:errors.append('fact '+str(row.get('id','?'))+' formula syntax: '+str(e))
        if isinstance(row,dict) and isinstance(row.get('convert'),dict) and isinstance(row['convert'].get('fact'),str):
            deps.add(row['convert']['fact'])
        if isinstance(row,dict) and isinstance(row.get('converted_from'),dict) and isinstance(row['converted_from'].get('fact'),str):
            deps.add(row['converted_from']['fact'])
        pending.append((row,deps))
    ordered=[];resolved=set()
    while pending:
        ready=[x for x in pending if x[1]<=resolved]
        if not ready:
            errors.append('fact dependency cycle or undefined identifiers: '+str({str(row.get('id','?')):sorted(deps-resolved) for row,deps in pending if isinstance(row,dict)}))
            ordered.extend(row for row,deps in pending);break
        for pair in ready:
            pending.remove(pair);row,deps=pair;ordered.append(row)
            if isinstance(row,dict) and isinstance(row.get('id'),str):resolved.add(row['id'])
    return ordered,errors


def source_quantities(sources):
    return _quantities.catalogue(sources)


def quantity_view(sources):
    return _quantities.view(sources)


def prepare_requirements(declaration,sources,*,declaration_mode=True):
    """Materialize explicit source selections/conversions, retaining raw requests.

    This does not select quantities, infer semantics or fix formulas. The caller
    keeps the declaration and binds revisions to its identity, never this result.
    """
    result=copy.deepcopy(declaration);bindings=[];errors=[]
    if not isinstance(result,dict) or not isinstance(result.get('facts'),list):
        return {'requirements':result,'bindings':[],'errors':['facts must be a list']}
    ordered,dependency_errors=dependency_order(result['facts']);errors.extend(dependency_errors)
    resolved={};values={}
    for row in ordered:
        name=row.get('id') if isinstance(row,dict) else None
        if not isinstance(name,str) or not name.isidentifier() or name in resolved:
            errors.append('invalid/duplicate fact id: '+str(name));continue
        try:
            if declaration_mode and ('value' in row or 'converted_from' in row):
                raise ValueError('raw source declaration uses source, convert or formula; value and converted_from are program-owned materialized fields')
            if sum(k in row for k in ('source','convert','formula','value'))!=1:
                raise ValueError('fact needs exactly one source, convert, formula or literal value')
            if 'source' in row:
                fact,receipt=_quantities.bind_fact(row,sources,_units);bindings.append(receipt)
            elif 'convert' in row or 'converted_from' in row:
                origin=row.get('convert',row.get('converted_from'))
                supplied={key:copy.deepcopy(row[key]) for key in ('id','citations') if key in row}
                supplied['convert']=copy.deepcopy(origin)
                if 'convert' in row and set(row)-{'id','convert','citations'}:raise ValueError('convert fact permits only id, convert and optional citations')
                if 'converted_from' in row and (set(row)-{'id','converted_from','value','unit','citations','prefix_base'} or 'value' not in row):
                    raise ValueError('invalid materialized conversion record')
                source_id=origin.get('fact') if isinstance(origin,dict) else None
                if source_id not in resolved:raise ValueError('conversion source fact is not resolved: '+str(source_id))
                if 'citations' not in supplied:supplied['citations']=copy.deepcopy(resolved[source_id].get('citations',[]))
                converted=_units.convert_fact(supplied,resolved,values)
                fact={'id':name,'value':converted['value'],'unit':converted['unit'],'citations':supplied['citations'],
                      'converted_from':copy.deepcopy(origin)}
                fact['prefix_base']=converted['prefix_base']
                if 'converted_from' in row and (type(row.get('value')) not in (int,float) or row['value']!=fact['value'] or row.get('unit')!=fact['unit'] or row.get('prefix_base')!=fact.get('prefix_base')):
                    raise ValueError('materialized conversion differs from fixed recalculation; stored result is not repaired')
                bindings.append({'fact':name,'conversion':copy.deepcopy(origin),
                                 'result':copy.deepcopy(converted),'materialized_fact':copy.deepcopy(fact),
                                 'scope':'fixed unit scaling of explicitly referenced quantity; no semantic inference'})
            else:
                fact=copy.deepcopy(row)
                if not isinstance(fact.get('unit'),str) or not fact['unit'].strip():raise ValueError('fact unit must be a nonempty string')
                if declaration_mode and (set(row)-{'id','formula','unit','citations'} or not {'id','formula','unit','citations'}<=set(row)):
                    raise ValueError('derived fact supports only id, formula, unit and exact source citations')
            resolved[name]=fact
            values[name]=expression(fact['formula'],values) if 'formula' in fact else fact['value']
        except (KeyError,ValueError,TypeError,SyntaxError,ZeroDivisionError,OverflowError) as error:
            errors.append('fact '+str(name)+': '+str(error))
    # Keep model-authored ordering and failed declarations; no omission or fill.
    result['facts']=[resolved.get(row.get('id'),row) if isinstance(row,dict) else row for row in result['facts']]
    bindings.extend(bind_unique_citations(result,sources))
    return {'requirements':result,'bindings':bindings,'errors':errors}


def validate_requirements(requirements, sources):
    prepared=prepare_requirements(requirements,sources,declaration_mode=False)
    requirements=prepared['requirements']
    errors, facts = list(prepared['errors']), {}
    def need(condition, message):
        if not condition:
            errors.append(message)
    if not isinstance(requirements.get('facts'),list):
        return {}, ['facts must be a list']
    # Full design requirements carry constraints and need explicit conditions.
    # Pure fact/work arithmetic also uses this entry without that declaration.
    if requirements.get('constraints') or 'open_conditions' in requirements:
        need(isinstance(requirements.get('open_conditions'),list),'open_conditions must be a list; empty is legal')
    ordered_facts,dependency_errors=dependency_order(requirements['facts']);errors.extend(dependency_errors)
    for f in ordered_facts:
        if not isinstance(f, dict):
            errors.append('fact must be an object'); continue
        name = f.get('id')
        if not isinstance(name,str) or not name.isidentifier():
            errors.append('fact id must be an identifier string: '+str(name)); continue
        if sum(k in f for k in ('value','formula','source','convert'))!=1 or 'source' in f or 'convert' in f:
            errors.append('fact not resolved to exactly one value or formula: '+name);continue
        if 'formula' not in f and (type(f.get('value')) not in (int,float) or not math.isfinite(f['value'])):
            errors.append('fact value must be finite numeric: '+name); continue
        need(name.isidentifier() and name not in facts, 'invalid/duplicate fact ' + name)
        citations = f.get('citations', [])
        if not isinstance(citations,list):
            errors.append('citations must be a list for '+name);continue
        need(bool(citations), 'fact without input citation: ' + name)
        citation_errors_before = len(errors)
        for c in citations:
            if not isinstance(c,dict) or not isinstance(c.get('file'),str) or not isinstance(c.get('quote'),str) or type(c.get('start')) is not int or type(c.get('end',c.get('start'))) is not int:
                errors.append('citation field types invalid for '+name); continue
            lines = sources.get(c['file'], '').splitlines()
            start, end = c['start'], c.get('end', c['start'])
            valid=1 <= start <= end <= len(lines) and bool(c['quote']) and c['quote'] in '\n'.join(lines[start-1:end])
            if not valid:
                near='\n'.join(str(i+1)+': '+lines[i] for i in range(max(0,start-3),min(len(lines),end+2)))[:1400]
                errors.append('invalid citation for '+name+'; copy an exact contiguous excerpt including original punctuation/markup, or correct the bounds. Source near declared range:\n'+near)
        if 'formula' not in f and 'converted_from' not in f and len(errors) == citation_errors_before:
            numbers = []
            for citation in citations:
                # The same lexical reader handles underscores, encoded integers,
                # Q components and source-selector literals. No hand conversion.
                numbers.extend(x['value'] for x in _quantities._numbers(citation['quote']))
                quote = re.sub(r'(?<=\d)\.\.(?=\d)', ' ', citation['quote'])
                numeric_pattern = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
                for match in re.finditer(numeric_pattern, quote):
                    number = float(match.group())
                    numbers.append(number)
                    # A binary subtraction sign is not the sign of its operand.
                    if number < 0 and match.start() and (quote[match.start()-1].isalnum() or quote[match.start()-1] in ')]'):
                        numbers.append(-number)
                numbers.extend(int(n, 16) for n in re.findall(r"\d+'[hH]([0-9a-fA-F]+)", quote))
                numbers.extend(int(n) for pair in re.findall(r'Q(\d+)\.(\d+)', quote) for n in pair)
                numbers.extend(float(n)/100 for n in re.findall(r'(\d+(?:\.\d+)?)\s*[%％]', quote))
                # Literal cardinal words used in port/resource descriptions.
                cardinal = {'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
                numbers.extend(cardinal[n] for n in quote if n in cardinal)
                english = ['zero','one','two','three','four','five','six','seven','eight','nine','ten']
                numbers.extend(i for i,n in enumerate(english) if re.search(r'\b'+n+r'\b',quote,re.I))
            need(any(math.isclose(f['value'], n, rel_tol=0, abs_tol=1e-12) for n in numbers),
                 'literal fact absent from cited input; use derivation formula for conversion or composite quantity: ' + name + '; cited numeric alternatives=' + str(sorted(set(numbers))))
        try:value = expression(f['formula'], facts) if 'formula' in f else f['value']
        except (ValueError,SyntaxError,ZeroDivisionError,TypeError,OverflowError) as e:
            errors.append('fact '+name+': '+str(e));continue
        need(type(value) in (int, float) and math.isfinite(value), 'invalid fact value ' + name)
        facts[name] = value
    for con in requirements.get('constraints', []):
        if not isinstance(con,dict) or not isinstance(con.get('id'),str) or con.get('limit') not in facts:
            errors.append('constraint must reference a valid fact identifier'); continue
        if con.get('quantity') not in ('area','capacity','clock','numerical','functional','task_cycles','cycles_per_item','items_per_second','other'):
            errors.append('missing/invalid constraint quantity: '+con['id'])
        if con.get('kind') not in ('hard','target') or con.get('relation') not in ('<=','>=','==') or not con.get('unit'):
            errors.append('invalid constraint kind/relation/unit: '+con['id'])
        if con.get('verification','estimate') not in ('estimate','implementation'):
            errors.append('invalid constraint verification: '+con['id'])
        if con.get('quantity') in ('area','capacity','task_cycles','cycles_per_item','items_per_second') and con.get('verification','estimate')!='estimate':
            errors.append('calculable budget cannot be deferred: '+con['id'])
        if con.get('quantity') in ('task_cycles','cycles_per_item','items_per_second') and not isinstance(con.get('workload'),str):
            errors.append('performance constraint needs workload id: '+con['id'])
    for rule in requirements.get('storage_rules',[]):
        try:
            for key in ('min_depth','width_multiple'):
                v=expression(rule[key],facts)
                if v <= 0 or v!=int(v):raise ValueError('must be a positive integer')
        except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError) as e:
            errors.append('storage rule must be executable numeric expression: '+str(e))
    parameters=requirements.get('runtime_parameters')
    if not isinstance(parameters,list):errors.append('runtime_parameters must be an explicit array (empty only if no runtime domain)')
    else:
        try:_helpers['storage_model']._parameter_domains(requirements,facts)
        except (KeyError,ValueError,TypeError) as e:errors.append(str(e))
        for parameter in parameters:
            if isinstance(parameter,dict):errors.extend(_work.citation_errors(parameter.get('citations'),sources,'runtime_parameters/'+str(parameter.get('id'))))
    for rule in requirements.get('storage_rules',[]):
        for key in ('read_ports','write_ports','read_latency_cycles'):
            if key in rule:
                try:
                    v=expression(rule[key],facts)
                    if v<0 or v!=int(v):raise ValueError('nonnegative integer required')
                except (KeyError,ValueError,TypeError,SyntaxError) as e:errors.append('storage capability '+key+': '+str(e))
    errors.extend(_units.fact_dimension_errors(ordered_facts))
    for con in requirements.get('constraints', []):
        if not isinstance(con,dict):continue
        kind=con.get('quantity')
        expected={'area':{'area':1},'capacity':{'storage':1},'clock':{'time':-1}}.get(kind)
        if expected is not None:
            parsed=_units.unit(con.get('unit',''))
            if parsed is None or parsed[0]!=expected:errors.append('unsupported or wrong constraint unit: '+str(con.get('id')))
    if requirements.get('constraints') or 'storage_area_model' in requirements:
        try:_units.macro_density(requirements,facts)
        except (KeyError,ValueError,TypeError) as e:errors.append(str(e))
    return facts, errors


# These derived quantities have one authoritative producer: this script.
AUTO_QUANTITIES = {'area','capacity','task_cycles','cycles_per_item','items_per_second'}
SECTIONS = ('cost_groups','traffic','buffers','schedule_windows','area_components')


def design_data(document):
    return {k:v for k,v in document.items() if k not in ('architecture_md','tool_receipt')}



def link_traffic_service(candidate, row, derived, names):
    """Bind an actual wire transfer to an explicit service and accounting boundary."""
    declaration = row['service']
    required = {'resource_check', 'boundary', 'basis'}
    if not isinstance(declaration,dict) or not required <= set(declaration) or set(declaration)-required-{'cost_group','operand_relation'} or not declaration.get('basis'):
        raise ValueError('traffic service needs resource_check/boundary/basis and run cost_group')
    boundary = declaration['boundary']
    if boundary not in ('run','preloaded_before_run'):
        raise ValueError('traffic service boundary must be run|preloaded_before_run')
    capacity = _work.service_capacity(candidate,declaration['resource_check'],names,expression)
    demand = derived['wire_bytes']*8
    minimum = max(math.ceil(demand/capacity['bits_per_cycle']),derived.get('service_cycles_lower_bound',0))
    result = dict(capacity,boundary=boundary,demand_bits=demand,wire_bytes=derived['wire_bytes'],
                  minimum_cycles=minimum,basis=declaration['basis'],
                  scope='declared transfer and capability lower bound, not latency/overlap/completion proof; boundary legality requires original-input review')
    if boundary=='preloaded_before_run':
        if 'cost_group' in declaration or 'operand_relation' in declaration:raise ValueError('preloaded_before_run service is outside RUN; omit cost_group and operand_relation rather than charge a RUN group')
        result['status']='pre_run_cost_disclosed'
    else:
        groups=[g for g in candidate['cost_groups'] if g['id']==declaration.get('cost_group')]
        if len(groups)!=1:raise ValueError('run traffic service needs an actual cost_group')
        group=groups[0]
        if declaration['resource_check'] not in group['resources']:
            raise ValueError('traffic service resource must be included in charged group resources')
        if row.get('workload',group['workload'])!=group['workload']:
            raise ValueError('traffic service and charged cost_group workloads differ')
        operand_projection=any(r['resource_check']==declaration['resource_check'] for r in group.get('_operand_service',[]))
        relation=declaration.get('operand_relation')
        if operand_projection and relation not in ('same_stream','independent'):
            raise ValueError('traffic and operand service share group/resource; explicit operand_relation same_stream|independent required with ownership basis')
        if not operand_projection and relation is not None:
            raise ValueError('operand_relation names no operand service on this charged group/resource')
        result.update(cost_group=group['id'],workload=group['workload'],status='run_bound')
        if relation is not None:result['operand_relation']=relation
    return result


def apply_service_links(ledger,errors,label,execution_groups=()):
    """Price linked whole transfers; aggregate shared capacities without a trace IR."""
    groups={g['id']:g for g in ledger['cost_groups']}
    linked={}
    for traffic in ledger['traffic']:
        service=traffic.get('service')
        if not service or service['boundary']!='run':continue
        key=(service['cost_group'],service['resource_check'])
        row=linked.setdefault(key,dict(resource_check=service['resource_check'],demand_bits=0,
                   bits_per_cycle=service['bits_per_cycle'],minimum_cycles=0,traffic_ids=[],same_stream_demand_bits=0,independent_demand_bits=0))
        row['demand_bits']+=service['demand_bits'];row['traffic_ids'].append(traffic['id'])
        key='same_stream_demand_bits' if service.get('operand_relation')=='same_stream' else 'independent_demand_bits'
        row[key]+=service['demand_bits']
        row['minimum_cycles']=max(row['minimum_cycles'],service['minimum_cycles'])
    for (gid,rid),bound in linked.items():
        bound['minimum_cycles']=max(bound['minimum_cycles'],math.ceil(bound['demand_bits']/bound['bits_per_cycle']))
        group=groups.get(gid)
        if group is None:continue  # The actual group declaration already failed.
        if gid not in execution_groups and group['scheduled_cycles']<bound['minimum_cycles']:
            errors.append(label+' traffic service/'+gid+'/'+rid+': charged group cycles '+str(group['scheduled_cycles'])+' below linked transfer service lower bound '+str(bound['minimum_cycles']))
    for gid,group in groups.items():
        bounds={r['resource_check']:dict(resource_check=r['resource_check'],demand_bits=r['demand_bits'],
                     bits_per_cycle=r['bits_per_cycle'],minimum_cycles=r['minimum_cycles'],operand_service=True)
                for r in group.get('operand_service_bounds',[]) if r['demand_bits'] is not None}
        for (target,rid),bound in linked.items():
            if target!=gid:continue
            if rid in bounds:
                old=bounds[rid]
                old['demand_bits']=max(old['demand_bits'],bound['same_stream_demand_bits'])+bound['independent_demand_bits']
                old['minimum_cycles']=max(old['minimum_cycles'],bound['minimum_cycles'],math.ceil(old['demand_bits']/old['bits_per_cycle']))
                old['traffic_ids']=bound['traffic_ids']
                old['scope']='explicit same_stream projections use max; declared independent traffic adds; ownership evidence requires audit'
            else:bounds[rid]=bound
        for bound in bounds.values():
            if gid not in execution_groups and group['scheduled_cycles']<bound['minimum_cycles']:
                message=label+' traffic service/'+gid+'/'+bound['resource_check']+': charged group cycles '+str(group['scheduled_cycles'])+' below linked transfer service lower bound '+str(bound['minimum_cycles'])
                if message not in errors:errors.append(message)
        group['service_bounds']=list(bounds.values())


def check_ledgers(candidate, values, density=None):
    """Fixed formulas over declarations; necessary bounds, never schedule proof."""
    cid=candidate['id']; errors=[]; results={}; unsupported=[]; evidence_gaps=[]
    def number(row,key):return expression(row[key],values)
    def positive(v,integer=False):
        if v<=0 or (integer and v!=int(v)):raise ValueError('positive'+(' integer' if integer else '')+' required')
    def nonnegative(v,integer=False):
        if v<0 or (integer and v!=int(v)):raise ValueError('nonnegative'+(' integer' if integer else '')+' required')
    forbidden={'traffic':{'total_bytes','minimum_inflight','reserved_response_bytes'},'buffers':{'allocated_bits'},'area_components':{'lower_mm2','upper_mm2'}}
    inventory={};bits=0
    for m in candidate.get('macros',[]):
        try:
            if m['name'] in inventory:raise ValueError('duplicate macro name')
            count,depth,width=(number(m,k) for k in ('count','depth','width'))
            for v in (count,depth,width):positive(v,True)
            if m['kind'] not in ('SRAM','ROM') or not m.get('ports_and_schedule'):raise ValueError('macro kind/access schedule missing')
            for rule in candidate['_requirements'].get('storage_rules',[]):
                if rule['kind']==m['kind']:
                    if depth<expression(rule['min_depth'],values) or width%expression(rule['width_multiple'],values):raise ValueError('macro dimensions violate input storage rule')
            inventory[m['name']]={'kind':m['kind'],'count':count,'depth':depth,'width':width,'bits':count*depth*width}
            bits+=count*depth*width
        except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError) as e:errors.append(cid+' macro/'+str(m.get('name','?'))+': '+str(e))
    results['macro_inventory_bits']=bits
    abi=candidate.get('_abi_geometry',{})
    errors.extend(cid+' ABI geometry/'+error for error in abi.get('errors',[]))
    unsupported.extend('abi_geometry/'+error for error in abi.get('unknown',[]))
    if 'abi_geometry' in candidate:results['abi_geometry']=abi.get('details',[])
    storage=_helpers['storage_model'].derive(candidate,values,candidate['_requirements'],expression,inventory)
    execution=_helpers['execution_model'].derive(candidate,values,candidate['_requirements'],expression,inventory)
    errors.extend(cid+' storage/'+e for e in storage['errors']);errors.extend(cid+' execution/'+e for e in execution['errors'])
    unsupported.extend('storage/'+e for e in storage['unknown'])
    # Finite execution witnesses are optional implementation evidence. Missing
    # coverage does not invalidate independently costed coarse schedules.
    evidence_gaps.extend('execution/'+e for e in execution['unknown'])
    results['storage_derivation']={k:v for k,v in storage.items() if k!='buffers'}
    results['execution_derivation']={k:v for k,v in execution.items() if k!='windows'}
    for section in SECTIONS:
        results[section]=[]
        rows=candidate.get(section);coverage=candidate.get('coverage',{}).get(section,{})
        if not isinstance(rows,list) or not coverage.get('basis') or coverage.get('status')!=('covered' if rows else 'not_applicable'):
            errors.append(cid+' missing list/coverage '+section);continue
        seen=set()
        for row in rows:
            try:
                if not isinstance(row,dict) or not isinstance(row.get('id'),str) or row['id'] in seen or not row.get('basis'):raise ValueError('unique id and basis required')
                seen.add(row['id']);r={'id':row['id']}
                old=set(row)&forbidden.get(section,set())
                if old:raise ValueError('program-derived fields must be omitted: '+','.join(sorted(old)))
                if section=='cost_groups':
                    scheduled=number(row,'scheduled_cycles');nonnegative(scheduled,True)
                    r.update(workload=row['workload'],work_ids=row['work_ids'],binding=row['_work_binding'],resources=row['resources'],model=row['model'],scheduled_cycles=scheduled)
                    if row['model']=='compute':
                        r.update(operations=row['operations'],operation_unit=row['operation_unit'],minimum_cycles=row['minimum_cycles'],compute_minimum_cycles=row['compute_minimum_cycles'],service_minimum_cycles=row['service_minimum_cycles'],operand_service_bounds=row['_operand_service'],units=number(row,'units'),operations_per_unit_cycle=number(row,'operations_per_unit_cycle'),scope='maximum of compute and declared service lower bounds plus whole-task overhead; no schedule proof')
                        if not row.get('operand_service'):evidence_gaps.append('operand_service/'+row['id']+': no executed distinct-operand/capability link; explained conservative rate and engineering review still required')
                    if row['id'] in candidate.get('_unknown_cost',[]):unsupported.append('cost_groups/'+row['id']);r['status']='unverified'
                elif section=='traffic':
                    payload=number(row,'payload_bytes');count=number(row,'transfers');align=number(row,'alignment_bytes');beat=number(row,'transaction_bytes')
                    for v in (payload,count,align,beat):positive(v,True)
                    padded=math.ceil(payload/align)*align;total=padded*count
                    # Each independently aligned payload has its own last transaction.
                    transactions=math.ceil(padded/beat)*count
                    r.update(total_bytes=total,transactions=transactions,wire_bytes=transactions*beat)
                    model=row.get('model')
                    if model=='unsupported':
                        if not row.get('reason'):raise ValueError('unsupported model requires reason')
                        r.update(status='unverified',reason=row['reason']);unsupported.append(section+'/'+row['id'])
                    elif model=='posted_write':
                        if any(k in row for k in ('latency_cycles','transactions_per_cycle','inflight','response_buffer_bytes')):raise ValueError('posted_write has no read-response fields')
                        r.update(minimum_inflight=0,reserved_response_bytes=0,scope='traffic only; write acceptance/completion schedule still needs evidence')
                    elif model=='continuous_reserved':
                        latency=number(row,'latency_cycles');rate=number(row,'transactions_per_cycle');flight=number(row,'inflight');buffer=number(row,'response_buffer_bytes')
                        nonnegative(latency,True);positive(rate);positive(flight,True);nonnegative(buffer,True)
                        minimum=math.ceil(latency*rate)
                        r.update(minimum_inflight=minimum,reserved_response_bytes=flight*beat,
                                 service_cycles_lower_bound=max(math.ceil(transactions/rate),latency),
                                 scope='steady request rate with reserved responses; lower bound is not complete latency')
                        if flight<minimum:raise ValueError('inflight below latency*rate lower bound '+str(minimum)+'; reduce sustained rate and recost schedule or increase credits')
                        if buffer<flight*beat:raise ValueError('response queue below reserved outstanding bytes')
                    else:raise ValueError('traffic model must be continuous_reserved|posted_write|unsupported')
                    if 'service' in row and model!='unsupported':r['service']=link_traffic_service(candidate,row,r,values)
                    else:evidence_gaps.append('traffic_service/'+row['id']+': transfer/capability/accounting-boundary link not checked')
                elif section=='buffers':
                    r=next((v for v in storage['buffers'] if v['id']==row['id']),{'id':row['id'],'status':'not_derived'})
                elif section=='schedule_windows':
                    r=next((v for v in execution['windows'] if v['id']==row['id']),{'id':row['id'],'status':'not_derived'})
                elif section=='area_components':
                    if row.get('kind') not in ('logic','macro'):raise ValueError('area kind must be logic or macro')
                    if row['kind']=='macro':
                        if density is not None:raise ValueError('input density supplies all macro costs automatically; omit macro area rows')
                        if 'count' in row:raise ValueError('macro count comes from inventory; omit count')
                        macro=inventory.get(row.get('resource_reference'))
                        if macro is None:raise ValueError('macro cost needs actual inventory reference')
                        count=macro['count']
                    else:count=number(row,'count');nonnegative(count)
                    low=number(row,'unit_area_lower');high=number(row,'unit_area_upper')
                    nonnegative(low);nonnegative(high)
                    if high<low:raise ValueError('area interval reversed')
                    if not row.get('source') or not row.get('uncertainty'):raise ValueError('area source/uncertainty missing')
                    r.update(lower_mm2=count*low,upper_mm2=count*high)
                results[section].append(r)
            except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as e:
                errors.append(cid+' '+section+'/'+str(row.get('id','?') if isinstance(row,dict) else '?')+': '+str(e))
    # A priced execution component is the actual charged interval; the group's
    # scheduled_cycles remains only a resource lower bound in that mode. Defer
    # its service comparison until charge() validates the template and interval.
    execution_groups=set()
    for workload in candidate.get('workloads',[]):
        if not isinstance(workload,dict) or not isinstance(workload.get('components'),list):continue
        for component in workload['components']:
            if not isinstance(component,dict) or component.get('composition')!='execution' or not isinstance(component.get('references'),list):continue
            execution_groups.update(gid for gid in component['references'] if isinstance(gid,str))
    apply_service_links(results,errors,cid,execution_groups)
    components=results['area_components'];macro_area=bits/density['bits_per_mm2'] if density else 0
    if density:results['macro_inventory_area_mm2']=macro_area
    results['area_total']={'lower_mm2':macro_area+sum(x['lower_mm2'] for x in components),'upper_mm2':macro_area+sum(x['upper_mm2'] for x in components)}
    represented=[];covered_registers=set()
    for row in candidate.get('area_components',[]):
        if not isinstance(row,dict):continue
        if row.get('kind')=='macro':represented.append(row.get('resource_reference'))
        elif row.get('kind')=='logic':
            links=row.get('covers_buffers',[])
            if not isinstance(links,list):errors.append(cid+' covers_buffers must be a list')
            else:covered_registers.update(x for x in links if isinstance(x,str))
    if not density:
        if len(represented)!=len(set(represented)):errors.append(cid+' duplicate macro area reference')
        for name in inventory:
            if name not in represented:errors.append(cid+' macro has no area cost basis: '+name)
    for b in candidate.get('buffers',[]):
        if b.get('kind')=='register' and b.get('id') not in covered_registers:errors.append(cid+' register buffer has no linked logic cost: '+str(b.get('id')))
    groups={}
    for f in candidate.get('abi_fields',[]):
        try:
            if not f.get('carrier'):raise ValueError('explicit carrier identity required')
            offset,width,carrier=(number(f,k) for k in ('offset','width','carrier_width'))
            for old_offset,old_end,old_carrier,name in groups.setdefault(f['carrier'],[]):
                if carrier!=old_carrier:raise ValueError('carrier widths disagree')
                if max(offset,old_offset)<min(offset+width,old_end):raise ValueError('overlaps field '+name)
            groups[f['carrier']].append((offset,offset+width,carrier,f['name']))
        except (KeyError,TypeError,ValueError,SyntaxError) as e:errors.append(cid+' ABI carrier: '+str(e))
    results['unsupported_models']=unsupported
    results['evidence_gaps']=evidence_gaps
    return results,errors


def check_workloads(candidate,names,facts,constraints,errors,ledger):
    """Derive whole-task sums and tracing from costs referenced once in schedule."""
    results={};workloads=candidate.get('workloads',[]);profiles=candidate.get('_work_quantities',{});charged_execution=set()
    if not isinstance(workloads,list) or not workloads:
        errors.append(candidate['id']+' needs explicit complete-task workloads');return results
    if {w.get('id') for w in workloads}!=set(profiles):errors.append(candidate['id']+' candidate workloads must match source profiles')
    groups={x['id']:x for x in ledger['cost_groups']}
    for w in workloads:
        try:
            profile=w['id']
            if profile in results:raise ValueError('duplicate workload id')
            old=set(w)&{'items','quantity','unit','clock_hz','task_cycles','cycles_per_item','items_per_second'}
            if old:raise ValueError('program-derived workload fields must be omitted: '+','.join(sorted(old)))
            quantity=profiles[profile];clock_id=w['clock_fact']
            fact=next((f for f in candidate['_requirements']['facts'] if f['id']==clock_id),None)
            parsed=_units.unit(fact.get('unit','')) if fact else None
            if parsed is None or parsed[0]!={'time':-1}:raise ValueError('clock_fact must name a cited input frequency fact, never a design assumption or literal Hz value')
            clock=facts[clock_id]*parsed[1]
            if clock<=0:raise ValueError('positive clock required')
            components=w['components'];costs=[];seen=set();used=set();used_execution=set()
            if not components:raise ValueError('missing task components')
            for component in components:
                if component['id'] in seen or not component.get('basis'):raise ValueError('unique component id and schedule basis required')
                seen.add(component['id']);ids=component['references'];mode=component['composition']
                if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):raise ValueError('references must be unique nonempty cost group ids')
                if used&set(ids):raise ValueError('duplicate complete-task cost reference')
                used.update(ids);parts=[groups[x] for x in ids]
                if any(x['workload']!=profile for x in parts):raise ValueError('cost reference has different workload')
                lower=max(x['scheduled_cycles'] for x in parts)
                execution_evidence={}
                if mode=='execution':
                    cost,execution_evidence=_helpers['execution_budget'].charge(component,groups,ledger['execution_derivation'].get('templates',[]),names,expression,used_execution)
                elif mode=='serial':
                    if 'cycles' in component:raise ValueError('serial cycles are derived; omit cycles')
                    cost=sum(x['scheduled_cycles'] for x in parts)
                elif mode=='parallel':
                    if 'cycles' in component:raise ValueError('parallel cycles are derived; omit cycles')
                    resource_ids=[v for x in parts for v in x['resources']]
                    if len(resource_ids)!=len(set(resource_ids)):raise ValueError('parallel groups share declared resources; express an overlap schedule with explicit arbitration/port basis')
                    cost=lower
                elif mode=='overlap':
                    cost=expression(component['cycles'],names)
                    if cost<lower:raise ValueError('overlap interval below whole-task cost lower bound '+str(lower))
                else:raise ValueError('composition must be serial|parallel|overlap|execution')
                if cost<0 or cost!=int(cost):raise ValueError('nonnegative integer cycle count required')
                service_bounds={}
                for part in parts:
                    for bound in part.get('service_bounds',[]):
                        rid=bound['resource_check']
                        total=service_bounds.setdefault(rid,{'resource_check':rid,'demand_bits':0,'bits_per_cycle':bound['bits_per_cycle'],'minimum_cycles':0,'cost_groups':[]})
                        total['demand_bits']+=bound['demand_bits'];total['cost_groups'].append(part['id'])
                        total['minimum_cycles']=max(total['minimum_cycles'],bound['minimum_cycles'])
                for bound in service_bounds.values():
                    bound['minimum_cycles']=max(bound['minimum_cycles'],math.ceil(bound['demand_bits']/bound['bits_per_cycle']))
                    bound['scope']='necessary shared service bound; capability, ownership and legal overlap require audit'
                    if cost<bound['minimum_cycles']:raise ValueError('component '+component['id']+' below shared service '+bound['resource_check']+' lower bound '+str(bound['minimum_cycles']))
                costs.append(dict({'id':component['id'],'cycles':cost,'composition':mode,'references':ids,'basis':component['basis'],'service_bounds':list(service_bounds.values())},**({'execution':execution_evidence} if execution_evidence else {})))
            expected={gid for gid,g in groups.items() if g['workload']==profile}
            if used!=expected:raise ValueError('schedule must reference all workload cost groups exactly once; missing='+str(sorted(expected-used)))
            task=sum(c['cycles'] for c in costs)
            charged_execution.update(used_execution)
            if task<=0:raise ValueError('complete task cycles must be positive')
            results[profile]={'quantity':quantity,'unit':candidate['_work_units'][profile],'clock_hz':clock,'components':costs,'task_cycles':task,'cycles_per_item':task/quantity,'items_per_second':clock*quantity/task,'clock_input_facts':[clock_id],'scope':'sum of nonoverlapped whole-task intervals; overlap arbitration, dependencies and completeness require audit'}
        except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError) as e:errors.append(candidate['id']+' workload/'+str(w.get('id','?'))+': '+str(e))
    expected_execution={(t['id'],p['id']) for t in ledger['execution_derivation'].get('templates',[]) if t.get('status') in ('pass','unverified') for p in t['components']}
    priced_templates={template for template,phase in charged_execution}
    unpriced=expected_execution-charged_execution
    omitted_cost={pair for pair in unpriced if pair[0] in priced_templates}
    if omitted_cost:
        ledger['unsupported_models'].append('execution_templates/unpriced_phases: '+str(sorted(omitted_cost)))
    if unpriced-omitted_cost:
        ledger['evidence_gaps'].append('execution_templates/unpriced_phases: '+str(sorted(unpriced-omitted_cost)))
    return results


def check_resources(candidate, names, errors):
    """Reject declared demand exceeding declared service; not a schedule proof."""
    resources = candidate.get('resource_checks', [])
    if not resources:
        errors.append(candidate['id'] + ' missing explicit resource service checks')
    for resource in resources:
        try:
            demand = expression(resource['demand_per_cycle'], names)
            supply = expression(resource['supply_per_cycle'], names)
            if demand < 0 or supply < 0 or (demand > 0 and supply == 0) or not resource['unit'] or not resource['basis']:
                raise ValueError('invalid resource evidence')
            if demand > supply:
                raise ValueError('declared demand exceeds service')
        except (KeyError, TypeError, ValueError, SyntaxError, ZeroDivisionError) as e:
            errors.append(candidate['id'] + ' resource service: ' + str(e))
    for field in candidate.get('abi_fields', []):
        try:
            width = expression(field['width'], names)
            offset = expression(field['offset'], names)
            carrier = expression(field['carrier_width'], names)
            if any(v != int(v) for v in (width,offset,carrier)) or width <= 0 or offset < 0 or offset+width > carrier:
                raise ValueError('field does not fit carrier')
        except (KeyError, TypeError, ValueError, SyntaxError) as e:
            errors.append(candidate['id'] + ' ABI field: ' + str(e))



def calculate(document,sources,require_document=True):
    """One declarative contract, no legacy derived-input compatibility."""
    sources=_quantities.original_sources(sources, strict=True)
    document=copy.deepcopy(document)
    prepared=prepare_requirements(document['requirements'],sources,declaration_mode=False);document['requirements']=prepared['requirements']
    errors=list(prepared['errors']);results={};facts,fact_errors=validate_requirements(document['requirements'],sources);errors.extend(fact_errors)
    constraints=document['requirements']['constraints'];candidates=document['candidates'];ids=[c['id'] for c in candidates]
    if not constraints:errors.append('no constraints extracted')
    if len({c['id'] for c in constraints})!=len(constraints):errors.append('duplicate constraint id')
    if not 2<=len(ids)<=4 or len(ids)!=len(set(ids)):errors.append('need 2-4 distinct candidates')
    if document.get('selected') not in ids:errors.append('selected candidate absent')
    density=_units.macro_density(document['requirements'],facts)
    work=_work.validate(document.get('work_model'),facts,sources,expression);errors.extend(work['errors'])
    revision_errors=[]
    if 'work_model_revision' in document:
        revision=document['work_model_revision']
        if not isinstance(revision,dict) or not revision.get('reason'):revision_errors.append('work_model_revision reason required')
        else:revision_errors.extend(_work.citation_errors(revision.get('references'),sources,'work_model_revision'))
    errors.extend(revision_errors)
    for c in candidates:
        cid=c['id'];names=dict(facts);definitions={};local=[]
        if 'area_totals' in c:local.append(cid+' area_totals is program-derived; omit field')
        for section in ('assumptions','calculations'):
            if not isinstance(c.get(section),list):local.append(cid+' '+section+' must be an array of {id,value OR formula,unit,basis} objects, not '+type(c.get(section)).__name__);continue
            for row in c[section]:
                try:
                    if not isinstance(row,dict):raise ValueError('row must be an object with id, one expression, unit and basis')
                    name=row['id'];fields=[k for k in ('value','formula') if k in row]
                    if not isinstance(name,str) or not name.isidentifier() or name in names or name=='macro_bits' or not row.get('unit') or not row.get('basis'):raise ValueError('identifier must be new, not reserved macro_bits; unit and basis required')
                    if len(fields)!=1:raise ValueError('exactly one of formula or value required; expressions use earlier identifiers')
                    names[name]=expression(row[fields[0]],names);definitions[name]=row[fields[0]]
                except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError) as e:local.append(cid+' '+section+'/'+str(row.get('id','?') if isinstance(row,dict) else '?')+': '+str(e))
        numeric=derive_numeric_probes(c.get('numeric_probes',[]),names)
        local.extend(cid+' numeric/'+error for error in numeric['errors'])
        for probe in numeric['probes']:
            for check in probe['assertions']:
                if not check['pass']:local.append(cid+' numeric/'+probe['id']+': declared assertion failed: '+str(check))
        abi=_helpers['abi_geometry'].derive_all(c.get('abi_geometry',[]),names,expression)
        names.update(abi['values'])
        bound,link_errors=_work.bind_candidate(c,work,names,expression);local.extend(link_errors)
        view=dict(bound,_requirements=document['requirements'],_expressions=definitions,_abi_geometry=abi)
        ledger,ledger_errors=check_ledgers(view,names,density);local.extend(ledger_errors)
        if numeric.get('coverage_status') not in ('checked','failed'):
            ledger['evidence_gaps'].append('numeric_probes/'+numeric.get('coverage_status','not_checked')+': no complete asserted concrete probe evidence; source-risk coverage and end-to-end accuracy remain independent obligations')
        workloads=check_workloads(view,names,facts,constraints,local,ledger);check_resources(c,names,local)
        checks=[];metrics=c.get('metrics',{})
        if not isinstance(metrics,dict):local.append(cid+' metrics must be an object');metrics={}
        for key in metrics:
            if key not in {x['id'] for x in constraints}:local.append(cid+' unknown metric '+str(key))
        for con in constraints:
            quantity=con.get('quantity');actual=None;metadata={}
            try:
                if quantity in AUTO_QUANTITIES:
                    if con['id'] in metrics:raise ValueError('program-derived metric must be omitted')
                    if quantity in ('area','capacity'):
                        parsed=_units.unit(con['unit']);expected={'area':1} if quantity=='area' else {'storage':1}
                        if parsed is None or parsed[0]!=expected:raise ValueError('unsupported constraint unit')
                        actual=(ledger['area_total']['upper_mm2'] if quantity=='area' else ledger['macro_inventory_bits'])/parsed[1]
                    else:
                        workload=workloads[con['workload']];actual=workload[quantity]
                        # Performance ledger has canonical cycles, cycles/item and items/s.
                        parsed=_units.unit(con['unit'])
                        expected={'task_cycles':{},'cycles_per_item':{'item':-1},'items_per_second':{'item':1,'time':-1}}[quantity]
                        if parsed is None or parsed[0]!=expected:raise ValueError('unsupported or wrong performance unit')
                        actual/=parsed[1]
                    metadata={'computed_by':'fixed_budget_rule','quantity':quantity}
                else:
                    metric=metrics[con['id']]
                    if isinstance(metric,dict):
                        if metric.get('status')!='unverified' or not metric.get('reason'):raise ValueError('invalid unverified metric')
                        checks.append({'constraint':con['id'],'kind':con['kind'],'verification':con.get('verification','estimate'),'status':'unverified','reason':metric['reason'],'estimate_pass':None});continue
                    actual=expression(metric,names)
                limit=facts[con['limit']];fact=next(f for f in document['requirements']['facts'] if f['id']==con['limit']);a=_units.unit(fact.get('unit',''));b=_units.unit(con['unit'])
                if a is not None and b is not None:
                    if a[0]!=b[0]:raise ValueError('constraint limit dimension mismatch')
                    limit*=a[1]/b[1]
                passed=actual<=limit if con['relation']=='<=' else actual>=limit if con['relation']=='>=' else math.isclose(actual,limit,rel_tol=1e-9)
                checks.append(dict(constraint=con['id'],kind=con['kind'],actual=actual,limit=limit,unit=con['unit'],relation=con['relation'],verification=con.get('verification','estimate'),estimate_pass=passed,**metadata))
            except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError) as e:
                local.append(cid+' metric/'+con['id']+': '+str(e));checks.append({'constraint':con['id'],'kind':con['kind'],'status':'calculation_failed','estimate_pass':None})
        results[cid]={'numeric_probes':numeric,'values':names,'macro_inventory_bits':ledger['macro_inventory_bits'],'ledger':ledger,'work_trace':bound.get('_work_trace',[]),'workloads':workloads,'checks':checks};errors.extend(local)
    if require_document and (not isinstance(document.get('architecture_md'),str) or len(document['architecture_md'])<=100):errors.append('missing substantive unified architecture_md')
    if any(k.endswith('_md') and k!='architecture_md' for k in document):errors.append('only architecture_md is a supported design document field')
    return {'ok':not errors,'errors':errors,'source_quantity_bindings':prepared['bindings'],'candidates':results,'storage_area_model':density or document['requirements']['storage_area_model'],'work_model':dict({k:v for k,v in work.items() if k!='items'},revision_ok=not revision_errors)}


def evaluate(document,sources,require_document=False):
    import hashlib,json
    fingerprint=hashlib.sha256(json.dumps(design_data(document),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    try:
        report=calculate(document,sources,require_document)
        selected=report['candidates'].get(document.get('selected'),{});checks=selected.get('checks',[])
        violations=[x['constraint'] for x in checks if x.get('kind')=='hard' and x.get('estimate_pass') is False]
        missing=[x['constraint'] for x in checks if x.get('kind')=='hard' and x.get('status')=='unverified' and x.get('verification')!='implementation']
        unsupported=selected.get('ledger',{}).get('unsupported_models',[])+report.get('work_model',{}).get('unknown',[])
        report['gate']={'status':'fail' if not report['ok'] or violations else 'unknown' if missing or unsupported else 'pass','hard_budget_violations':violations,'unverified_design_budgets':missing,'unsupported_models':unsupported}
    except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as e:report={'ok':False,'errors':[type(e).__name__+': '+str(e)],'candidates':{},'gate':{'status':'fail'}}
    report['design_sha256']=fingerprint
    report['scope']='program-derived budget totals and necessary service/liveness bounds; no proof of requirement completeness, scheduling, numerical accuracy or physical closure'
    report['unchecked']=['semantic correspondence of extracted constraints','omitted resources/work','full repeated/tail schedule, actual bank mapping and declared lifetime feasibility','unsupported traffic models','RTL protocols and numerical execution','library costs and complete physical timing']
    return report


def evaluate_work_model(model,requirements,sources):
    try:
        sources=_quantities.original_sources(sources, strict=True)
        facts,errors=validate_requirements(requirements,sources)
        report=_work.validate(model,facts,sources,expression)
        expected={c['workload'] for c in requirements.get('constraints',[]) if isinstance(c.get('workload'),str)}
        missing=expected-set(report['workloads'])
        if missing:errors.append('work-model profiles missing required performance ids: '+','.join(sorted(missing)))
        report['errors']=errors+report['errors'];report['ok']=not report['errors'];return report
    except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError) as e:return {'ok':False,'errors':[str(e)]}


def main():
    import argparse,json,sys
    parser=argparse.ArgumentParser(description='Calculate declared architecture budgets; JSON stdin/stdout, no project or model dependency.');parser.add_argument('--require-document',action='store_true');parser.add_argument('--work-model-only',action='store_true');parser.add_argument('--source-quantities-only',action='store_true');args=parser.parse_args();request=json.load(sys.stdin)
    result={'catalogue':source_quantities(request['sources']),'view':quantity_view(request['sources'])} if args.source_quantities_only else evaluate_work_model(request['work_model'],request['requirements'],request['sources']) if args.work_model_only else evaluate(request['document'],request['sources'],args.require_document)
    json.dump(result,sys.stdout,ensure_ascii=False,indent=2);sys.stdout.write('\n')

if __name__=='__main__':main()
