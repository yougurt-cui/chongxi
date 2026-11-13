"""Lossless formatting repairs before the existing health evidence validation."""
from copy import deepcopy


def compact_health_details(groups):
    """Pack short list items into <=3 lines, without dropping facts or sources.

    Models sometimes merge causes correctly but return four short symptoms. Keep
    every symptom while fitting the card format. Invalid or oversized material is
    deliberately left for the caller's strict validation, never silently removed.
    """
    result = deepcopy(groups)
    if not isinstance(result, list):
        return result
    for group in result:
        if not isinstance(group, dict):
            continue
        for key in ('manifestations', 'supporting'):
            items = group.get(key)
            if (not isinstance(items, list) or len(items) <= 3
                    or any(not isinstance(item, str) or not 1 <= len(item) <= 100 for item in items)):
                continue
            packed = []
            for item in items:
                if packed and len(packed[-1]) + len(item) + 1 <= 100:
                    packed[-1] += '；' + item
                else:
                    packed.append(item)
            if len(packed) <= 3:
                group[key] = packed
    return result
