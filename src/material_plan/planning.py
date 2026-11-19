"""Time-phased supply netting with planned-release-driven dependent demand."""
from collections import defaultdict
from datetime import date, timedelta
from .common import fields, text, integer, array
from .bom import compile_items


def day(value):
    text(value)
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('ISO date required')
    return result


def plan(data):
    fields(data, 'as_of items demands receipts')
    as_of = day(data['as_of'])
    items, order = compile_items(data['items'])
    gross = {sku: defaultdict(int) for sku in items}
    supply = {sku: defaultdict(int) for sku in items}
    for key, target in [('demands', gross), ('receipts', supply)]:
        seen = set()
        for row in array(data[key]):
            fields(row, 'id sku date quantity')
            rid, sku, when = text(row['id']), text(row['sku']), day(row['date'])
            if rid in seen or sku not in items or when < as_of or (when - as_of).days > 3650:
                raise ValueError('duplicate id, unknown sku or date outside horizon')
            seen.add(rid)
            target[sku][when] += integer(row['quantity'], 1)
    ledger, orders = [], []
    for sku in order:
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
                release = when - timedelta(days=item['lead'])
                orders.append({'sku': sku, 'receipt_date': when.isoformat(), 'release_date': release.isoformat(),
                               'quantity': planned, 'past_due': release < as_of,
                               'kind': 'make' if item['components'] else 'buy'})
                for child, quantity in item['components'].items():
                    gross[child][release] += planned * quantity
    return {'orders': sorted(orders, key=lambda r: (r['release_date'], r['sku'])),
            'ledger': sorted(ledger, key=lambda r: (r['sku'], r['date'])),
            'ending_inventory': {sku: next((r['closing'] for r in reversed(ledger) if r['sku'] == sku), items[sku]['on_hand']) for sku in sorted(items)}}
