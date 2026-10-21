"""Partial construction is mutable guidance, never a production constraint."""
import hashlib
import json

FIELDS = ('heat_start', 'heat_end', 'forge_start', 'forge_end', 'furnace')
ELIGIBLE_FAILURES = {'construction_deadend', 'construction_budget', 'resource_budget'}


def describe_partial(sc, partial, failure_kind):
    from longda_cpsat.model import flatten
    total = len(flatten(sc))
    errors = []
    known = 0
    if not isinstance(partial, dict):
        return dict(eligible=False, reason='invalid_partial', errors=['Expected object'])
    for key in FIELDS:
        values = partial.get(key)
        if not isinstance(values, list) or len(values) != total:
            errors.append(f'{key}: expected {total} entries')
            continue
        for x in values:
            if x is None:
                continue
            if type(x) is not int or x < 0 or (key == 'furnace' and x >= sc['furnace_count']):
                errors.append(f'{key}: invalid value')
                break
            known += 1
    forged = sum(type(x) is int for x in partial.get('forge_start', []))
    allowed = failure_kind in ELIGIBLE_FAILURES
    return dict(eligible=allowed and not errors and known > 0,
                reason=('invalid_partial' if errors else 'failure_requires_repair' if not allowed
                        else 'empty_partial' if not known else 'mutable_partial_hint'),
                hint_values=known, scheduled_forges=forged, total_operations=total,
                failure_kind=failure_kind, errors=errors,
                content_hash=hashlib.sha256(json.dumps(partial, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                is_complete_solution=False, can_change_hinted_values=True)


def add_partial_hint(model, variables, partial):
    """Only AddHint. Out-of-domain values are diagnosed, never fixed or clamped."""
    counts, rejected = {}, []
    for key in FIELDS:
        counts[key] = 0
        for i, (var, x) in enumerate(zip(variables[key], partial.get(key, []))):
            if x is None:
                continue
            domain = list(var.proto.domain)
            if type(x) is not int or not any(lo <= x <= hi for lo, hi in zip(domain[::2], domain[1::2])):
                rejected.append(dict(field=key, operation=i, value=x))
                continue
            model.add_hint(var, x)
            counts[key] += 1
    return dict(count=sum(counts.values()), by_field=counts, skipped_out_of_domain=rejected,
                transport_guidance=False, fixed=False)
