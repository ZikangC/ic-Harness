"""Portable review evidence checks; arithmetic is supplied by a trusted host."""
import argparse
import ast
import importlib.util
import json
import hashlib
import math
import difflib
import sys
import copy

TOPICS = {'requirements','cycle_accounting','resource_service','abi','numerics','area_basis'}
INPUTS = {'spec.md','reference.py'}
PROPOSAL = {'architecture.md','design.json'}


def is_original_source(name):
    """Original-input logical namespace; filenames never imply program authority."""
    if name in ('spec.md', 'reference.py'):
        return True
    if not isinstance(name, str) or not name.startswith('support/'):
        return False
    relative = name[len('support/'):]
    return (bool(relative) and not any(ord(char) < 32 or ord(char) == 127 for char in relative)
            and not any(char in relative for char in ('\\', ':'))
            and all(part not in ('', '.', '..') for part in relative.split('/')))


def original_sources(sources, *, strict=False):
    """Extract the entire declared input closure, rejecting malformed namespaces."""
    if not isinstance(sources, dict) or not {'spec.md', 'reference.py'} <= set(sources):
        raise ValueError('original sources require both spec.md and reference.py')
    selected = {}
    for name, value in sources.items():
        if not isinstance(name, str):
            raise ValueError('source names must be text')
        original = is_original_source(name)
        if not original and (strict or name == 'support' or name.startswith('support/')):
            raise ValueError('invalid original-source name: ' + repr(name))
        if original:
            if not isinstance(value, str):
                raise ValueError('original sources must map filenames to frozen text')
            selected[name] = value
    return selected

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def executed_source_values(result,sources,model,execution_dependencies):
    """Rebuild source views from actual context-bound successful transactions.

    Integrity is not semantic correctness. Failed declarations are preserved and
    complete coverage remains unavailable; no invalid value is promoted to fact.
    The caller separately authenticates the actual script execution sidecar.
    """
    values={};verified=[];errors=[];applicable=[]
    data={key:{} for key in ('fact_values','fact_units','workloads','workload_units','computations','states','flows')}
    selected={'values':values,'calculation_ids':[],'subreceipts':verified,'errors':errors,
              'applicability':applicable,'verified_data':data,'coverage_complete':False,
              'missing_output_declarations':[]}
    if not isinstance(result,dict) or not isinstance(result.get('subreceipts'),list) or not isinstance(model,dict):
        errors.append('source evidence requires actual typed transaction receipts and original declaration');return selected
    if not isinstance(sources,dict) or any(not isinstance(text,str) for text in sources.values()) or not isinstance(execution_dependencies,dict):
        errors.append('source texts and trusted dependency identities must be objects');return selected
    if any(not isinstance(result.get(key),dict) for key in data):
        errors.append('source typed parent views must be objects');return selected
    try:
        sources=original_sources(sources, strict=True)
    except (TypeError,ValueError) as error:
        errors.append(str(error));return selected
    expected={'source_sha256s':{key:hashlib.sha256(text.encode()).hexdigest() for key,text in sources.items()},'model_sha256':fingerprint(model)}
    if result.get('model_sha256')!=expected['model_sha256'] or not isinstance(result.get('values'),dict) or not isinstance(result.get('calculation_ids'),list):
        errors.append('source parent input/declaration/value identity mismatch');return selected
    ids=set();outputs=set()
    collections={'fact':'facts','workload':'workloads','computation':'computations','state':'states','flow':'flows'}
    for row in result['subreceipts']:
        label=row.get('id') if isinstance(row,dict) else None
        try:
            if not isinstance(label,str) or not label or label in ids:raise ValueError('unique source transaction id required')
            ids.add(label)
            if row.get('receipt_sha256')!=fingerprint({key:value for key,value in row.items() if key!='receipt_sha256'}):raise ValueError('source transaction content fingerprint mismatch')
            binding=row.get('binding',{})
            if not isinstance(binding,dict) or any(binding.get(key)!=value for key,value in expected.items()):raise ValueError('source transaction belongs to different original inputs/declaration')
            if binding!=result.get('binding'):raise ValueError('source transaction differs from parent binding')
            for kind in ('checker','calculator'):
                files=binding.get(kind+'_files_sha256');bundle=binding.get(kind+'_bundle_sha256')
                if not isinstance(files,dict) or not files or fingerprint(files)!=bundle or bundle!=(execution_dependencies or {}).get(kind+'_bundle_sha256'):
                    raise ValueError('source '+kind+' dependency fingerprint mismatch')
            status=row.get('status');info=row.get('applicability',{})
            if not isinstance(info,dict):raise ValueError('source applicability must be an object')
            if status=='declaration_failed':
                if row.get('values') or row.get('calculation_ids') or row.get('output') or not row.get('errors'):raise ValueError('failed source declaration cannot supply values/output')
                applicable.append(dict(info,status=status,errors=row['errors']));continue
            if status!='executed' or row.get('errors'):raise ValueError('successful source execution status required')
            output=row['output'];collection=output['collection'];key=output['key'];profile=output.get('workload');produced=output['data']
            if collection!=collections.get(row.get('section')) or not isinstance(key,str) or not isinstance(produced,dict):raise ValueError('source typed output collection/key/data mismatch')
            raw_rows=model.get(collection)
            if not isinstance(raw_rows,list):raise ValueError('source output belongs to malformed collection')
            matches=[raw for raw in raw_rows if isinstance(raw,dict) and raw.get('id')==key]
            if len(matches)!=1:raise ValueError('source output has ambiguous original declaration')
            declaration=matches[0] if profile is None else {'row':matches[0],'workload':profile}
            if row.get('declaration_sha256')!=fingerprint(declaration):raise ValueError('source output does not match actual declaration/profile')
            identity=(collection,key,profile)
            if identity in outputs:raise ValueError('duplicate source output credit')
            if profile is not None and profile not in data['workloads']:raise ValueError('source output uses unverified workload')
            dependencies=info.get('dependencies',[])
            if not isinstance(dependencies,list) or set(dependencies)-set(data['fact_values']):raise ValueError('source output depends on unverified facts')
            computed=row.get('values');calculated=row.get('calculation_ids')
            if not isinstance(computed,dict) or not isinstance(calculated,list) or len(calculated)!=len(set(calculated)) or set(computed)!=set(calculated):raise ValueError('source values must name unique executed calculations')
            if any(not isinstance(name,str) or name in values or type(value) not in (int,float) or not math.isfinite(value) for name,value in computed.items()):raise ValueError('duplicate/nonfinite source calculation')
            if any(result['values'].get(name)!=value or name not in result['calculation_ids'] for name,value in computed.items()):raise ValueError('source parent values differ from actual transaction')
            if collection=='facts':
                if result.get('fact_values',{}).get(key)!=produced['value'] or result.get('fact_units',{}).get(key)!=produced['unit']:raise ValueError('source fact view differs from transaction')
                data['fact_values'][key]=produced['value'];data['fact_units'][key]=produced['unit']
            elif collection=='workloads':
                if result.get('workloads',{}).get(key)!=produced['quantity'] or result.get('workload_units',{}).get(key)!=produced['unit']:raise ValueError('source workload view differs from transaction')
                data['workloads'][key]=produced['quantity'];data['workload_units'][key]=produced['unit']
            elif collection=='computations':
                parent=result.get(collection,{}).get(key,{})
                if parent.get('unit')!=produced['unit'] or any(parent.get(field,{}).get(profile)!=produced[part] for field,part in (('totals','total'),('per_occurrence','per_occurrence'),('steps','steps'),('scopes','scope'))):raise ValueError('source computation view differs from transaction')
                aggregate=data[collection].setdefault(key,{'totals':{},'per_occurrence':{},'steps':{},'scopes':{},'unit':produced['unit']})
                for field,part in (('totals','total'),('per_occurrence','per_occurrence'),('steps','steps'),('scopes','scope')):aggregate[field][profile]=copy.deepcopy(produced[part])
            elif collection=='states':
                parent=result.get(collection,{}).get(key,{})
                if parent.get('lifecycle')!=produced['lifecycle'] or parent.get('profiles',{}).get(profile)!={part:produced[part] for part in ('shape','elements')}:raise ValueError('source state view differs from transaction')
                aggregate=data[collection].setdefault(key,{'profiles':{},'lifecycle':produced['lifecycle']})
                aggregate['profiles'][profile]={part:copy.deepcopy(produced[part]) for part in ('shape','elements')}
            elif profile is None:
                if produced.get('encoding')!='abi_dependent' or result.get(collection,{}).get(key)!=produced:raise ValueError('source ABI-dependent view differs from transaction')
                data[collection][key]=copy.deepcopy(produced)
            else:
                parent=result.get(collection,{}).get(key,{})
                if any(parent.get(part)!=produced[part] for part in ('direction','encoding')) or any(parent.get(field,{}).get(profile)!=produced[part] for field,part in (('totals','total'),('per_occurrence_bits','per_occurrence_bits'),('scopes','scope'))):raise ValueError('source fixed flow view differs from transaction')
                aggregate=data[collection].setdefault(key,{'direction':produced['direction'],'encoding':produced['encoding'],'totals':{},'per_occurrence_bits':{},'scopes':{}})
                for field,part in (('totals','total'),('per_occurrence_bits','per_occurrence_bits'),('scopes','scope')):aggregate[field][profile]=copy.deepcopy(produced[part])
            outputs.add(identity);values.update(computed);verified.append(copy.deepcopy(row));applicable.append(dict(info,status=status,receipt_id=label))
        except (KeyError,TypeError,ValueError,AttributeError,OverflowError) as error:errors.append('source transaction/'+str(label)+': '+str(error))
    selected['calculation_ids']=list(values)
    try:
        if set(result['values'])!=set(values) or set(result['calculation_ids'])!=set(values):errors.append('source parent contains unexecuted/unbound values')
        if any(result.get(key,{})!=value for key,value in data.items()):errors.append('source parent typed view contains unexecuted/unbound outputs')
    except TypeError:errors.append('source parent calculation IDs must be strings')
    expected_outputs=set();declarations_valid=True
    declared={}
    for collection in collections.values():
        rows=model.get(collection)
        if not isinstance(rows,list):declarations_valid=False;continue
        keys=[row.get('id') for row in rows if isinstance(row,dict)]
        if len(keys)!=len(rows) or any(not isinstance(key,str) or not key for key in keys) or len(set(key for key in keys if isinstance(key,str)))!=len(keys):
            declarations_valid=False;continue
        declared[collection]=keys
    for collection in ('facts','workloads'):
        expected_outputs.update((collection,key,None) for key in declared.get(collection,[]))
    for collection in ('computations','states'):
        expected_outputs.update((collection,key,profile) for key in declared.get(collection,[]) for profile in declared.get('workloads',[]))
    for row in model.get('flows',[]) if isinstance(model.get('flows'),list) else []:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str):declarations_valid=False;continue
        if row.get('encoding')=='abi_dependent':expected_outputs.add(('flows',row['id'],None))
        elif row.get('encoding')=='fixed_external':expected_outputs.update(('flows',row['id'],profile) for profile in declared.get('workloads',[]))
        else:declarations_valid=False
    selected['missing_output_declarations']=[{'collection':collection,'id':key,'workload':profile}
        for collection,key,profile in sorted(expected_outputs-outputs,key=lambda value:tuple(str(part) for part in value))]
    complete=declarations_valid and bool(declared.get('workloads')) and not selected['missing_output_declarations']
    if result.get('ok') is True and result.get('coverage_complete') is True and not complete:
        errors.append('source complete coverage claim omits original declarations/profiles')
    selected['coverage_complete']=complete and result.get('ok') is True and result.get('coverage_complete') is True and not result.get('missing_declarations') and not errors and all(isinstance(row,dict) and row.get('status')=='executed' for row in result['subreceipts'])
    return selected


def _expression_names(value):
    """Identifiers only; never execute a submitted expression."""
    if isinstance(value,str):
        try:return {node.id for node in ast.walk(ast.parse(value,mode='eval')) if isinstance(node,ast.Name)}
        except (SyntaxError,ValueError):return set()
    if isinstance(value,list):return set().union(*(_expression_names(item) for item in value)) if value else set()
    if isinstance(value,dict):return set().union(*(_expression_names(item) for item in value.values())) if value else set()
    return set()


def _comparison_evidence(request,sources,numeric,values):
    """Bind comparison identity/lineage without deciding its chip relevance."""
    nodes={}; targets={}
    for section in ('facts','assumptions','calculations'):
        for row in request.get(section,[]):
            if not isinstance(row,dict) or row.get('id') not in values:continue
            name=row['id']
            declaration={key:copy.deepcopy(value) for key,value in row.items() if key not in ('expected','basis','citations','references')}
            dependencies=_expression_names(row.get('formula'))
            if isinstance(row.get('convert'),dict):dependencies.add(row['convert'].get('fact'))
            nodes[name]={'declaration':declaration,'dependencies':dependencies-{name,None}}
            if 'expected' in row:
                targets[name]={'kind':'recompute_comparison','target':{'id':name,'unit':row.get('unit')},
                               'measurement_id':name,'roots':{name}|_expression_names(row['expected'])}
    declared={row['id']:row for row in request.get('numeric_probes',[]) if isinstance(row,dict) and isinstance(row.get('id'),str)}
    for probe in numeric.get('probes',[]):
        row=declared[probe['id']]
        declaration={key:copy.deepcopy(value) for key,value in row.items() if key not in ('assertions','basis','references')}
        dependencies=_expression_names([row.get('input'),row.get('format'),row.get('scale')])
        outputs={probe['id']+'__'+metric for metric in probe.get('results',{}) if probe['id']+'__'+metric in values}
        for name in outputs:nodes[name]={'declaration':declaration,'dependencies':dependencies-outputs}
        for index,assertion in enumerate(row.get('assertions',[])):
            key=probe['id']+'__assertion_'+str(index)
            targets[key]={'kind':'numeric_assertion','target':{'id':probe['id'],'operation':row['operation'],
                           'metric':assertion['metric'],'input':copy.deepcopy(row['input'])},
                          'measurement_id':probe['id']+'__'+assertion['metric'],
                          'roots':outputs|_expression_names(assertion['expected'])}
    details={}
    for key,target in targets.items():
        pending=list(target.pop('roots')); lineage={};selectors=[]
        while pending:
            name=pending.pop()
            if name in lineage or name not in nodes:continue
            node=nodes[name];lineage[name]=node['declaration'];pending.extend(node['dependencies'])
            selector=node['declaration'].get('source')
            if isinstance(selector,dict) and selector not in selectors:selectors.append(copy.deepcopy(selector))
        # A related guard proof must reproduce the original measurement and
        # its value dependencies, not merely choose another point that passes.
        lineage_hashes={}
        for root in lineage:
            needed=[root];closure={}
            while needed:
                name=needed.pop()
                if name in closure or name not in nodes:continue
                closure[name]={'declaration':nodes[name]['declaration'],'value':values.get(name)}
                needed.extend(nodes[name]['dependencies'])
            lineage_hashes[root]=fingerprint(closure)
        measurement=target.pop('measurement_id')
        details[key]=dict(target,declaration_sha256=fingerprint(lineage),source_selectors=sorted(selectors,key=fingerprint),
                          measurement={'id':measurement,'sha256':lineage_hashes.get(measurement)},lineage_sha256s=lineage_hashes)
    return {'source_sha256s':{name:hashlib.sha256(text.encode()).hexdigest() for name,text in sources.items()},'comparisons':details}


def _recompute_checks(result,sources):
    """Program-owned view of ordinary comparisons, including successful fixes."""
    comparisons=result.get('comparisons',{})
    if not isinstance(comparisons,dict):raise ValueError('recompute comparisons must be an object')
    if not comparisons:return []
    evidence=result.get('comparison_evidence',{})
    if not isinstance(evidence,dict):raise ValueError('recompute comparison evidence must be an object')
    expected={name:hashlib.sha256(text.encode()).hexdigest() for name,text in sources.items()}
    if evidence.get('source_sha256s')!=expected:raise ValueError('recompute comparison belongs to a different frozen source/proposal packet')
    details=evidence.get('comparisons',{})
    if not isinstance(details,dict) or set(details)!=set(comparisons):raise ValueError('recompute comparison evidence does not match executed comparisons')
    checks=[]
    for key,row in comparisons.items():
        metadata=details[key]
        if (not isinstance(key,str) or not isinstance(row,dict) or type(row.get('matches_expected')) is not bool
                or not isinstance(metadata,dict) or metadata.get('kind') not in ('recompute_comparison','numeric_assertion')
                or not isinstance(metadata.get('target'),dict) or not isinstance(metadata.get('declaration_sha256'),str)
                or not isinstance(metadata.get('source_selectors'),list)):
            raise ValueError('invalid executed recompute comparison metadata')
        checks.append(dict(copy.deepcopy(row),id=key,topic='independent_recompute',status='pass' if row['matches_expected'] else 'fail',
                           relation=row.get('relation','=='),**copy.deepcopy(metadata)))
    return checks


