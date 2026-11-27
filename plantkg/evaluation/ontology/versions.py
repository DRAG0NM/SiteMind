"""Version and measured batch summaries, using explicitly supplied references."""


def migration(expected, actual):
    """Mappings keyed by deprecated term; no map means unresolved, not success."""
    correct = sorted(k for k, v in expected.items() if actual.get(k) == v)
    missing = sorted(k for k in expected if k not in actual)
    wrong = sorted(k for k, v in expected.items() if k in actual and actual[k] != v)
    return {'correct': correct, 'missing': missing, 'wrong': wrong,
            'accuracy': len(correct) / len(expected) if expected else None}


def affected_rules(expected, actual):
    expected, actual = set(expected), set(actual)
    return {'identified': sorted(expected & actual), 'missed': sorted(expected - actual),
            'extra': sorted(actual - expected),
            'recall': len(expected & actual) / len(expected) if expected else None}


def batches(records):
    """B30 batch exports; accuracy is unknown unless separately measured."""
    return [{'batch_id': x['batch_id'], 'items': x['items'], 'agent_fraction': x['agent_fraction'],
             'template_fraction': 1 - x['agent_fraction'],
             'review_minutes_per_100': x['review_minutes_per_100'], 'tokens': x['tokens'],
             'elapsed_seconds': x['elapsed_seconds'], 'accuracy': x.get('accuracy')}
            for x in records]
