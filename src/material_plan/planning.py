"""Time-phased supply netting with planned-release-driven dependent demand.

Material netting determines *how much* each sku must be replenished per
requirement bucket.  Capacity-constrained manufactured items are then scheduled
into a shared per-workcenter daily capacity calendar: a shortage may be split
into several production batches whose release dates differ, each completing no
later than the requirement bucket.  Component demand is exploded at each
batch's *actual* release date.

BOM levels are planned parent-first.  Within one level the skus are
independent, so their constrained shortages are allocated to the shared
calendar in earliest-deadline-first order with a latest-date-first packing
policy; this keeps early days available for shorter windows and never
double-books a workcenter.
"""
from collections import defaultdict
from datetime import date, timedelta
from .common import fields, text, integer, array
from .bom import compile_items

HORIZON_DAYS = 3650


def day(value):
    text(value)
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('ISO date required')
    return result


def _capacity_calendar(raw, as_of):
    """Validate optional capacities and return a mutable availability map.

    Unlisted (workcenter, date) pairs have zero capacity.
    """
    calendar = {}
    seen = set()
    for row in array(raw):
        fields(row, 'workcenter date capacity')
        workcenter = text(row['workcenter'])
        when = day(row['date'])
        if when < as_of or (when - as_of).days > HORIZON_DAYS:
            raise ValueError('capacity date outside horizon')
        capacity = integer(row['capacity'])
        key = (workcenter, when)
        if key in seen:
            raise ValueError('duplicate capacity row')
        seen.add(key)
        calendar[key] = capacity
    return calendar


def _top_fields(data):
    """Require ``as_of items demands receipts`` plus optional ``capacities``."""
    if not isinstance(data, dict):
        raise ValueError('expected fields: as_of items demands receipts')
    present = set(data)
    required = {'as_of', 'items', 'demands', 'receipts'}
    optional = {'capacities'}
    if not required <= present or present - required - optional:
        raise ValueError('expected fields: as_of items demands receipts '
                         '[capacities]')


def _levels(order, items):
    """Assign each sku its BOM depth (parents strictly before children)."""
    depth = {sku: 0 for sku in order}
    for sku in order:
        for child in items[sku]['components']:
            depth[child] = max(depth[child], depth[sku] + 1)
    grouped = defaultdict(list)
    for sku in order:
        grouped[depth[sku]].append(sku)
    return [grouped[level] for level in sorted(grouped)]


def _allocate(quantity, multiple, per_unit, workcenter, as_of, latest, calendar):
    """Distribute ``quantity`` over the release window, packing late days first.

    Every batch quantity is a positive multiple of ``multiple`` and all batches
    together equal ``quantity``.  Capacity is debited as it is committed so a
    workcenter shared by several skus is never double-booked.  Filling from the
    latest admissible release backwards leaves room for other skus (and for
    capacity-constrained components) with earlier deadlines.
    """
    if latest < as_of:
        raise ValueError('no release date on or after as_of')
    unit = multiple * per_unit
    remaining = quantity
    batches = []
    cursor = latest
    while remaining > 0 and cursor >= as_of:
        available = calendar.get((workcenter, cursor), 0)
        whole = (available // unit) * multiple
        if whole > 0:
            take = min(whole, remaining)
            calendar[(workcenter, cursor)] = available - take * per_unit
            batches.append((cursor, take))
            remaining -= take
        cursor -= timedelta(days=1)
    if remaining > 0:
        raise ValueError('infeasible capacity for ' + workcenter)
    return batches


def plan(data):
    _top_fields(data)
    as_of = day(data['as_of'])
    items, order = compile_items(data['items'])
    calendar = _capacity_calendar(data.get('capacities', []), as_of)
    gross = {sku: defaultdict(int) for sku in items}
    supply = {sku: defaultdict(int) for sku in items}
    for key, target in [('demands', gross), ('receipts', supply)]:
        seen = set()
        for row in array(data[key]):
            fields(row, 'id sku date quantity')
            rid, sku, when = text(row['id']), text(row['sku']), day(row['date'])
            if rid in seen or sku not in items or when < as_of or (when - as_of).days > HORIZON_DAYS:
                raise ValueError('duplicate id, unknown sku or date outside horizon')
            seen.add(rid)
            target[sku][when] += integer(row['quantity'], 1)
    ledger, orders = [], []
    for level in _levels(order, items):
        # Net every sku in this level first; all parents were planned in
        # earlier levels, so their dependent gross demand is already complete.
        jobs = []
        for sku in level:
            item = items[sku]
            balance = item['on_hand']
            for when in sorted(gross[sku].keys() | supply[sku].keys()):
                opening = balance
                need, arriving = gross[sku][when], supply[sku][when]
                short = max(0, need - balance - arriving)
                planned = ((short + item['multiple'] - 1) // item['multiple']) * item['multiple']
                balance += arriving + planned - need
                ledger.append({'sku': sku, 'date': when.isoformat(), 'opening': opening,
                               'gross': need, 'scheduled_receipt': arriving,
                               'planned_receipt': planned, 'closing': balance})
                if planned:
                    jobs.append((when - timedelta(days=item['lead']), sku, item,
                                 when, planned))
        # Skus in one level are independent but may share a workcenter.  Allocate
        # earliest-deadline-first so tight windows are protected; `_allocate`
        # then packs each job latest-date-first and debits the shared calendar.
        jobs.sort(key=lambda job: (job[0], job[1], job[3]))
        for release, sku, item, when, planned in jobs:
            constrained = (item['workcenter'] is not None
                           and bool(item['components']) and release >= as_of)
            if constrained:
                batches = _allocate(planned, item['multiple'],
                                    item['capacity_per_unit'], item['workcenter'],
                                    as_of, release, calendar)
            else:
                # Unconstrained items and already-overdue batches keep the
                # historical single-order past_due semantics and never touch
                # the future capacity calendar.
                batches = [(release, planned)]
            kind = 'make' if item['components'] else 'buy'
            for batch_release, quantity in batches:
                receipt = batch_release + timedelta(days=item['lead'])
                orders.append({'sku': sku, 'receipt_date': receipt.isoformat(),
                               'release_date': batch_release.isoformat(),
                               'quantity': quantity, 'past_due': batch_release < as_of,
                               'kind': kind})
                for child, per_unit in item['components'].items():
                    gross[child][batch_release] += quantity * per_unit
    return {'orders': sorted(orders, key=lambda r: (r['release_date'], r['sku'],
                                                    r['receipt_date'], r['quantity'])),
            'ledger': sorted(ledger, key=lambda r: (r['sku'], r['date'])),
            'ending_inventory': {sku: next((r['closing'] for r in reversed(ledger) if r['sku'] == sku), items[sku]['on_hand']) for sku in sorted(items)}}