def executed_values(result,sources,source_analysis,document,budget,execution_dependencies):
    """Select actual calculations from context-bound successful transactions.

    The host supplies this context and the script outputs. Hashes check identity
    and internal consistency; they are not signatures or a semantic proof.
    A bad independent transaction cannot erase a separate successful witness.
    Neither partial evidence nor this selector establishes release coverage.
    """
    values={};checks=[];verified=[];errors=[]
    selected={'values':values,'calculation_ids':[],'checks':checks,'subreceipts':verified,'errors':errors}
    if not isinstance(result,dict):errors.append('calculation result must be an object');return selected
    if not isinstance(result.get('values',{}),dict) or not isinstance(result.get('calculation_ids',[]),list):
        errors.append('calculation values and IDs must be an object and array');return selected
    if 'subreceipts' not in result:
        if 'work_result' in result:
            errors.append('proposal evidence requires executed transaction receipts');return selected
        if result.get('ok') is True:
            for key in result.get('calculation_ids',[]):
                value=result.get('values',{}).get(key)
                if isinstance(key,str) and type(value) in (int,float) and math.isfinite(value):values[key]=value
                else:errors.append('invalid executed calculation '+str(key))
            selected['calculation_ids']=list(values)
            try:
                comparison_checks=_recompute_checks(result,sources)
                if comparison_checks:
                    checks.extend(comparison_checks)
                    verified.append({'id':'recompute','status':'executed','checks':comparison_checks,
                                     'scope':'program view of actual recompute comparisons; no source-semantic or relevance proof'})
            except (KeyError,TypeError,ValueError) as error:
                selected['comparison_evidence_errors']=['recompute comparison: '+str(error)]
                errors.extend(selected['comparison_evidence_errors'])
        return selected
    rows=result['subreceipts']
    if not isinstance(rows,list):errors.append('subreceipts must be an array');return selected
    expected={'source_analysis_sha256':fingerprint(source_analysis),
              'source_sha256s':{key:hashlib.sha256(text.encode()).hexdigest() for key,text in sources.items()},
              'design_sha256':fingerprint({key:value for key,value in document.items() if key not in ('architecture_md','tool_receipt')}),
              'budget_sha256':fingerprint(budget)}
    parent=result.get('binding',{})
    if not isinstance(parent,dict):errors.append('transaction parent binding must be an object');return selected
    ids=set()
    for row in rows:
        label=row.get('id') if isinstance(row,dict) else None
        try:
            if not isinstance(label,str) or not label or label in ids:raise ValueError('unique transaction id required')
            ids.add(label)
            if row.get('status')=='declaration_failed':continue
            if row.get('status')!='executed' or row.get('errors'):raise ValueError('successful execution status required')
            if row.get('receipt_sha256')!=fingerprint({k:v for k,v in row.items() if k!='receipt_sha256'}):raise ValueError('transaction content fingerprint mismatch')
            binding=row.get('binding',{})
            if not isinstance(binding,dict) or any(binding.get(key)!=value for key,value in expected.items()):raise ValueError('transaction belongs to a different frozen input/proposal/baseline/budget')
            if binding.get('model_sha256')!=parent.get('model_sha256') or result.get('model_sha256')!=parent.get('model_sha256'):raise ValueError('transaction belongs to a different audit declaration')
            for kind in ('checker','calculator'):
                files=binding.get(kind+'_files_sha256');bundle=binding.get(kind+'_bundle_sha256')
                if not isinstance(files,dict) or not files or fingerprint(files)!=bundle or bundle!=(execution_dependencies or {}).get(kind+'_bundle_sha256'):
                    raise ValueError('transaction '+kind+' script dependency fingerprint mismatch')
            computed=row.get('values');calculated=row.get('calculation_ids')
            if not isinstance(computed,dict) or not isinstance(calculated,list) or len(calculated)!=len(set(calculated)) or set(calculated)!=set(computed):raise ValueError('transaction values must name unique executed calculations only')
            if any(not isinstance(key,str) or key in values or type(value) not in (int,float) or not math.isfinite(value) for key,value in computed.items()):raise ValueError('duplicate/nonfinite executed calculation')
            if any(result.get('values',{}).get(key)!=value or key not in result.get('calculation_ids',[]) for key,value in computed.items()):raise ValueError('parent calculation view differs from executed transaction')
            evidence=row.get('checks',[])
            if not isinstance(evidence,list):raise ValueError('transaction checks must be an array')
            values.update(computed);checks.extend(copy.deepcopy(evidence));verified.append(copy.deepcopy(row))
        except (KeyError,TypeError,ValueError) as error:errors.append('transaction/'+str(label)+': '+str(error))
    selected['calculation_ids']=list(values)
    try:
        if set(result.get('calculation_ids',[]))!=set(values) or set(result.get('values',{}))!=set(values):
            errors.append('parent calculation view contains unexecuted or unbound values')
    except TypeError:errors.append('parent calculation IDs must be strings')
    return selected

def source_window(text,start,end):
    lines=text.splitlines(keepends=True)
    if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines):raise ValueError('source span needs integer bounds within the frozen file; requested='+repr((start,end))+'; valid=1..'+str(len(lines)))
    return ''.join(lines[start-1:end]).rstrip('\r\n')


def resolve_citation(reference,sources,citations,input_only=False):
    """Resolve the model's exact span or a host-issued ID; never choose evidence."""
    if not isinstance(reference,dict):return reference
    if input_only and 'file' in reference and not is_original_source(reference['file']):
        raise ValueError('source reference is outside its frozen original-input scope')
    if 'evidence_id' not in reference:
        if 'file' not in reference or 'quote' in reference:return reference
        if set(reference)!={'file','start','end'}:raise ValueError('source span requires only file, start and end; no quote override')
        file=reference['file']
        if not isinstance(file,str) or file not in sources or (input_only and not is_original_source(file)):raise ValueError('source span is outside its frozen source scope')
        quote=source_window(sources[file],reference['start'],reference['end'])
        if not quote:raise ValueError('source span has no nonempty excerpt')
        row=dict(reference,quote=quote,source_sha256=hashlib.sha256(sources[file].encode()).hexdigest())
        row['evidence_id']=hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        return row
    if set(reference)!={'evidence_id'}:raise ValueError('evidence_id reference cannot override its source location')
    matches=[row for row in citations if isinstance(row,dict) and row.get('evidence_id')==reference['evidence_id']]
    if len(matches)!=1:raise ValueError('unknown/ambiguous frozen source evidence_id')
    row=matches[0];file=row.get('file')
    if file not in sources or (input_only and not is_original_source(file)):raise ValueError('evidence_id is outside the original-input scope')
    identity={key:row[key] for key in ('file','source_sha256','start','end','quote')}
    source_hash=hashlib.sha256(sources[file].encode()).hexdigest()
    expected=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if identity['source_sha256']!=source_hash or expected!=row['evidence_id']:raise ValueError('evidence_id belongs to a different source version or location')
    a,b=identity['start'],identity['end'];quote=identity['quote']
    if not isinstance(quote,str) or not quote or quote not in sources[file] or quote not in source_window(sources[file],a,b):raise ValueError('evidence_id does not locate its exact frozen excerpt')
    return row


def citation_binding(reference,resolved,location):
    return {'location':location,'reference':copy.deepcopy(reference),'resolved':copy.deepcopy(resolved),
            'scope':'model-selected frozen source location; excerpt and content fingerprint only, no semantic selection or report edits'}

def baseline_citation_diagnostic(obligation_id,reference_index,reference,sources,error):
    """Bounded original-source locators, never quote repair or semantic evidence."""
    ref=reference if isinstance(reference,dict) else {}
    file=ref.get('file');quote=ref.get('quote')
    result={'obligation_id':obligation_id,'reference_index':reference_index,
            'file':file if isinstance(file,str) else None,'error':str(error),
            'kind':'invalid_reference',
            'scope':'original-input source locator only; verify meaning and choose evidence yourself; no automatic quote or bounds changes'}
    # Baseline diagnostics stay blind to proposal/program sources even if callers
    # accidentally provide a larger packet.
    if not isinstance(file,str) or not is_original_source(file) or not isinstance(sources.get(file),str):
        return result
    text=sources[file];lines=text.splitlines()
    result.update(line_count=len(lines),requested_bounds={'start':ref.get('start'),'end':ref.get('end')},
                  available_bounds={'minimum':1,'maximum':len(lines)})
    if not isinstance(quote,str) or not quote:
        result['kind']='invalid_span'
        a=ref.get('start');anchor=a if type(a) is int and 1<=a<=len(lines) else len(lines)
        result['context']=[{'line':i+1,'text':lines[i]} for i in range(max(0,anchor-3),min(len(lines),anchor+4))]
        return result
    matches=text.count(quote)
    if matches:
        result.update(kind='ambiguous_excerpt',match_count=matches,matches=[],matches_truncated=matches>6)
        offset=0
        for _ in range(min(matches,6)):
            position=text.find(quote,offset);start=text[:position].count('\n')+1
            result['matches'].append({'file':file,'start':start,'end':start+quote.count('\n'),'quote':quote})
            offset=position+len(quote)
    else:
        # Full source lines retain punctuation/spacing. Similarity locates text;
        # it does not decide whether a nearby line supports the obligation.
        candidates=[]
        for index,line in enumerate(text.splitlines(),1):
            score=difflib.SequenceMatcher(None,quote,line).ratio()
            if score>=0.35:candidates.append((score,index,line))
        candidates.sort(key=lambda row:(-row[0],row[1]))
        result.update(kind='inexact_excerpt',suggestions=[{'file':file,'start':index,'end':index,'quote':line} for _,index,line in candidates[:3]])
    return result


