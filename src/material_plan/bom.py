"""Validate a bounded acyclic bill of materials and parent-first traversal."""
from .common import fields, text, integer, array


def compile_items(raw):
    items = {}
    for row in array(raw, 100):
        fields(row, 'sku lead_days lot_multiple on_hand components')
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
        items[sku] = {'lead': integer(row['lead_days'], 0, 365),
                      'multiple': integer(row['lot_multiple'], 1, 10**6),
                      'on_hand': integer(row['on_hand']), 'components': components}
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
