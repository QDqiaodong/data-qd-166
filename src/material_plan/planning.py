"""Time-phased supply netting with planned-release-driven dependent demand.

Material netting determines *how much* each sku must be replenished per
requirement bucket.  Capacity-constrained manufactured items are then scheduled
into a shared per-workcenter daily capacity calendar: a shortage may be split
into several production batches whose release dates differ, each completing no
later than the requirement bucket.  Component demand is exploded at each
batch's *actual* release date.

Optionally a sku declares physical inventory by quality/release window
(``inventory_lots``) and a receipt may itself be one lot.  Such a sku is netted
in *batch mode*: a lot is consumable only on dates inside
``[available_from, expires_on]``, is consumed FEFO (earliest expiry, then
``lot_id``), and any remainder is written off on the day after ``expires_on``.
Unreleased or already expired quantities therefore never mask a shortage.
Skus without any lot declaration keep the historical unlimited-availability
netting and ledger shape.

BOM levels are planned parent-first.  Within one level the skus are
independent, so their constrained shortages are allocated to the shared
calendar in earliest-deadline-first order with a latest-date-first packing
policy; this keeps early days available for shorter windows and never
double-books a workcenter.
"""
from collections import defaultdict
from datetime import timedelta
from .common import fields, text, integer, array, day
from .bom import compile_items

HORIZON_DAYS = 3650


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


def _parse_flows(data, items, as_of):
    """Validate demands/receipts and split supply into legacy and lot receipts.

    Returns ``gross``, ``supply`` (legacy receipts keyed by date) and
    ``receipt_lots`` (declared-window receipts, one whole batch each).
    """
    gross = {sku: defaultdict(int) for sku in items}
    supply = {sku: defaultdict(int) for sku in items}
    receipt_lots = {sku: [] for sku in items}
    lot_ids = set()
    for item in items.values():
        for lot in item['lots'] or ():
            if lot['lot_id'] in lot_ids:
                raise ValueError('duplicate lot_id')
            lot_ids.add(lot['lot_id'])
    for key, target in [('demands', gross), ('receipts', supply)]:
        seen = set()
        for row in array(data[key]):
            if not isinstance(row, dict):
                raise ValueError('expected fields: id sku date quantity')
            present = set(row)
            if key == 'receipts' and present == {'id', 'sku', 'date', 'quantity',
                                                 'lot_id', 'available_from',
                                                 'expires_on'}:
                pass
            elif key == 'receipts' and present & {'lot_id', 'available_from',
                                                  'expires_on'}:
                raise ValueError('expected receipt fields: id sku date quantity '
                                 '[lot_id available_from expires_on]')
            else:
                fields(row, 'id sku date quantity')
            rid, sku, when = text(row['id']), text(row['sku']), day(row['date'])
            if rid in seen or sku not in items or when < as_of or (when - as_of).days > HORIZON_DAYS:
                raise ValueError('duplicate id, unknown sku or date outside horizon')
            seen.add(rid)
            quantity = integer(row['quantity'], 1)
            if key == 'receipts' and 'lot_id' in present:
                lot_id = text(row['lot_id'])
                if lot_id in lot_ids:
                    raise ValueError('duplicate lot_id')
                lot_ids.add(lot_id)
                available_from = day(row['available_from'])
                expires_on = day(row['expires_on'])
                if available_from < when:
                    raise ValueError('lot available before receipt date')
                if expires_on < available_from:
                    raise ValueError('lot expires before available')
                receipt_lots[sku].append({'lot_id': lot_id, 'quantity': quantity,
                                          'available_from': available_from,
                                          'expires_on': expires_on})
            else:
                target[sku][when] += quantity
    return gross, supply, receipt_lots


