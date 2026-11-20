"""Validate a bounded acyclic bill of materials and parent-first traversal."""
from .common import fields, text, integer, array


_BASE_ITEM_FIELDS = {'sku', 'lead_days', 'lot_multiple', 'on_hand', 'components'}
_CAPACITY_ITEM_FIELDS = {'workcenter', 'capacity_per_unit'}


def compile_items(raw):
    items = {}
    for row in array(raw, 100):
        if not isinstance(row, dict):
            raise ValueError('item row must be an object')
        present = set(row)
        has_workcenter = 'workcenter' in present
        has_per_unit = 'capacity_per_unit' in present
        if (not _BASE_ITEM_FIELDS <= present
                or present - _BASE_ITEM_FIELDS - _CAPACITY_ITEM_FIELDS
                or has_workcenter != has_per_unit):
            raise ValueError('expected item fields: sku lead_days lot_multiple '
                             'on_hand components [workcenter capacity_per_unit]')
        sku = text(row['sku'])
        if sku in items:
            raise ValueError('duplicate sku')
        components = {}
        for component in array(row['components'], 100):
            fields(component, 'sku quantity')
            child = text(component['sku'])
            if child in components:
                raise ValueError('duplicate component')
            components[child] = integer(component['quantity'], 1, 10**6)
        if has_workcenter:
            text(row['workcenter'])
            integer(row['capacity_per_unit'], 1, 10**9)
        items[sku] = {'lead': integer(row['lead_days'], 0, 365),
                      'multiple': integer(row['lot_multiple'], 1, 10**6),
                      'on_hand': integer(row['on_hand']), 'components': components,
                      'workcenter': row['workcenter'] if has_workcenter else None,
                      'capacity_per_unit': (row['capacity_per_unit']
                                            if has_per_unit else None)}
    if not items:
        raise ValueError('empty item master')
    indegree = dict.fromkeys(items, 0)
    for row in items.values():
        for child in row['components']:
            if child not in items:
                raise ValueError('unknown component')
            indegree[child] += 1
    ready = sorted(sku for sku in items if indegree[sku] == 0)
    order = []
    while ready:
        sku = ready.pop(0)
        order.append(sku)
        for child in sorted(items[sku]['components']):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(order) != len(items):
        raise ValueError('BOM cycle')
    return items, order