def validate_baseline(baseline,sources,citations=(),*,source_analysis=None,execution_dependencies=None):
    errors=[];ids=set();files=set();diagnostics=[];bindings=[]
    try:
        sources=original_sources(sources)
    except (TypeError,ValueError) as error:
        # Keep the bundle failure while locating bad references using only real,
        # safe, text sources that remain available. Missing mains are never
        # invented, and this diagnostic subset cannot make the baseline pass.
        errors.append(str(error))
        sources={name:text for name,text in sources.items()
                 if is_original_source(name) and isinstance(text,str)} if isinstance(sources,dict) else {}
    if not isinstance(baseline,dict):return {'ok':False,'errors':['baseline must be an object'],'citation_diagnostics':diagnostics,'citation_bindings':bindings,'scope':'source evidence/structure only'}
    for key in ('scope','limits'):
        if not isinstance(baseline.get(key),str) or not baseline[key].strip():errors.append('baseline '+key+' required')
    if not isinstance(baseline.get('ambiguities'),list):errors.append('baseline ambiguities must be a list')
    if 'source_revision' in baseline:
        revision=baseline['source_revision']
        try:
            if not isinstance(revision,dict) or not isinstance(revision.get('reason'),str) or not revision['reason'].strip():raise ValueError('explicit source revision reason required')
            refs=revision.get('references')
            if not isinstance(refs,list) or not refs:raise ValueError('source revision original-input references required')
            for ref in refs:
                original=ref;ref=resolve_citation(ref,sources,citations,input_only=True)
                if ref is not original:bindings.append(citation_binding(original,ref,'source_revision'))
                file=ref['file'];quote=ref['quote']
                if not is_original_source(file) or not isinstance(quote,str) or not quote or quote not in sources[file]:raise ValueError('source revision must cite exact original-input text')
                if sources[file].count(quote)!=1:
                    lines=sources[file].splitlines();a=ref['start'];b=ref.get('end',a)
                    if type(a) is not int or type(b) is not int or not 1<=a<=b<=len(lines) or quote not in source_window(sources[file],a,b):raise ValueError('source revision ambiguous excerpt needs line bounds')
        except (KeyError,TypeError,ValueError) as e:errors.append('baseline source_revision: '+str(e))
    rows=baseline.get('obligations')
    if not isinstance(rows,list) or not rows:errors.append('baseline obligations must be a nonempty array');rows=[]
    for row in rows:
        try:
            rid=row['id']
            if not isinstance(rid,str) or not rid or rid in ids:raise ValueError('unique obligation id required')
            ids.add(rid)
            if row.get('category') not in ('computation','state','interface','numerical','budget','completion'):raise ValueError('invalid category')
            if not isinstance(row.get('requirement'),str) or not row['requirement'].strip():raise ValueError('requirement required')
            refs=row['references']
            if not isinstance(refs,list) or not refs:raise ValueError('original source references required')
            for reference_index,ref in enumerate(refs):
                try:
                    original=ref;ref=resolve_citation(ref,sources,citations,input_only=True)
                    if ref is not original:bindings.append(citation_binding(original,ref,'obligation/'+rid+'/references/'+str(reference_index)))
                    file=ref['file'];quote=ref['quote']
                    if not is_original_source(file) or file not in sources or not isinstance(quote,str) or not quote or quote not in sources[file]:raise ValueError('exact original-input excerpt required')
                    if sources[file].count(quote)!=1:
                        lines=sources[file].splitlines();a=ref['start'];b=ref.get('end',a)
                        if type(a) is not int or type(b) is not int or not 1<=a<=b<=len(lines) or quote not in source_window(sources[file],a,b):raise ValueError('ambiguous excerpt needs line bounds')
                    files.add(file)
                except (KeyError,TypeError,ValueError) as e:
                    diagnostics.append(baseline_citation_diagnostic(rid,reference_index,ref,sources,e))
                    raise
        except (KeyError,TypeError,ValueError) as e:errors.append('baseline obligation/'+str(row.get('id','?') if isinstance(row,dict) else '?')+': '+str(e))
    source_errors=[]
    if source_analysis is not None:
        selected=executed_source_values(source_analysis,sources,baseline.get('analysis'),execution_dependencies)
        source_errors.extend(selected['errors'])
        for receipt in selected['subreceipts'] if not source_errors else []:
            for binding in receipt.get('citation_bindings',[]):
                refs=([binding['resolved']] if isinstance(binding,dict) and 'resolved' in binding else
                      binding.get('materialized_fact',{}).get('citations',[]) if isinstance(binding,dict) else [])
                for ref in refs:
                    try:
                        # These are already program-resolved receipt excerpts,
                        # not model references with optional location overrides.
                        file=ref['file'];quote=ref['quote']
                        if not is_original_source(file) or not isinstance(quote,str) or not quote or quote not in sources[file]:
                            raise ValueError('successful source transaction needs exact original evidence')
                        if 'source_sha256' in ref and ref['source_sha256']!=hashlib.sha256(sources[file].encode()).hexdigest():
                            raise ValueError('successful source transaction evidence belongs to a stale original')
                        if sources[file].count(quote)!=1 and quote not in source_window(sources[file],ref.get('start'),ref.get('end',ref.get('start'))):
                            raise ValueError('successful source transaction has incorrect original evidence bounds')
                        files.add(file)
                    except (KeyError,TypeError,ValueError) as error:source_errors.append(str(error))
        errors.extend('baseline source evidence: '+error for error in source_errors)
    if not INPUTS<=files:errors.append('baseline must inspect both spec.md and reference.py')
    return {'ok':not errors,'errors':errors,'citation_diagnostics':diagnostics,'citation_bindings':bindings,
            'input_evidence_files':sorted(files),'source_evidence_errors':source_errors,
            'scope':'valid obligation references plus actual verified successful source transaction references; source identity/structure only, not complete source coverage or engineering judgment'}


def basic_errors(review):
    errors=[]
    if not isinstance(review,dict) or review.get('verdict') not in ('accept','revise','insufficient_evidence'):
        return ['invalid review verdict']
    for key in ('findings','unverified','checks'):
        if not isinstance(review.get(key),list):errors.append(key+' must be a list')
    if errors:return errors
    checks=review['checks']
    if len(checks)!=len(TOPICS) or {x.get('topic') for x in checks if isinstance(x,dict) and isinstance(x.get('topic'),str)}!=TOPICS:
        errors.append('review coverage must contain each required topic once')
    for x in checks:
        if not isinstance(x,dict):errors.append('check must be an object');continue
        label='check/'+str(x.get('topic','?'))
        if x.get('status') not in ('pass','conditional','fail'):errors.append(label+'.status must be pass|conditional|fail')
        if not isinstance(x.get('evidence'),str) or not x['evidence'].strip():errors.append(label+'.evidence must be a nonempty reasoning STRING; references belong in the separate references array')
    ids=set()
    for f in review['findings']:
        if not isinstance(f,dict):errors.append('finding is not an object');continue
        if f.get('severity') not in ('blocking','condition','suggestion') or f.get('disposition') not in ('open','resolved'):
            errors.append('invalid finding severity/disposition')
        for key in ('id','location','issue','evidence','required_change'):
            if not isinstance(f.get(key),str) or not f[key].strip():errors.append('missing finding field '+key)
        if not isinstance(f.get('id'),str):continue
        if f['id'] in ids:errors.append('duplicate finding id')
        ids.add(f['id'])
    design_blocks=[f for f in review['findings'] if isinstance(f,dict) and f.get('category')=='design' and f.get('severity')=='blocking' and f.get('disposition')=='open']
    if review['verdict']=='revise' and not design_blocks:
        errors.append('revise requires an open blocking finding with a necessary change; conditions/suggestions do not justify design revision, and missing audit evidence belongs to insufficient_evidence')
        for row in review['unverified']:
            if isinstance(row,dict) and row.get('kind')=='design_decision':
                errors.append('unverified design decision requires revise and an open blocking design finding: '+str(row.get('topic')))
    if design_blocks and review['verdict']!='revise':
        errors.append('open blocking design finding requires revise; missing audit evidence cannot defer an established design defect')
    if review['verdict']=='accept':
        if any(isinstance(f,dict) and f.get('severity')=='blocking' and f.get('disposition')=='open' for f in review['findings']):errors.append('accept contradicts open blocking finding')
        if any(isinstance(x,dict) and x.get('status')=='fail' for x in checks):errors.append('accept contradicts failed check')
    return errors