def _plan_legacy(sku, item, gross, supply):
    """Historical unlimited-availability netting; row shape is unchanged."""
    balance = item['on_hand']
    multiple = item['multiple']
    rows, jobs = [], []
    for when in sorted(gross.keys() | supply.keys()):
        opening = balance
        need, arriving = gross[when], supply[when]
        short = max(0, need - balance - arriving)
        planned = ((short + multiple - 1) // multiple) * multiple
        balance += arriving + planned - need
        rows.append({'sku': sku, 'date': when.isoformat(), 'opening': opening,
                     'gross': need, 'scheduled_receipt': arriving,
                     'planned_receipt': planned, 'closing': balance})
        if planned:
            jobs.append((when - timedelta(days=item['lead']), sku, item,
                         when, planned))
    return rows, jobs, balance


def _plan_batched(sku, item, gross, supply, receipt_lots, as_of):
    """Net a lot-tracked sku and emit expiry and per-lot consumption evidence.

    A stock lot already released at the earliest planning date is opening
    inventory; every other lot enters available inventory on its
    ``available_from`` as a release event (booked as ``scheduled_receipt``).
    A lot stays consumable through ``expires_on`` inclusive and its remainder
    is written off on the following day, producing a ledger row even when no
    demand falls there.  Unreleased and expired quantities never cover demand.
    """
    multiple = item['multiple']
    earliest = min(set(gross) | set(supply) | {as_of})
    horizon_end = as_of + timedelta(days=HORIZON_DAYS)
    generic = item['on_hand'] if item['lots'] is None else 0
    released, unreleased, dates = [], [], set(gross) | set(supply) | {as_of}
    # Receipt lots always release on their receipt/availability day; stock lots
    # released at or before the earliest bucket are simply opening stock.
    for raw, is_stock in ([(lot, True) for lot in (item['lots'] or ())]
                          + [(lot, False) for lot in receipt_lots]):
        lot = {'lot_id': raw['lot_id'], 'remaining': raw['quantity'],
               'available_from': raw['available_from'],
               'expires_on': raw['expires_on']}
        if is_stock and lot['available_from'] <= earliest:
            released.append(lot)
        elif lot['available_from'] <= horizon_end:
            unreleased.append(lot)
            dates.add(lot['available_from'])
        else:
            continue  # only released after the planning horizon
        # A remainder dies on the day after ``expires_on``; a lot already void
        # before the first bucket is written off on that bucket.
        expiry = max(lot['expires_on'] + timedelta(days=1), earliest)
        if expiry <= horizon_end:
            dates.add(expiry)
    rows, jobs = [], []
    final = generic + sum(lot['remaining'] for lot in released)
    for when in sorted(dates):
        opening = generic + sum(lot['remaining'] for lot in released)
        scheduled = supply[when]
        pending = []
        for lot in unreleased:
            if lot['available_from'] == when:
                released.append(lot)
                scheduled += lot['remaining']
            else:
                pending.append(lot)
        unreleased = pending
        expired, survivors = 0, []
        for lot in released:
            if lot['expires_on'] < when:
                expired += lot['remaining']
                lot['remaining'] = 0
            else:
                survivors.append(lot)
        released = survivors
        available = opening - expired + scheduled
        need = gross[when]
        short = max(0, need - available)
        planned = ((short + multiple - 1) // multiple) * multiple
        consumption, outstanding = [], need
        for lot in sorted(released, key=lambda row: (row['expires_on'],
                                                     row['lot_id'])):
            if outstanding == 0:
                break
            take = min(lot['remaining'], outstanding)
            if take:
                lot['remaining'] -= take
                outstanding -= take
                consumption.append({'lot_id': lot['lot_id'], 'quantity': take})
        # Demand not covered by dated lots draws on unlimited-availability
        # supply (legacy stock/receipts plus this sku's own planned receipt).
        closing = opening - expired + scheduled + planned - need
        generic = closing - sum(lot['remaining'] for lot in released)
        final = closing
        rows.append({'sku': sku, 'date': when.isoformat(), 'opening': opening,
                     'gross': need, 'scheduled_receipt': scheduled,
                     'planned_receipt': planned, 'closing': closing,
                     'expired': expired, 'lot_consumption': consumption})
        if planned:
            jobs.append((when - timedelta(days=item['lead']), sku, item,
                         when, planned))
    rows = [row for row in rows if row['gross'] or row['scheduled_receipt']
            or row['planned_receipt'] or row['expired']]
    return rows, jobs, final


def plan(data):
    _top_fields(data)
    as_of = day(data['as_of'])
    items, order = compile_items(data['items'])
    calendar = _capacity_calendar(data.get('capacities', []), as_of)
    gross, supply, receipt_lots = _parse_flows(data, items, as_of)
    ledger, orders, ending = [], [], {}
    for level in _levels(order, items):
        # Net every sku in this level first; all parents were planned in
        # earlier levels, so their dependent gross demand is already complete.
        jobs = []
        for sku in level:
            item = items[sku]
            if item['lots'] is not None or receipt_lots[sku]:
                rows, new_jobs, balance = _plan_batched(
                    sku, item, gross[sku], supply[sku], receipt_lots[sku], as_of)
            else:
                rows, new_jobs, balance = _plan_legacy(
                    sku, item, gross[sku], supply[sku])
            ledger.extend(rows)
            jobs.extend(new_jobs)
            ending[sku] = balance
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
            'ending_inventory': {sku: ending[sku] for sku in sorted(items)}}
