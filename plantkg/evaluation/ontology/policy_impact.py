"""Score independently annotated changed-policy impacts, or report pending annotation."""
import argparse
import json
from pathlib import Path

from .evaluation import metrics


def evaluate(annotations, predictions):
    if not isinstance(annotations, list) or not isinstance(predictions, list):
        raise ValueError('annotations and predictions must be arrays')
    if not annotations:
        return {'status': 'pending_annotation', 'recall': None,
                'reason': 'no independently annotated policy changes'}
    gold, predicted, changes, predicted_changes = set(), set(), set(), set()
    for row in annotations:
        if not row.get('change_id') or not row.get('annotator') or not isinstance(row.get('affected_rule_ids'), list):
            raise ValueError('each annotation requires change_id, annotator and affected_rule_ids')
        change = row['change_id']
        if change in changes:
            raise ValueError(f'duplicate annotation for {change}')
        changes.add(change)
        if not all(isinstance(rule, str) and rule for rule in row['affected_rule_ids']):
            raise ValueError('affected rule identifiers must be non-empty strings')
        gold.update((change, rule) for rule in row['affected_rule_ids'])
    for row in predictions:
        if row.get('change_id') not in changes or not isinstance(row.get('affected_rule_ids'), list):
            raise ValueError('prediction requires an annotated change_id and affected_rule_ids')
        if row['change_id'] in predicted_changes:
            raise ValueError('duplicate prediction for change')
        if not all(isinstance(rule, str) and rule for rule in row['affected_rule_ids']):
            raise ValueError('affected rule identifiers must be non-empty strings')
        predicted_changes.add(row['change_id'])
        predicted.update((row['change_id'], rule) for rule in row['affected_rule_ids'])
    if predicted_changes != changes:
        raise ValueError('every annotated change requires an explicit prediction')
    score = metrics(gold, predicted)
    if not gold:
        score['recall'] = None
        score['reason'] = 'no annotated affected rules; recall denominator is zero'
    return {'status': 'scored', 'sample_count': len(changes), **score}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('annotations'); p.add_argument('predictions'); p.add_argument('--output', required=True)
    args = p.parse_args()
    result = evaluate(json.loads(Path(args.annotations).read_text(encoding='utf8')),
                      json.loads(Path(args.predictions).read_text(encoding='utf8')))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8', newline='\n') as f:
        json.dump(result, f, indent=2)
        f.write('\n')


if __name__ == '__main__':
    main()
