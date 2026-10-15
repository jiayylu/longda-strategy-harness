"""Fixed HTML presentation of a saved, independently audited complete plan.

No CP-SAT model is built and no search is started here. Both pages embed the
same snapshot. This module supports the two existing transport modes and any
configured order, furnace, vehicle count or planning horizon.
"""
from __future__ import annotations

from collections import defaultdict
import colorsys
from fractions import Fraction
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import tempfile

from .audit import audit, flatten_operations
from .result_summary import build_summary

TEMPLATE_VERSION = 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _color(i):
    rgb = colorsys.hls_to_rgb(((i * 137.508 + 205) % 360) / 360, .43, .56)
    return '#' + ''.join(f'{round(c * 255):02x}' for c in rgb)


def build_view_data(scenario, schedule, *, run_name='', first_only=False):
    """Audit afresh, then derive display rows without mutating solver records."""
    checked = audit(scenario, schedule)
    if not checked['valid']:
        raise ValueError('Saved schedule failed independent audit; HTML not produced')
    summary = build_summary(scenario, checked)
    ops = flatten_operations(scenario)
    scale = scenario['ticks_per_minute']
    finish = checked['makespan_ticks']
    transport = scenario.get('transport', {})
    enabled = transport.get('enabled') is True
    vehicle_count = transport.get('vehicle_count', 1) if enabled else 0
    heat_end = schedule['heat_end'] if enabled else schedule['forge_start']
    forge_end = [s + q['forge_ticks'] for s, q in zip(schedule['forge_start'], ops)]
    by_order = defaultdict(list)
    for i, op in enumerate(ops):
        by_order[op['order']].append(i)
    order_data = []
    for i, order in enumerate(scenario['orders']):
        ids = by_order[order['id']]
        completion = (checked['final_delivery_ticks'][order['id']] if enabled
                      else max(forge_end[j] for j in ids))
        order_data.append(dict(id=order['id'], route=order['route'], color=_color(i),
                               completion=completion, fires=len(ids),
                               release=ops[ids[0]]['release_ticks']))

    steps = []

    def step(kind, start, end, **fields):
        if end > start:
            steps.append(dict(id=len(steps), kind=kind, start=start, end=end, **fields))

    slots = {}
    for furnace in range(scenario['furnace_count']):
        ends = [0] * scenario['capacity']
        indices = sorted((i for i in range(len(ops)) if schedule['furnace'][i] == furnace),
                         key=lambda i: (schedule['heat_start'][i], heat_end[i], i))
        for i in indices:
            lane = next((j for j, end in enumerate(ends) if end <= schedule['heat_start'][i]), None)
            if lane is None:
                raise ValueError('No presentation lane available for audited furnace occupancy')
            ends[lane] = heat_end[i]
            slots[i] = lane
    for i, op in enumerate(ops):
        common = dict(order=op['order'], fire=op['fire'], op=i)
        step('hold', schedule['heat_start'][i], heat_end[i], furnace=schedule['furnace'][i],
             lane=slots[i], temp=op['temp'], lower=op['hold_min_ticks'], upper=op['hold_max_ticks'], **common)
        step('forge', schedule['forge_start'][i], forge_end[i], **common)

    moves = schedule['transport'] if enabled else []
    for move in moves:
        step('loaded', move['start'], move['end'], order=move['order'], fire=ops[move['op']]['fire'],
             op=move['op'], vehicle=move['vehicle'], source=move['source'], destination=move['destination'],
             move_kind=move['kind'], task=move['id'])
    for move in checked.get('canonical_empty_moves', []):
        step('empty', move['start'], move['end'], vehicle=move['vehicle'],
             source=move['source'], destination=move['destination'])
    for furnace in range(scenario['furnace_count']):
        cursor, temperature = 0, scenario['initial_temperature']
        for ramp in sorted((r for r in checked['canonical_ramps'] if r['furnace'] == furnace),
                           key=lambda r: r['start_ticks']):
            step('temperature', cursor, ramp['start_ticks'], furnace=furnace, temp=temperature)
            step('ramp', ramp['start_ticks'], ramp['end_ticks'], furnace=furnace,
                 from_temp=ramp['from_temperature'], to_temp=ramp['to_temperature'])
            cursor, temperature = ramp['end_ticks'], ramp['to_temperature']
        step('temperature', cursor, finish, furnace=furnace, temp=temperature)

    for order in order_data:
        first_inbound = min((m['start'] for m in moves if m['order'] == order['id']), default=None)
        first_heat = schedule['heat_start'][by_order[order['id']][0]]
        first = first_inbound if enabled else first_heat
        step('unreleased', 0, order['release'], order=order['id'])
        step('cold', order['release'], first, order=order['id'])

    # Verify the presentation did not drop, duplicate or double-count events.
    for order in order_data:
        chain = sorted((s for s in steps if s.get('order') == order['id']), key=lambda s: (s['start'], s['end']))
        if (not chain or chain[0]['start'] != 0 or chain[-1]['end'] != order['completion']
                or any(a['end'] != b['start'] for a, b in zip(chain, chain[1:]))):
            raise ValueError('Incomplete presentation chain for order: ' + order['id'])
    resource_groups = [[s for s in steps if s['kind'] == 'forge']]
    resource_groups.extend([s for s in steps if s.get('vehicle') == v] for v in range(vehicle_count))
    for events in resource_groups:
        events.sort(key=lambda s: (s['start'], s['end']))
        if any(a['end'] > b['start'] for a, b in zip(events, events[1:])):
            raise ValueError('Presentation introduced overlapping exclusive resource events')
    for day in summary['daily']:
        lo, hi = day['start_minutes'] * scale, day['end_minutes'] * scale
        busy = sum(max(0, min(s['end'], hi) - max(s['start'], lo)) for s in steps if s['kind'] == 'forge')
        if busy != day['busy_ticks']:
            raise ValueError('Presentation daily forging does not match independent audit')
    for ramp in (s for s in steps if s['kind'] == 'ramp'):
        if any(min(ramp['end'], h['end']) > max(ramp['start'], h['start'])
               for h in steps if h['kind'] == 'hold' and h['furnace'] == ramp['furnace']):
            raise ValueError('Presentation ramp overlaps loaded furnace')

    horizon = int(Fraction(str(scenario['horizon_minutes'])) * scale)
    # Display complete 24-hour days so the visible mean has the same denominator
    # as the daily statistic, including a last partial planning day.
    axis_end = max(1, (horizon + 1440 * scale - 1) // (1440 * scale)) * 1440 * scale
    return dict(template_version=TEMPLATE_VERSION, orders=order_data, steps=steps, summary=summary,
                scale=scale, finish=finish, horizon=axis_end, planning_horizon=horizon,
                furnace_count=scenario['furnace_count'], capacity=scenario['capacity'],
                vehicle_count=vehicle_count, transport_enabled=enabled,
                travel_minutes=transport.get('travel_minutes', 5) if enabled else 0,
                initial_temperature=scenario['initial_temperature'],
                ramp_up=scenario['ramp_up_deg_per_min'], ramp_down=scenario['ramp_down_deg_per_min'],
                schedule_sha256=_sha(schedule), scenario_sha256=_sha(scenario),
                run=run_name, first_only=first_only,
                solution_label='首个合法方案' if first_only else '已保存合法方案')


def _write(path, content):
    """Replace one display artifact atomically; solver files are never touched."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.' + path.name, suffix='.tmp', delete=False) as file:
        tmp = Path(file.name)
        file.write(content)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def export_run_html(out):
    """Generate views for a saved result, including old runs, without solving."""
    out = Path(out).resolve()
    scenario = json.loads((out / 'scenario.json').read_text(encoding='utf-8'))
    best_path = out / 'best.json'
    if not best_path.exists():
        raise ValueError('No complete saved schedule; HTML export does not start a search')
    best = json.loads(best_path.read_text(encoding='utf-8'))
    report_path = out / 'report.json'
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    data = build_view_data(scenario, best['schedule'], run_name=out.name,
                           first_only=report.get('first_only') is True or report.get('phase') == 'first_solution_found')
    template = files('longda_cpsat').joinpath('templates/schedule.html').read_text(encoding='utf-8')
    # Prevent arbitrary external order names from closing the embedded data tag.
    payload = _json(data).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    artifacts = {}
    for view, title in (('orders', '订单视角'), ('equipment', '设备视角')):
        artifacts[view + '.html'] = template.replace('__VIEW__', view).replace('__TITLE__', title).replace('__DATA__', payload)
    artifacts['view_data.json'] = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    dest = out / 'views'
    manifest = dict(valid=True, template_version=TEMPLATE_VERSION, source='best.json',
                    schedule_sha256=data['schedule_sha256'], scenario_sha256=data['scenario_sha256'],
                    orders=len(data['orders']), operations=data['summary']['operations'],
                    daily_minutes=[d['busy_minutes'] for d in data['summary']['daily']],
                    order_view=str(dest / 'orders.html'), equipment_view=str(dest / 'equipment.html'),
                    daily_totals_match=True, complete_material_chains=True, exclusive_resource_nonoverlap=True)
    dest.mkdir(parents=True, exist_ok=True)
    for filename, content in artifacts.items():
        _write(dest / filename, content)
    _write(dest / 'export_manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest
