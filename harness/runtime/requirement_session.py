"""Bounded source-extraction transport, whole-fact repair and checkpoint replay."""
import ast
import copy
import importlib.util
import json

from harness.prompts import architecture as prompts
from harness.runtime.run_store import dump,digest,feedback_revision_seed


def identity(requirements):
    return digest(json.dumps(requirements,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())


def validate_repair(request):
    fields={'base_sha256','facts'};optional={'storage_area_model','remove_fact_ids'}
    if not isinstance(request,dict) or not fields<=set(request) or set(request)-(fields|optional):
        raise ValueError('REQUIREMENTS_REPAIR requires base_sha256 and facts; only storage_area_model and remove_fact_ids are optional')
    if not isinstance(request['base_sha256'],str) or not request['base_sha256'] or not isinstance(request['facts'],list):
        raise ValueError('repair needs the actual base_sha256 and an array of complete fact rows')
    removals=request.get('remove_fact_ids',[])
    if not isinstance(removals,list) or any(not isinstance(name,str) or not name.isidentifier() for name in removals) or len(removals)!=len(set(removals)):
        raise ValueError('remove_fact_ids must be an array of unique actual fact identifiers')
    if not request['facts'] and not removals and 'storage_area_model' not in request:raise ValueError('empty requirements repair')
    if 'storage_area_model' in request and not isinstance(request['storage_area_model'],dict):
        raise ValueError('storage_area_model replacement must be a complete object')
    ids=set()
    for row in request['facts']:
        if not isinstance(row,dict):raise ValueError('each repaired fact must be a complete raw declaration')
        name=row.get('id')
        if not isinstance(name,str) or not name.isidentifier() or name in ids:
            raise ValueError('repair fact IDs must be unique identifier strings')
        ids.add(name)
        forms=set(row)&{'source','convert','formula'}
        if len(forms)!=1 or 'value' in row or 'converted_from' in row:raise ValueError('complete raw repaired fact needs exactly one source, convert or formula; authored literal values/materialized records are forbidden and omitted fields are not merged')
        if 'source' in forms:
            if 'citations' in row or not isinstance(row['source'],dict):raise ValueError('source fact uses its selector instead of copied citations')
            if 'unit' in row and (not isinstance(row['unit'],str) or not row['unit'].strip()):raise ValueError('source unit override must be nonempty when supplied')
        elif 'convert' in forms:
            if 'unit' in row or not isinstance(row['convert'],dict) or set(row['convert'])!={'fact','to_unit'}:
                raise ValueError('convert fact needs convert={fact,to_unit}; unit is program materialized')
            if any(not isinstance(row['convert'][key],str) or not row['convert'][key].strip() for key in ('fact','to_unit')):
                raise ValueError('convert needs an actual fact identifier and target unit')
        elif not isinstance(row.get('unit'),str) or not row['unit'].strip():raise ValueError('formula fact requires a nonempty unit')
        if ('source' not in forms and 'convert' not in forms) or ('convert' in forms and 'citations' in row):
            if not isinstance(row.get('citations'),list) or not row['citations']:raise ValueError('formula facts and explicitly cited conversions require nonempty citations')
    if ids&set(removals):raise ValueError('a fact cannot be removed and replaced in the same repair')


def parse_turn(text):
    full='<<<REQUIREMENTS>>>';repair='<<<REQUIREMENTS_REPAIR>>>'
    value=text.strip()
    if value.startswith(full) and text.count(full)==1 and repair not in text:
        return {'requirements':prompts.parse_payload(value[len(full):])}
    if value.startswith(repair) and text.count(repair)==1 and full not in text:
        request=prompts.parse_payload(value[len(repair):]);validate_repair(request)
        return {'repair':request}
    raise ValueError('return exactly <<<REQUIREMENTS>>> with one complete requirements object, or <<<REQUIREMENTS_REPAIR>>> with base_sha256/facts and optional storage_area_model/remove_fact_ids; no bare JSON, narration or extra frames')


def _expression_names(value):
    class Names(ast.NodeVisitor):
        def __init__(self):self.names=set()
        def visit_Name(self,node):self.names.add(node.id)
        def visit_Call(self,node):
            for arg in node.args:self.visit(arg)
            for arg in node.keywords:self.visit(arg.value)
    names=Names();names.visit(ast.parse(str(value),mode='eval'));return names.names


def _retained_references(requirements):
    """Read declared dependencies only; do not repair or infer their semantics."""
    refs=set()
    for fact in requirements.get('facts',[]):
        if 'formula' in fact:refs.update(_expression_names(fact['formula']))
        if 'convert' in fact:
            target=fact['convert'].get('fact') if isinstance(fact['convert'],dict) else None
            if isinstance(target,str):refs.add(target)
    for con in requirements.get('constraints',[]):
        if isinstance(con,dict) and isinstance(con.get('limit'),str):refs.add(con['limit'])
    for parameter in requirements.get('runtime_parameters',[]):
        domain=parameter.get('domain',{}) if isinstance(parameter,dict) else {}
        if not isinstance(domain,dict):continue
        for key in ('lower','upper'):
            if isinstance(domain.get(key),str):refs.add(domain[key])
        if isinstance(domain.get('values'),list):refs.update(x for x in domain['values'] if isinstance(x,str))
    for rule in requirements.get('storage_rules',[]):
        if not isinstance(rule,dict):continue
        for key in ('min_depth','width_multiple','read_ports','write_ports','read_latency_cycles'):
            if key in rule:refs.update(_expression_names(rule[key]))
    model=requirements.get('storage_area_model')
    if isinstance(model,dict) and isinstance(model.get('density_fact'),str):refs.add(model['density_fact'])
    return refs


def repair_requirements(previous,request):
    """Edit only raw declarations; source checking still decides whether they hold."""
    validate_repair(request)
    if not isinstance(previous,dict) or request['base_sha256']!=identity(previous):
        raise ValueError('requirements repair base_sha256 does not match the last unaccepted raw declaration')
    rows=previous.get('facts')
    if not isinstance(rows,list) or any(not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'].isidentifier() for row in rows):
        raise ValueError('base needs actual fact IDs; return a full REQUIREMENTS object to replace invalid structure')
    ids=[row['id'] for row in rows]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate base fact IDs require a full REQUIREMENTS object')
    removals=set(request.get('remove_fact_ids',[]))
    if removals-set(ids):raise ValueError('remove_fact_ids contains unknown fact IDs: '+str(sorted(removals-set(ids))))
    replacements={row['id']:copy.deepcopy(row) for row in request['facts']}
    result=copy.deepcopy(previous)
    result['facts']=[replacements.pop(row['id'],row) for row in result['facts'] if row['id'] not in removals]
    result['facts'].extend(replacements.values())
    if 'storage_area_model' in request:result['storage_area_model']=copy.deepcopy(request['storage_area_model'])
    if removals:
        try:dangling=removals&_retained_references(result)
        except (ValueError,TypeError,SyntaxError,AttributeError) as error:
            raise ValueError('cannot verify retained references for deletion; correct malformed declarations or return a full REQUIREMENTS object: '+str(error)) from error
        if dangling:raise ValueError('cannot remove referenced facts: '+str(sorted(dangling)))
    # Source selectors require actual integer types; True -> 1 is a real
    # declaration repair even though Python dictionary equality ignores it.
    if identity(result)==identity(previous):raise ValueError('requirements repair does not change the unaccepted extraction')
    return result


def _checker(out):
    path=out/'snapshot/harness/skills/microarchitecture-design/scripts/evaluate.py'
    spec=importlib.util.spec_from_file_location('frozen_requirement_checker',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def _check(checker,requirements,sources):
    try:
        _,errors=checker.validate_requirements(requirements,sources)
        if not requirements.get('constraints'):errors.append('no requirements constraints extracted')
        return errors
    except (KeyError,ValueError,TypeError,SyntaxError,ZeroDivisionError,OverflowError) as error:
        return [type(error).__name__+': '+str(error)]


def _record(path,value):
    """A replay may verify a version, never silently rewrite its evidence."""
    if path.exists():
        if json.loads(path.read_text())!=value:raise ValueError('requirements checkpoint differs from frozen replay: '+path.name)
    else:dump(path,value)


def _materialize(checker,declaration,sources):
    result=checker.prepare_requirements(copy.deepcopy(declaration),sources)
    requirements=copy.deepcopy(result['requirements']);bindings=copy.deepcopy(result['bindings']);errors=list(result['errors'])
    bindings.extend(checker.bind_unique_citations(requirements,sources))
    errors.extend(_check(checker,requirements,sources))
    return requirements,bindings,list(dict.fromkeys(errors))


def _receipt(declaration,requirements,catalogue,sources):
    return {'declaration_sha256':identity(declaration),'requirements_sha256':identity(requirements),
            'catalogue_sha256':identity(catalogue),'source_sha256':{name:digest(text.encode()) for name,text in sorted(sources.items())},
            'selector_index_base':1,'scope':'frozen lexical source bindings and program checks; no semantic inference or requirement coverage proof'}


def prepare(out,sources,invoke,max_repairs,*,revision_feedback=None):
    """Preserve completed model turns; the caller freezes inputs and charges calls."""
    base=out/'snapshot/harness/skills/microarchitecture-design'
    skill=(base/'SKILL.md').read_text();reference=(base/'references/requirements-contract.md').read_text()
    checker=_checker(out);accepted=out/'extraction.json'
    catalogue=checker.source_quantities(sources);_record(out/'source-quantities.json',catalogue)
    quantities=checker.quantity_view(sources)
    seed=feedback_revision_seed(revision_feedback,sources)
    if seed is not None:
        declaration=copy.deepcopy(seed['requirement_declaration'])
        requirements,bindings,errors=_materialize(checker,declaration,sources)
        if requirements!=seed['proposal']['requirements']:
            errors.append('native revision seed requirement replay differs from proposal.requirements; no fact rebinding or fallback extraction')
        receipt=_receipt(declaration,requirements,catalogue,sources)
        _record(out/'extraction.seed.json',{'origin':'snapshot/feedback.json#native_revision_seed',
            'proposal_sha256':identity(seed['proposal']),'receipt':receipt,'bindings':bindings,
            'requirements':requirements,'ok':not errors,'errors':errors,
            'scope':'current frozen checker executed raw source-selector replay; imported interpretation remains fallible, not a new extraction model response or old budget evidence'})
        if errors:return None,errors
        _record(out/'extraction.declaration.json',declaration)
        _record(out/'extraction.receipt.json',receipt)
        _record(accepted,requirements)
        return requirements,[]
    if accepted.exists():
        raw_path=out/'extraction.declaration.json';receipt_path=out/'extraction.receipt.json'
        if not raw_path.exists() or not receipt_path.exists():raise ValueError('accepted requirements checkpoint lacks raw declaration or binding receipt')
        requirements=json.loads(accepted.read_text());declaration=json.loads(raw_path.read_text())
        if json.loads(receipt_path.read_text())!=_receipt(declaration,requirements,catalogue,sources):raise ValueError('accepted requirements checkpoint identity differs from frozen declaration/source/catalogue')
        replay,bindings,errors=_materialize(checker,declaration,sources)
        if errors:raise ValueError('accepted requirements checkpoint fails frozen checks: '+str(errors))
        if replay!=requirements:raise ValueError('accepted requirements checkpoint differs from raw source binding replay')
        return requirements,[]
    previous=None;errors=[]
    for attempt in range(max_repairs+1):
        label=f'extract-{attempt}';path=out/(label+'.json');raw_path=out/(label+'.declaration.json')
        reply_path=out/(label+'.reply.json');check_path=out/(label+'.check.json')
        if raw_path.exists():declaration=json.loads(raw_path.read_text())
        else:
            if path.exists():raise ValueError('materialized extraction has no raw declaration checkpoint: '+label)
            if not reply_path.exists() and check_path.exists():
                errors=json.loads(check_path.read_text()).get('errors',[]);continue
            message=prompts.extract(skill+'\n'+reference,sources,previous,errors,quantities=quantities)
            if previous is None:
                message+='\nCURRENT STEP: extract source requirements only. Prefer source selectors for original numbers and convert for unit conversions; the host binds them without choosing facts. Return ONLY <<<REQUIREMENTS>>> once, followed by the complete raw requirements JSON. The marker is not a closing delimiter. No candidate, design narrative or explanatory text.'
            else:
                message+='\nCURRENT STEP: repair the unaccepted RAW declaration. Prefer ONLY <<<REQUIREMENTS_REPAIR>>> {base_sha256,facts:[complete corrected/new fact rows],remove_fact_ids:optional_explicit_unreferenced_ids,storage_area_model:optional_complete_object}. Preserve other declarations and all constraints/prose/domains; omitted fact fields are not merged. All source selector line/number indexes are one based. Deletion cannot leave formula, convert, constraint, runtime-domain, storage-rule or density references. If other fields must change, return the full <<<REQUIREMENTS>>> object. No narration or closing marker. Actual raw base_sha256: '+identity(previous)
            try:
                reply=json.loads(reply_path.read_text()) if reply_path.exists() else invoke(label,'architecture',message,parse_turn)
                if not reply_path.exists():dump(reply_path,reply)
                if set(reply)=={'requirements'} and isinstance(reply['requirements'],dict):declaration=copy.deepcopy(reply['requirements'])
                elif set(reply)=={'repair'}:declaration=repair_requirements(previous,reply['repair'])
                else:raise ValueError('parsed extraction reply needs exactly requirements or repair; use the required frame')
                _record(raw_path,declaration)
            except (KeyError,ValueError,TypeError,IndexError) as error:
                errors=['model extraction format/replacement rejected: '+str(error)]
                _record(check_path,{'ok':False,'errors':errors});continue
        previous=declaration
        requirements,bindings,errors=_materialize(checker,declaration,sources)
        _record(out/(label+'.bindings.json'),bindings);_record(path,requirements)
        _record(out/(label+'.receipt.json'),_receipt(declaration,requirements,catalogue,sources))
        _record(check_path,{'ok':not errors,'errors':errors})
        if not errors:
            _record(out/'extraction.declaration.json',declaration)
            _record(out/'extraction.receipt.json',_receipt(declaration,requirements,catalogue,sources))
            dump(accepted,requirements);return requirements,[]
    return None,errors