def validate(review,context):
    errors=basic_errors(review);bindings=[];reference_diagnostics=[]
    sources=context['sources']
    status_only=bool(errors) and all(error.startswith('check/') and error.endswith('.status must be pass|conditional|fail') for error in errors)
    evidence_only=len(errors)==1 and errors[0] in {
        'check/'+topic+'.evidence must be a nonempty reasoning STRING; references belong in the separate references array' for topic in TOPICS}
    # Keep the error and inspect the unchanged report/context. A compact local
    # repair is eligible only if no hidden reference or judgment error remains.
    if errors and not (status_only or evidence_only):return {'ok':False,'errors':errors,'bindings':bindings,'reference_diagnostics':reference_diagnostics}
    def need(test,message):
        if not test:errors.append(message)
    def nonempty(value):return isinstance(value,str) and bool(value.strip())
    need(review.get('packet_sha256')==context['packet_sha256'],'review packet identity mismatch')
    need(nonempty(review.get('scope')),'scope must describe evidence and limits')
    record=context.get('baseline') or {};baseline=record.get('baseline',{})
    expected_hash=hashlib.sha256(json.dumps(baseline,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    need(bool(record) and record.get('baseline_sha256')==expected_hash,'validated source-only baseline required')
    need(review.get('baseline_sha256')==record.get('baseline_sha256'),'review baseline identity mismatch')
    if record:errors.extend(validate_baseline(baseline,original_sources(sources),context.get('citations',[]),
        source_analysis=context.get('source_analysis'),execution_dependencies=context.get('execution_dependencies'))['errors'])
    source_analysis=context.get('source_analysis')
    need(isinstance(source_analysis,dict),'executed independent source analysis record required')
    if review['verdict']=='accept':need(isinstance(source_analysis,dict) and source_analysis.get('ok') is True,'accept requires successful independent source analysis')
    if isinstance(source_analysis,dict):
        need(source_analysis.get('model_sha256')==hashlib.sha256(json.dumps(baseline.get('analysis'),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'source analysis is not bound to this baseline interpretation')
    baseline_ids={x['id'] for x in baseline.get('obligations',[]) if isinstance(x,dict) and isinstance(x.get('id'),str)}
    mapped_baseline=set()
    results={r['request_id']:r for r in context.get('calculations',[]) if isinstance(r,dict) and 'request_id' in r}
    executed={key:executed_values(result,sources,source_analysis,context['document'],context['budget'],context.get('execution_dependencies')) for key,result in results.items()}
    for available in executed.values():errors.extend(available.get('comparison_evidence_errors',[]))
    cited_calculations=set()
    def check_reference(ref,label):
        key=ref.get('request_id');receipt_id=ref.get('receipt_id');check_id=ref.get('check_id')
        if set(ref)!={'request_id','receipt_id','check_id'} or not all(nonempty(value) for value in (key,receipt_id,check_id)):
            errors.append(label+' actual check reference requires only request_id/receipt_id/check_id');return False
        target=executed.get(key,{})
        found=any(receipt['id']==receipt_id and any(check['id']==check_id for check in receipt['checks']) for receipt in target.get('subreceipts',[]))
        need(found,label+' actual check reference is not binding-verified execution');return found
    def calculation(ref,label):
        try:
            report=results[ref['request_id']]
            available=executed[ref['request_id']]
            if ref['calculation_id'] not in available['calculation_ids']:
                raise ValueError('reference must name an executed bound calculation, not a failed declaration, fact or assumption'+('; '+'; '.join(available['errors']) if available['errors'] else ''))
            actual=available['values'][ref['calculation_id']]
            value=ref['actual']
            if type(value) not in (int,float) or not math.isfinite(value) or not math.isclose(actual,value,rel_tol=1e-9,abs_tol=1e-9):raise ValueError('reported value differs from executed calculation')
            cited_calculations.add((ref['request_id'],ref['calculation_id']))
            return True
        except (KeyError,TypeError,ValueError) as e:
            key=ref.get('request_id') if isinstance(ref,dict) else None
            available=executed.get(key,{}) if isinstance(key,str) else {}
            badref={field:ref.get(field) for field in ('request_id','calculation_id')} if isinstance(ref,dict) else ref
            errors.append(label+' calculation reference: '+str(e)+'; reference='+json.dumps(badref,ensure_ascii=False)+
                          '; bound_calculation_ids='+json.dumps(available.get('calculation_ids',[]),ensure_ascii=False));return False
    def references(refs,label):
        roles=set()
        if not isinstance(refs,list) or not refs:errors.append(label+' needs evidence references');return roles
        for ref in refs:
            bound_by_host=False
            if isinstance(ref,dict) and ('evidence_id' in ref or ('file' in ref and 'quote' not in ref)):
                try:
                    original=ref;ref=resolve_citation(ref,sources,context.get('citations',[]))
                    bindings.append(citation_binding(original,ref,label))
                    bound_by_host=True
                except (KeyError,TypeError,ValueError) as e:errors.append(label+' source evidence: '+str(e));continue
            if not isinstance(ref,dict):errors.append(label+' invalid reference');continue
            if 'request_id' in ref:
                if (check_reference(ref,label) if 'check_id' in ref else calculation(ref,label)):roles.add('calculation')
                continue
            file=ref.get('file');quote=ref.get('quote')
            if not isinstance(file,str) or file not in sources or not nonempty(quote):errors.append(label+' reference file/quote missing');continue
            text=sources[file];start,end=ref.get('start'),ref.get('end',ref.get('start'))
            if quote not in text:
                hints=difflib.get_close_matches(quote,text.splitlines(),n=1,cutoff=0.35)
                errors.append(label+' quotation is not exact source text: '+file+'; do not paraphrase/change case or insert ellipsis'+('; possible original line (recheck meaning before citing): '+hints[0][:800] if hints else ''));continue
            if text.count(quote)==1 and not bound_by_host:
                original_start,original_end=start,end
                pos=text.index(quote);start=text[:pos].count('\n')+1;end=start+quote.count('\n')
                if (original_start,original_end)!=(start,end):bindings.append({'location':label,'file':file,'quote':quote,'original_start':original_start,'original_end':original_end,'start':start,'end':end,'scope':'unique exact excerpt relocation only; report values/text unchanged'})
            lines=text.splitlines()
            if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines) or quote not in source_window(text,start,end):
                errors.append(label+' ambiguous quotation/incorrect line bounds: '+file+'; choose a longer unique exact excerpt or correct start/end');continue
            roles.add('input' if is_original_source(file) else 'proposal' if file in PROPOSAL else 'program')
            if file=='budget.json':
                proposal={k:v for k,v in context['document'].items() if k not in ('architecture_md','tool_receipt')}
                fingerprint=hashlib.sha256(json.dumps(proposal,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if context['budget'].get('design_sha256')==fingerprint:roles.add('proposal')
        return roles
    for check in review['checks']:
        roles=references(check.get('references'),check['topic'])
        need({'input','proposal'}<=roles,check['topic']+' requires original-input and proposal evidence')
        reference_diagnostics.append({'collection':'checks','topic':check['topic'],'required_roles':['input','proposal'],'actual_roles':sorted(roles),'missing_roles':sorted({'input','proposal'}-roles),'reference_count':len(check.get('references',[])) if isinstance(check.get('references'),list) else 0})
    rows=review.get('requirements');ids=set()
    if not isinstance(rows,list):errors.append('requirements mapping must be a list');rows=[]
    constraints={c['id']:c for c in context['document'].get('requirements',{}).get('constraints',[]) if isinstance(c,dict) and isinstance(c.get('id'),str)}
    for row in rows:
        if not isinstance(row,dict) or not nonempty(row.get('id')):errors.append('invalid requirement row');continue
        linked=row.get('baseline_ids',[])
        need(isinstance(linked,list) and all(isinstance(x,str) for x in linked),'baseline_ids must be an id array')
        if isinstance(linked,list):
            need(set(x for x in linked if isinstance(x,str))<=baseline_ids,'unknown baseline obligation mapping')
            mapped_baseline.update(x for x in linked if isinstance(x,str))
        rid=row['id'];need(rid not in ids,'duplicate requirement mapping '+rid);ids.add(rid)
        need(nonempty(row.get('requirement')) and nonempty(row.get('explanation')),'requirement explanation missing: '+rid)
        need(row.get('status') in ('covered','missing','contradicted','deferred'),'invalid requirement status: '+rid)
        roles=references(row.get('references'),'requirement/'+rid)
        need('input' in roles,'requirement needs original-input evidence: '+rid+'; internal design defects belong in findings, not new input requirements')
        if row.get('status') in ('covered','deferred'):need('proposal' in roles,'covered/deferred requirement needs proposal evidence: '+rid)
        required={'input','proposal'} if row.get('status') in ('covered','deferred') else {'input'}
        reference_diagnostics.append({'collection':'requirements','id':rid,'required_roles':sorted(required),'actual_roles':sorted(roles),'missing_roles':sorted(required-roles),'reference_count':len(row.get('references',[])) if isinstance(row.get('references'),list) else 0})
        if row.get('status')=='deferred' and rid in constraints and constraints[rid].get('kind')=='hard' and constraints[rid].get('verification','estimate')!='implementation':errors.append('calculable hard budget cannot be deferred: '+rid)
        if review['verdict']=='accept' and row.get('status') in ('missing','contradicted'):errors.append('accept contradicts requirement '+rid)
    missing_baseline=sorted(baseline_ids-mapped_baseline);missing_constraints=sorted(constraints.keys()-ids)
    if review['verdict']=='accept':
        need(not missing_baseline,'missing independent baseline coverage: '+','.join(missing_baseline))
        need(not missing_constraints,'missing extracted constraint mappings: '+','.join(missing_constraints))
    observations=review.get('observations')
    if not isinstance(observations,list):errors.append('observations must be a list');observations=[]
    for obs in observations:
        if not isinstance(obs,dict):errors.append('invalid observation');continue
        need(nonempty(obs.get('interpretation')),'observation interpretation missing');calculation(obs,'observation')
    realization_reviews=review.get('realization_reviews',[])
    if not isinstance(realization_reviews,list):errors.append('realization_reviews must be a list');realization_reviews=[]
    realization_rows={}
    for row in realization_reviews:
        if not isinstance(row,dict) or not nonempty(row.get('count_check_id')):
            errors.append('realization review needs an actual count_check_id');continue
        rid=row['count_check_id']
        need(rid not in realization_rows,'duplicate realization review '+rid)
        realization_rows[rid]=row
        need(row.get('status') in ('reviewed','unverified') and nonempty(row.get('evidence')),'realization review needs status and concrete correspondence/cost/numerical evidence')
        roles=references(row.get('references'),'realization/'+rid)
        need({'input','proposal'}<=roles,'realization review needs original-input and actual proposal evidence: '+rid)
    if review['verdict']=='accept':
        need(bool(observations),'accept requires actual independent calculation observation')
        # Architecture acceptance needs actual source/proposal calculations,
        # not exhaustive implementation/profile/operand receipt coverage.
        # The audit owns relevance and sufficiency; the program still binds
        # evidence and requires disposition of every executed failure below.
        checked=[]
        for receipt in context.get('proposal_checks',[]):
            if not isinstance(receipt,dict):continue
            available=executed_values(receipt,sources,source_analysis,context['document'],context['budget'],context.get('execution_dependencies'))
            if not available['errors'] and any(child.get('checks') or child.get('observations') for child in available['subreceipts']):
                checked.append((receipt,available))
        need(bool(checked),'accept requires executed check_proposal evidence bound to this context, not only self-declared formulas')
        if checked:
            need(not any(row.get('status')=='fail' for row in checked[-1][0].get('checks',[])),
                 'accept contradicts an executed proposal consistency check')
        if any(receipt.get('missing_service_checks') for receipt,available in checked):
            prefix='accept must disclose unverified executed service capability as a resource_service condition: '
            need(any(row.get('topic')=='resource_service' and row.get('status')=='conditional' for row in review['checks']),
                 prefix+'checks[topic="resource_service"].status must be "conditional"')
            need(any(row.get('topic')=='resource_service' and row.get('kind')=='implementation_evidence' for row in review.get('unverified',[])),
                 prefix+'unverified[*] must include kind="implementation_evidence" and topic="resource_service" in the same row; put the descriptive service title in obligation/reason')
        transformed={}
        for receipt,available in checked:
            for row in receipt.get('work_result',{}).get('observations',[]):
                if row.get('realization')=='transformed':transformed[row['id']]=(row,receipt,available)
        need(set(realization_rows)==set(transformed),'accept requires exactly one independent review per transformed count correspondence')
        for rid,(row,latest,available) in transformed.items():
                physical=row.get('physical_realization',{})
                need(physical.get('coverage_complete') is True and physical.get('status')=='pass','accept lacks complete executed native physical cost: '+rid)
                assessment=realization_rows.get(rid,{})
                need(assessment.get('status')=='reviewed','accept leaves transformed semantic correspondence unreviewed: '+rid)
                physical_ids={key for receipt in available['subreceipts']
                              if any(item.get('id')==rid for item in receipt.get('observations',[]))
                              for key in receipt['calculation_ids'] if '__physical__' in key}
                refs=assessment.get('references',[])
                need(isinstance(refs,list) and any(isinstance(ref,dict) and ref.get('request_id')==latest.get('request_id') and ref.get('calculation_id') in physical_ids for ref in refs),'realization review needs that actual executed native-cost calculation: '+rid)
    for finding in review['findings']:
        need(finding.get('category') in ('design','extraction','tool','evidence'),'finding category missing: '+finding['id'])
        roles=references(finding.get('references'),'finding/'+finding['id'])
        if finding.get('severity')=='blocking':need('proposal' in roles,'blocking finding needs proposal evidence: '+finding['id'])
    for finding in review['findings']:
        if finding.get('severity')=='condition':
            need(finding.get('condition_kind') in ('external','implementation_evidence'),'condition must identify external or implementation evidence: '+finding['id'])
    for row in review['unverified']:
        if not isinstance(row,dict):errors.append('unverified obligation must be an object');continue
        need(nonempty(row.get('topic')) and nonempty(row.get('obligation')) and nonempty(row.get('reason')),'unverified obligation detail missing')
        need(row.get('kind') in ('design_decision','audit_evidence','implementation_evidence'),'unverified kind must distinguish design decision, audit evidence and implementation evidence')
        if row.get('kind')=='design_decision' and review['verdict']!='revise':errors.append('unverified design decision requires revise and an open blocking design finding: '+str(row.get('topic')))
        if review['verdict']=='accept' and row.get('kind')=='audit_evidence':errors.append('accept lacks audited evidence: '+str(row.get('topic')))
        references(row.get('references'),'unverified/'+str(row.get('topic')))
    corrections=review.get('baseline_corrections',[])
    if not isinstance(corrections,list):errors.append('baseline_corrections must be a list');corrections=[]
    for row in corrections:
        if not isinstance(row,dict) or not nonempty(row.get('obligation_id')) or row['obligation_id'] not in baseline_ids or not nonempty(row.get('correction')) or not nonempty(row.get('reason')):
            errors.append('baseline correction needs known obligation id, correction and reason');continue
        roles=references(row.get('references'),'baseline correction/'+row['obligation_id'])
        need('input' in roles,'baseline corrections require original-input evidence')
        # Rebuilding may receive only source corrections, never the proposal.
        try:
            need(all(isinstance(x,dict) and is_original_source(resolve_citation(x,sources,context.get('citations',[]),input_only=True).get('file')) for x in row.get('references',[])),'baseline correction references must directly quote original inputs only')
        except (KeyError,TypeError,ValueError):errors.append('baseline correction references must directly quote original inputs only')
        targets=row.get('analysis_targets',[])
        need(isinstance(targets,list),'baseline correction analysis_targets must be a list')
        for target in targets if isinstance(targets,list) else []:
            section=target.get('section') if isinstance(target,dict) else None
            declared=baseline.get('analysis',{}).get(section,[]) if isinstance(section,str) else []
            valid=section in ('facts','workloads','computations','states','flows') and isinstance(target,dict) and nonempty(target.get('id')) and set(target)<={'section','id','workload'} and any(isinstance(item,dict) and item.get('id')==target.get('id') for item in (declared if isinstance(declared,list) else []))
            if isinstance(target,dict) and 'workload' in target:
                valid=valid and any(item.get('id')==target['workload'] for item in baseline.get('analysis',{}).get('workloads',[]) if isinstance(item,dict))
            need(valid,'baseline correction target must name an actual original analysis declaration/profile')
    prior=context.get('previous_audit') or {}
    open_prior={f['id'] for f in prior.get('findings',[]) if isinstance(f,dict) and f.get('severity')=='blocking' and f.get('disposition')=='open' and isinstance(f.get('id'),str)}
    revisions=review.get('revision_checks');seen=set()
    if not isinstance(revisions,list):errors.append('revision_checks must be a list');revisions=[]
    for row in revisions:
        if not isinstance(row,dict) or not isinstance(row.get('finding_id'),str):errors.append('invalid revision check');continue
        fid=row['finding_id'];need(fid not in seen,'duplicate revision check '+fid);seen.add(fid)
        need(fid in open_prior,'unknown prior blocking finding '+fid)
        need(row.get('status') in ('resolved','open','rebutted') and nonempty(row.get('evidence')),'invalid revision disposition '+fid)
        references(row.get('references'),'revision/'+fid)
        if review['verdict']=='accept' and row.get('status')=='open':errors.append('accept contradicts unresolved prior finding '+fid)
    need(open_prior<=seen,'missing prior blocking finding checks: '+','.join(sorted(open_prior-seen)))
    failures={(request_id,receipt['id'],check['id']):(receipt,check)
              for request_id,available in executed.items() for receipt in available['subreceipts']
              for check in receipt['checks'] if check.get('status')=='fail'}
    dispositions=review.get('counterexample_reviews',[])
    if not isinstance(dispositions,list):errors.append('counterexample_reviews must be a list');dispositions=[]
    seen=set();counterexamples=[]
    for row in dispositions:
        if not isinstance(row,dict):errors.append('counterexample review must be an object');continue
        key=tuple(row.get(field) for field in ('request_id','receipt_id','check_id'))
        if any(not nonempty(value) for value in key):errors.append('counterexample review requires actual request/receipt/check ids');continue
        if key not in failures:errors.append('counterexample review must name an actual binding-verified failed check: '+str(key));continue
        need(key not in seen,'duplicate counterexample review: '+str(key));seen.add(key)
        receipt,check=failures[key];label='counterexample/'+ '/'.join(key)
        need(nonempty(row.get('reason')),label+' needs a concrete interpretation and closing evidence')
        roles=references(row.get('references'),label)
        # The validated row identity already selects the actual failed check
        # and its program-owned quantities. Do not require a second copied
        # numeric/check ID merely to cite the same immutable evidence again.
        disposition=row.get('disposition')
        need(disposition in ('design_defect','audit_declaration_error','source_interpretation_error','unresolved'),label+' invalid disposition')
        if disposition=='design_defect':
            finding=next((item for item in review['findings'] if isinstance(item,dict) and item.get('id')==row.get('finding_id')),None)
            need(isinstance(finding,dict) and finding.get('category')=='design' and finding.get('severity')=='blocking' and finding.get('disposition')=='open',label+' needs its open blocking design finding')
            need(review['verdict']=='revise',label+' design defect requires revise')
            need({'input','proposal'}<=roles,label+' design defect requires original-input and proposal evidence')
        elif disposition=='audit_declaration_error':
            correction=row.get('correction_check',{})
            correction_valid=isinstance(correction,dict) and all(nonempty(correction.get(field)) for field in ('request_id','receipt_id','check_id'))
            target=executed.get(correction['request_id'],{}) if correction_valid else {}
            corrected=next((value for child in target.get('subreceipts',[]) if child['id']==correction.get('receipt_id') for value in child['checks'] if value['id']==correction.get('check_id')),None) if isinstance(correction,dict) else None
            related=isinstance(corrected,dict) and corrected.get('status')=='pass' and corrected.get('topic')==check.get('topic') and corrected.get('kind')==check.get('kind') and corrected['id']==check['id']
            if check.get('kind') in ('flow_binding','candidate_inventory_bound'):
                related=related and sorted(corrected.get('flow_ids',[]))==sorted(check.get('flow_ids',[])) and all(corrected.get(field)==check.get(field) for field in ('source_workload','direction','required_bytes'))
            elif check.get('kind') in ('source_count_correspondence','native_cost_operations','native_cost_cycles'):
                related=related and sorted(corrected.get('source_paths',[]))==sorted(check.get('source_paths',[])) and all(corrected.get(field)==check.get(field) for field in ('source_workload','unit','cost_group'))
                if check.get('kind')=='source_count_correspondence':related=related and corrected.get('expected')==check.get('expected')
            elif check.get('kind')=='abi_geometry':
                related=related and all(corrected.get(field)==check.get(field) for field in (
                    'geometry_id','geometry_kind','geometry_result','geometry_probe','abi_target','relation'))
            elif check.get('kind') in ('recompute_comparison','numeric_assertion'):
                same_target=related and all(corrected.get(field)==check.get(field) for field in ('target','relation','expected'))
                previous_selectors=check.get('source_selectors',[])
                current_selectors=corrected.get('source_selectors',[]) if isinstance(corrected,dict) else []
                shared=any(old==new for old in previous_selectors for new in current_selectors)
                new_proposal=any(isinstance(selector,dict) and selector.get('file') in PROPOSAL for selector in current_selectors)
                direct_correction=(same_target and corrected.get('declaration_sha256')!=check.get('declaration_sha256')
                                   and (shared or not previous_selectors and new_proposal))
                measurement=check.get('measurement',{})
                rebuilt=(corrected.get('lineage_sha256s',{}).get(measurement.get('id'))==measurement.get('sha256')
                         and isinstance(measurement.get('sha256'),str)) if isinstance(corrected,dict) else False
                added_proposal=any(isinstance(selector,dict) and selector.get('file') in PROPOSAL and selector not in previous_selectors
                                   for selector in current_selectors)
                guard_rebuttal=(isinstance(corrected,dict) and corrected.get('status')=='pass'
                                and corrected.get('kind') in ('recompute_comparison','numeric_assertion')
                                and rebuilt and added_proposal
                                and corrected.get('measurement',{}).get('id')!=measurement.get('id'))
                related=direct_correction or guard_rebuttal
            need(related,label+' audit declaration correction requires a related actual passing check against the same frozen context')
            need({'input','proposal'}<=roles,label+' corrected audit declaration requires original-input and proposal evidence')
        elif disposition=='source_interpretation_error':
            correction=next((item for item in corrections if isinstance(item,dict) and item.get('obligation_id')==row.get('baseline_obligation_id')),None)
            targets=correction.get('analysis_targets',[]) if isinstance(correction,dict) else []
            need(bool(targets),label+' requires an explicit original-source baseline correction with actual analysis_targets')
            targets=[target for target in targets if isinstance(target,dict) and nonempty(target.get('section')) and nonempty(target.get('id')) and ('workload' not in target or nonempty(target['workload']))] if isinstance(targets,list) else []
            sid=check.get('source_workload');section='flows' if check.get('kind') in ('flow_binding','candidate_inventory_bound') else 'computations'
            affected=check.get('flow_ids',[]) if section=='flows' else check.get('source_paths',[])
            related_targets=False
            if isinstance(targets,list):
                workload_target=any(isinstance(target,dict) and target.get('section')=='workloads' and target.get('id')==sid for target in targets)
                direct_targets={target.get('id') for target in targets if isinstance(target,dict) and target.get('section')==section and target.get('workload',sid)==sid}
                dependency_targets={target.get('id') for target in targets if isinstance(target,dict) and target.get('section')=='facts'}
                for rid in affected:
                    dependencies={fact for child in context.get('source_execution',{}).get('subreceipts',[]) if child.get('output',{}).get('collection')==section and child.get('output',{}).get('key')==rid and child.get('output',{}).get('workload')==sid for fact in child.get('applicability',{}).get('dependencies',[])}
                    if dependencies & dependency_targets:direct_targets.add(rid)
                related_targets=workload_target or bool(affected) and set(affected)<=direct_targets or not affected and bool(targets)
            if check.get('kind') in ('recompute_comparison','numeric_assertion'):
                selectors=[selector for selector in check.get('source_selectors',[]) if is_original_source(selector.get('file'))]
                relevant=False
                for target in targets:
                    rows=baseline.get('analysis',{}).get(target['section'],[])
                    declaration=next((item for item in rows if isinstance(item,dict) and item.get('id')==target['id']),{}) if isinstance(rows,list) else {}
                    selected=declaration.get('source')
                    if isinstance(selected,dict) and any(selected==selector for selector in selectors):relevant=True
                    source_references=[]
                    for field in ('references','citations'):
                        if isinstance(declaration.get(field),list):source_references.extend(declaration[field])
                    for reference in source_references:
                        try:
                            reference=resolve_citation(reference,sources,context.get('citations',[]),input_only=True)
                            file=reference['file'];quote=reference['quote'];text=sources[file]
                            start=reference.get('start')
                            if type(start) is not int and text.count(quote)==1:start=text[:text.index(quote)].count('\n')+1
                            end=reference.get('end',start+quote.count('\n') if type(start) is int else None)
                            if type(start) is int and type(end) is int and any(selector['file']==file and start<=selector['line']<=end for selector in selectors):relevant=True
                        except (KeyError,TypeError,ValueError):continue
                related_targets=related_targets and relevant
            need(related_targets,label+' source correction must target its affected source declarations, workload or actual fact dependencies')
            need(review['verdict']!='accept',label+' an unexecuted source correction cannot support current accept')
            need('input' in roles,label+' source interpretation correction requires original-input evidence')
        elif disposition=='unresolved':
            need(review['verdict']=='insufficient_evidence',label+' unresolved interpretation requires insufficient_evidence')
            need(check.get('kind')!='candidate_inventory_bound',label+' complete candidate inventory deficit cannot be deferred as downstream evidence; identify a source interpretation correction or design defect')
            need(any(isinstance(item,dict) and item.get('topic')==row.get('unverified_topic') and item.get('kind') in ('audit_evidence','design_decision') for item in review['unverified']),label+' unresolved interpretation requires its explicit undecided correspondence')
        counterexamples.append({'request_id':key[0],'receipt_id':key[1],'check':copy.deepcopy(check),'disposition':disposition})
    for key in failures.keys()-seen:
        errors.append('counterexample requires explicit adjudication: '+'/'.join(key))
    return {'ok':not errors,'errors':errors,'bindings':bindings,'reference_diagnostics':reference_diagnostics,'counterexamples':counterexamples,
            'coverage':{'missing_baseline_ids':missing_baseline,'missing_constraint_ids':missing_constraints,'release_mapping_complete':not missing_baseline and not missing_constraints},
            'scope':'evidence existence, executed source/candidate correspondence, arithmetic receipts and report consistency only; interpreted shapes and mappings remain fallible, no proof of semantic completeness or hardware correctness. A valid negative/insufficient report may be partial; coverage gaps are explicit and cannot establish acceptance.'}


def recompute(request,sources,engine,citations=()):
    errors=[];values={};comparisons={};source_bindings=[]
    try:
        if not isinstance(request,dict) or set(request)-{'tool','facts','assumptions','calculations','numeric_probes'}:raise ValueError('only declared facts/assumptions/calculations/numeric_probes supported')
        # Resolve actual lookup receipts without modifying the submitted request.
        facts=json.loads(json.dumps(request.get('facts',[])))
        for fact in facts:
            for i,ref in enumerate(fact.get('citations',[])):
                if isinstance(ref,dict) and ('evidence_id' in ref or ('file' in ref and 'quote' not in ref)):
                    resolved=resolve_citation(ref,sources,citations)
                    source_bindings.append(citation_binding(ref,resolved,'fact/'+str(fact.get('id','?'))+'/citations/'+str(i)))
                    fact['citations'][i]=copy.deepcopy(resolved)
        requirements={'facts':facts,'constraints':[],'runtime_parameters':[]}
        prepared=engine.prepare_requirements(requirements,sources)
        requirements=prepared['requirements'];bindings=prepared['bindings']
        values,errors=engine.validate_requirements(requirements,sources)
        errors=prepared['errors']+errors
        for section in ('assumptions','calculations'):
            rows=request.get(section,[])
            if not isinstance(rows,list) or len(rows)>200:raise ValueError('invalid/oversized '+section)
            if section=='calculations' and not rows and not request.get('numeric_probes'):errors.append('at least one calculation or numeric probe required')
            for row in rows:
                try:
                    name=row['id'];keys=[k for k in ('value','formula') if k in row]
                    if not isinstance(name,str) or not name.isidentifier() or name in values or len(keys)!=1 or not row.get('unit') or not row.get('basis'):raise ValueError('new identifier, one expression, unit and basis required')
                    values[name]=engine.expression(row[keys[0]],values)
                    if 'expected' in row:
                        expected=engine.expression(row['expected'],values)
                        comparisons[name]={'actual':values[name],'expected':expected,'matches_expected':math.isclose(values[name],expected,rel_tol=1e-9,abs_tol=1e-9)}
                except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as e:errors.append(section+'/'+str(row.get('id','?') if isinstance(row,dict) else '?')+': '+str(e))
        numeric=engine.derive_numeric_probes(request.get('numeric_probes',[]),values)
        errors.extend('numeric/'+error for error in numeric['errors'])
        values.update(numeric['values'])
        for probe in numeric['probes']:
            for index,check in enumerate(probe['assertions']):
                key=probe['id']+'__assertion_'+str(index)
                if key in comparisons:raise ValueError('numeric assertion identifier collides with an existing comparison')
                comparisons[key]={'actual':check['actual'],'expected':check['expected'],'relation':check['relation'],'matches_expected':check['pass']}
        return {'numeric_probes':numeric,'comparison_evidence':_comparison_evidence(request,sources,numeric,values),'ok':not errors,'errors':errors,'values':values,'calculation_ids':[x['id'] for x in request.get('calculations',[]) if isinstance(x,dict) and x.get('id') in values]+list(numeric['values']),'comparisons':comparisons,'citation_bindings':source_bindings+bindings,'assumptions':request.get('assumptions',[]),'scope':'independent declared arithmetic; quoted values do not prove semantic relevance or complete cost coverage'}
    except (KeyError,TypeError,ValueError,SyntaxError,ZeroDivisionError,OverflowError) as e:return {'ok':False,'errors':errors+[str(e)],'values':values,'comparisons':comparisons,'citation_bindings':source_bindings}


def lookup(request,sources):
    evidence=[];errors=[]
    queries=request.get('queries')
    if set(request)-{'tool','queries'} or not isinstance(queries,list) or not 1<=len(queries)<=12:
        return {'ok':False,'errors':['lookup needs 1..12 file/query objects only'],'evidence':[]}
    for query in queries:
        if not isinstance(query,dict) or set(query)!={'file','query'} or not isinstance(query['file'],str) or query['file'] not in sources or not isinstance(query['query'],str) or not 1<=len(query['query'])<=1000:
            errors.append('invalid lookup file/query; supplied sources only');continue
        text=sources[query['file']];phrase=query['query'];position=0;count=0;lines=text.splitlines()
        while count<5:
            pos=text.find(phrase,position)
            if pos<0:break
            start=max(1,text[:pos].count('\n')-1);end=min(len(lines),text[:pos+len(phrase)].count('\n')+3)
            ref={'file':query['file'],'start':start,'end':end,'quote':source_window(text,start,end)}
            ref['source_sha256']=hashlib.sha256(text.encode()).hexdigest()
            ref['evidence_id']=hashlib.sha256(json.dumps(ref,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if not any(x['evidence_id']==ref['evidence_id'] for x in evidence):evidence.append(ref)
            position=pos+len(phrase);count+=1
        if count==0:errors.append('no exact match: '+query['file']+'/'+phrase)
    return {'ok':not errors,'errors':errors,'evidence':evidence,'scope':'read-only exact substring excerpts from caller-supplied packet; no semantic endorsement'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--calculator',help='trusted arithmetic dependency supplied by host, never by model')
    args=parser.parse_args();data=json.load(sys.stdin)
    if data['mode']=='baseline':result=validate_baseline(data['baseline'],data['sources'],data.get('citations',[]),
        source_analysis=data.get('source_analysis'),execution_dependencies=data.get('execution_dependencies'))
    elif data['mode']=='recompute':
        if not args.calculator:raise ValueError('recompute requires caller-provided calculator')
        spec=importlib.util.spec_from_file_location('shared_budget',args.calculator);engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)
        result=recompute(data['request'],data['sources'],engine,data.get('citations',[]))
    elif data['mode']=='lookup':result=lookup(data['request'],data['sources'])
    elif data['mode']=='validate':result=validate(data['review'],data['context'])
    else:raise ValueError('unsupported mode')
    json.dump(result,sys.stdout,ensure_ascii=False,indent=2);sys.stdout.write('\n')

if __name__=='__main__':main()
