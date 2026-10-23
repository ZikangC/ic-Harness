"""Skill arithmetic facade and independent-audit delivery policy."""
import importlib.util
from pathlib import Path
_path = Path(__file__).resolve().parents[1]/'skills/microarchitecture-design/scripts/evaluate.py'
_spec = importlib.util.spec_from_file_location('microarchitecture_budget', _path)
_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_engine)
expression = _engine.expression
bind_unique_citations = _engine.bind_unique_citations
validate_requirements = _engine.validate_requirements
calculate = _engine.calculate

_audit_path=Path(__file__).resolve().parents[1]/'skills/microarchitecture-audit/scripts/validate_review.py'
_audit_spec=importlib.util.spec_from_file_location('microarchitecture_review',_audit_path)
_audit_engine=importlib.util.module_from_spec(_audit_spec)
_audit_spec.loader.exec_module(_audit_engine)
validate_review=_audit_engine.basic_errors


def delivery_decision(document, budget, review, report_check=None):
    errors = validate_review(review)
    reasons = list(budget.get('errors', [])) + errors
    if not report_check or not report_check.get('ok'):reasons.extend((report_check or {}).get('errors') or ['review not validated against frozen audit packet'])
    if budget.get('gate',{}).get('status')=='unknown':reasons.append('program budget scope unresolved: '+str(budget['gate']))
    selected = budget.get('candidates', {}).get(document.get('selected'), {}).get('checks', [])
    for check in selected:
        if check.get('kind')=='hard' and check.get('estimate_pass') is False:
            reasons.append('selected hard budget violated: '+check['constraint'])
        if check.get('kind')=='hard' and check.get('status')=='unverified' and check.get('verification')!='implementation':
            reasons.append('selected design hard constraint unverified: '+check['constraint'])
    if not errors:
        for topic in review['checks']:
            if topic['status']=='fail':
                reasons.append('audit check failed: '+topic['topic'])
        for f in review['findings']:
            if f['severity']=='blocking' and f['disposition']=='open':
                reasons.append(f['id']+': '+f['issue'])
        if review['verdict']!='accept':
            reasons.append('audit verdict: '+review['verdict'])
    return {'deliverable': not reasons and budget.get('ok',False), 'reasons': reasons,
            'implementation_verified': False,
            'conditions': [f for f in review.get('findings',[]) if isinstance(f,dict) and f.get('severity')=='condition']}
