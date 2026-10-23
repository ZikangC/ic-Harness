"""Charge executed representative paths once against source-bound work counts.

These are declaration relationships, not source-equivalence or timing proofs.
"""


def charge(component, groups, templates, names, expression, used):
    if 'cycles' in component:
        raise ValueError('execution cycles are program-derived; omit cycles')
    binding = component.get('execution')
    if not isinstance(binding, dict) or set(binding) - {'template', 'component', 'repetitions'}:
        raise ValueError('execution needs template/component and optional checked repetitions')
    template = next((t for t in templates if t['id'] == binding.get('template')), None)
    if template is None or template.get('status') not in ('pass', 'unverified'):
        raise ValueError('execution needs a successfully expanded actual template')
    part = binding.get('component')
    phases = template['components']
    if part == 'whole':
        selected = phases
        duration = template['cycles']
        issued = template['issued_operations']
    else:
        selected = [p for p in phases if p['id'] == part]
        if not selected:
            raise ValueError('execution component must name whole or actual phase id')
        duration = selected[0]['cycles']
        issued = selected[0]['issued_operations']
    keys = {(template['id'], p['id']) for p in selected}
    if keys & used:
        raise ValueError('execution template phase charged more than once')
    referenced = set(component['references'])
    covered = {g for p in selected for g in p['cost_groups']}
    if referenced != covered:
        raise ValueError('execution references must equal all bound phase cost_groups')
    if set(issued) - covered:
        raise ValueError('issued operation group is outside charged phase cost_groups')
    repetitions = None
    witness = {}
    for gid in sorted(covered):
        group = groups[gid]
        if group['workload'] != template['workload']:
            raise ValueError('execution and referenced cost workloads differ')
        if group['model'] == 'unsupported':
            raise ValueError('unsupported cost cannot be closed by an execution template')
        if group['model'] != 'compute':
            if issued.get(gid, 0):
                raise ValueError('arithmetic issues require a source-bound compute group')
            continue
        operations = group.get('operations')
        count = issued.get(gid, 0)
        if operations is None or count <= 0:
            raise ValueError('execution compute group needs known source operations and actual issued lane operations')
        if operations <= 0 or operations != int(operations) or operations % count:
            raise ValueError('source operations must be a positive integer multiple of executed lane operations; split distinct scopes/tails')
        factor = int(operations // count)
        if repetitions is not None and factor != repetitions:
            raise ValueError('covered compute groups imply different execution repetitions')
        repetitions = factor
        witness[gid] = {'task_operations': operations, 'issued_operations': count,
                        'operation_unit': group['operation_unit'], 'repetitions': factor}
    explicit = binding.get('repetitions')
    if explicit is not None:
        explicit = expression(explicit, names)
        if type(explicit) not in (int, float) or explicit < 1 or explicit != int(explicit):
            raise ValueError('positive integer execution repetitions required')
        if repetitions is not None and explicit != repetitions:
            raise ValueError('explicit repetitions conflict with source/issued operation ratio')
        repetitions = int(explicit)
    if repetitions is None:
        raise ValueError('nonarithmetic-only execution needs explicit source/traffic-backed repetitions')
    cost = duration * repetitions
    lower = max(groups[g]['scheduled_cycles'] for g in covered)
    if cost < lower:
        raise ValueError('execution duration below whole-task resource/service lower bound ' + str(lower))
    used.update(keys)
    return cost, {'template': template['id'], 'component': part,
                  'representative_cycles': duration, 'repetitions': repetitions,
                  'count_witness': witness, 'peak_buffer_bits': template['peak_buffer_bits'],
                  'scope': 'inclusive declared path duration counted once; source scope, index mapping, inter-path dependencies, sharing and physical II require independent audit'}
