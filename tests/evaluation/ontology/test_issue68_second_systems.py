"""Second applicable systems added for Issue #68: no gold leakage, deterministic output."""
import json
from pathlib import Path

from plantkg.evaluation.ontology import brick_embedding, bts_llm, maintie_lexicon
from plantkg.evaluation.ontology.evaluation import stable_split
from plantkg.evaluation.ontology.real_data import BRICK


def _row(index, token, label):
    return {'text': token, 'tokens': [token],
            'entities': [{'start': 0, 'end': 1, 'type': label}], 'relations': []}


def test_lexicon_learns_only_from_train_rows():
    rows = []
    for index in range(200):
        rows.append(_row(index, f'tok{index}', 'PhysicalObject'))
    train = next(i for i in range(200) if stable_split(i, 42) == 'train')
    test = next(i for i in range(200) if stable_split(i, 42) == 'test')
    predictions, phrases, _ = maintie_lexicon.generate(rows, 42, 'test')
    assert ('tok' + str(test),) not in phrases
    assert predictions[str(test)]['entities'] == []
    renamed = [row for row in rows]
    renamed[test] = _row(test, f'tok{test}', 'Activity')
    again, _, _ = maintie_lexicon.generate(renamed, 42, 'test')
    assert again == predictions
    assert phrases[('tok' + str(train),)]['PhysicalObject'] == 1


def test_lexicon_weights_are_independent_signals():
    rows = [_row(index, 'pump', 'PhysicalObject') for index in range(200)]
    predictions, _, _ = maintie_lexicon.generate(rows, 42, 'test')
    item = predictions[next(iter(predictions))]
    assert item['uncertainty'] == 0
    assert item['impact'] == 1


def test_bts_features_hide_types_and_relation_edges(tmp_path):
    source = tmp_path / 'site.ttl'
    source.write_text(
        f'@prefix brick: <{BRICK}> . @prefix senaps: <urn:senaps:> . '
        '@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> . '
        '<urn:site#a.b> rdf:type brick:Sensor ; brick:isPointOf <urn:site#b> ; '
        'brick:isFedBy <urn:site#c> ; brick:hasUnit "DEG_C" ; senaps:stream_id "abc" .',
        encoding='utf8')
    subjects, predicates, literals = bts_llm.features(source)
    # Only named subjects are observed; objects of withheld edges are not collected.
    assert subjects == {'urn:site#a.b'}
    assert predicates['urn:site#a.b'] == {'hasUnit', 'stream_id'}
    assert literals['urn:site#a.b'] == {'DEG_C', 'abc'}
    prompt = bts_llm.prompt_concepts(['Point', 'Sensor'])
    assert 'CLASSES' in prompt and 'isPointOf' not in prompt and 'isFedBy' not in prompt
    assert 'hasLocation' in bts_llm.prompt_relations()


def test_bts_review_curves_reach_gold_at_full_budget():
    rows = [{'id': 'a', 'gold': {'entities': [('a', 'Sensor')], 'relations': [('a', 'feeds', 'b')]},
             'pred': {'entities': [], 'relations': []}, 'uncertainty': 0.5, 'impact': 1},
            {'id': 'b', 'gold': {'entities': [('b', 'Point')], 'relations': []},
             'pred': {'entities': [('b', 'Point')], 'relations': []}, 'uncertainty': 0.1, 'impact': 2}]
    curves = bts_llm.review_curves(rows, 42)
    assert set(curves) == {'random', 'uncertainty', 'uncertainty_x_impact'}
    for points in curves.values():
        assert [point['reviewed'] for point in points] == [0, 1, 2]
        assert points[0]['entity_f1'] < 1
        assert points[-1] == {'reviewed': 2, 'entity_f1': 1.0, 'relation_f1': 1.0}


def test_brick_embedding_ignores_replacement_assertions(tmp_path, monkeypatch):
    old = tmp_path / 'old.ttl'
    new = tmp_path / 'new.ttl'
    old.write_text(f'<{BRICK}Old_Pump> a <http://www.w3.org/2002/07/owl#Class> .', encoding='utf8')
    content = (f'@prefix brick: <{BRICK}> . @prefix owl: <http://www.w3.org/2002/07/owl#> . '
               'brick:Old_Pump a owl:Class ; owl:deprecated true ; brick:isReplacedBy brick:TARGET . '
               'brick:Pump a owl:Class . brick:Other a owl:Class .')
    vectors = {'old pump': [1.0, 0.0], 'pump': [1.0, 0.0], 'other': [0.0, 1.0]}
    monkeypatch.setattr(brick_embedding, '_digest', lambda model: {'digest': 'stub'})
    monkeypatch.setattr(brick_embedding, 'embed',
                        lambda model, texts, batch=32: [vectors.get(text, [0.0, 1.0]) for text in texts])
    new.write_text(content.replace('TARGET', 'Other'), encoding='utf8')
    first, _ = brick_embedding.generate(old, new, 'stub')
    assert first[f'{BRICK}Old_Pump'] == f'{BRICK}Pump'
    new.write_text(content.replace('TARGET', 'Pump'), encoding='utf8')
    assert brick_embedding.generate(old, new, 'stub')[0] == first


def test_frozen_second_system_evidence_matches_documented_hashes():
    from plantkg.core.hashing import hash_file
    root = Path(__file__).resolve().parents[3] / 'plantkg/evaluation/ontology/real_results'
    predictions = json.loads((root / 'maintie-lexicon-issue68/predictions.json').read_text(encoding='utf8'))
    assert len(predictions) == 216
    metadata = json.loads((root / 'maintie-lexicon-issue68/metadata.json').read_text(encoding='utf8'))
    assert hash_file(root / 'maintie-lexicon-issue68/predictions.json') == metadata['prediction_sha256']
    assert hash_file(root / 'brick-embedding-issue68.json') == (
        '2f0f517fff23f33ca1ccd095c1594bdc60c4dd9acedbc3b440d6b1b7d72e8715')
    bts = json.loads((root / 'bts-llm-issue68/results.json').read_text(encoding='utf8'))
    assert hash_file(root / 'bts-llm-issue68/concept.nt') == bts['concept_prediction_sha256']
    assert hash_file(root / 'bts-llm-issue68/relations.nt') == bts['relation_prediction_sha256']
    assert bts['failed_count'] == 0 and bts['sample']['subjects'] == 150
