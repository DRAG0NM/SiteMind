"""Probe B30's registered RuleExtractor with MaintIE text, without using labels."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from plantkg.core.contracts import AssetFragment, Context, Locator
from plantkg.core.hashing import hash_file
from plantkg.core.ports import RuleExtractor
from plantkg.core.registry import Registry
from .evaluation import stable_split, factory_match


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('gold'); p.add_argument('--split', choices=['train','validation','test'], default='test')
    p.add_argument('--seed', type=int, default=42); p.add_argument('--output', required=True)
    p.add_argument('--overwrite', action='store_true', help='允许覆盖已存在的输出文件')
    a = p.parse_args()
    out = Path(a.output)
    if out.exists() and not a.overwrite:
        p.error(f"输出文件已存在，拒绝覆盖（加 --overwrite 覆盖）：{out}")
    source = Path(a.gold); texts = json.loads(source.read_text(encoding='utf8'))
    registry = Registry()
    registry.load([Path(__file__).resolve().parents[2] / 'packs' / 'ontology_rules'], ['ontology_rules'])
    extractor = registry.resolve(RuleExtractor)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ctx = Context(plant_id='factory', industry_pack='factory', actor=None, channel='offline', trace_id='b48-maintie', as_of=at)
    outputs = []
    start = time.perf_counter()
    for i, row in enumerate(texts):
        if stable_split(i, a.seed) != a.split:
            continue
        fragment = AssetFragment(fragment_id=f'maintie-{i}', asset_version_id=f'asset://factory/12345678/maintie/{i}',
                                 locator=Locator(line_start=1), kind='text', text=row['text'], available_at=at)
        candidates = extractor.extract(ctx, [fragment])
        outputs.append({'id': i, 'factory_subset': factory_match(row['text']), 'candidate_count': len(candidates),
                        'kinds': [c.draft.get('kind') for c in candidates]})
    elapsed = time.perf_counter() - start
    result = {'source_sha256': hash_file(source), 'split': a.split, 'seed': a.seed,
              'items': len(outputs), 'candidates': sum(x['candidate_count'] for x in outputs),
              'factory_items': sum(x['factory_subset'] for x in outputs),
              'factory_candidates': sum(x['candidate_count'] for x in outputs if x['factory_subset']),
              'elapsed_seconds_measured': elapsed, 'token_usage': None, 'human_minutes': None,
              'output_schema': 'RuleCandidate, not MaintIE entity spans or relations', 'records': outputs}
    out.parent.mkdir(parents=True, exist_ok=True)  # #154：输出文件的父目录自动创建
    temp = out.with_name(out.name + '.tmp')
    with temp.open('w', encoding='utf8', newline='\n') as f: json.dump(result, f, indent=2)
    os.replace(temp, out)


if __name__ == '__main__':
    main()
