"""Summarize explicitly recorded construction-study measurements; never infer missing costs."""
import argparse
import json
import math
import re
from pathlib import Path

ROUTES = ('human', 'llm', 'plantkg')
MEASURES = ('human_minutes', 'wall_seconds', 'tokens', 'api_fee', 'accuracy')


def summarize(records):
    if not isinstance(records, list):
        raise ValueError('records must be an array')
    result = []
    for i, row in enumerate(records):
        if not isinstance(row, dict) or row.get('route') not in ROUTES:
            raise ValueError(f'record {i}: invalid route')
        if not row.get('batch_id') or not isinstance(row.get('source_sha256'), str) or not re.fullmatch(r'[0-9a-fA-F]{64}', row['source_sha256']):
            raise ValueError(f'record {i}: batch_id and a SHA-256 source_sha256 are required')
        for field in MEASURES:
            value = row.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError(f'record {i}: {field} must be non-negative and finite or null')
        if row.get('accuracy') is not None and row['accuracy'] > 1:
            raise ValueError(f'record {i}: accuracy must be between 0 and 1')
        if row.get('api_fee') is not None and not row.get('fee_evidence'):
            raise ValueError(f'record {i}: measured API fee requires fee_evidence')
        result.append({**row, **{field: row.get(field) for field in MEASURES}})
    return result


def totals(records):
    """Sum measured quantities only when every record for a route supplies them."""
    result = {}
    for route in ROUTES:
        rows = [row for row in records if row['route'] == route]
        if rows:
            result[route] = {'record_count': len(rows)}
            for field in ('human_minutes', 'wall_seconds', 'tokens', 'api_fee'):
                values = [row.get(field) for row in rows]
                result[route][field] = sum(values) if all(value is not None for value in values) else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='JSON array of independently measured records')
    parser.add_argument('--output', required=True, help='New output JSON file')
    args = parser.parse_args()
    rows = summarize(json.loads(Path(args.input).read_text(encoding='utf8')))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8', newline='\n') as f:
        json.dump({'records': rows, 'totals_by_route': totals(rows),
                   'note': 'Missing measurements are null; records are not proof of equivalent conditions.'}, f, indent=2)
        f.write('\n')


if __name__ == '__main__':
    main()
