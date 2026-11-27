import json
from plantkg.evaluation.ontology import evaluate_ontology, maintie_records, stable_split
from plantkg.evaluation.ontology.versions import migration, affected_rules, batches


def test_b48_smoke():
    path = 'fixtures/ontology/synthetic.json'
    a = evaluate_ontology(path, 42)
    assert a == evaluate_ontology(path, 42)
    assert len(a['review_curves']) == 3
    assert all(points[-1]['entity_f1'] == 1 for points in a['review_curves'].values())
    gold = json.load(open('fixtures/ontology/maintie/sample.json', encoding='utf8'))
    preds = {str(i): {'entities': [], 'relations': [], 'uncertainty': 1, 'impact': 1} for i in range(len(gold))}
    assert maintie_records(gold, preds, 42, 'test', True) == maintie_records(gold, preds, 42, 'test', True)
    assert stable_split('1', 42) == stable_split('1', 42)
    assert migration({'old': 'new'}, {'old': 'new'})['accuracy'] == 1
    assert affected_rules(['a', 'b'], ['b'])['recall'] == .5
    assert batches([{'batch_id': 'a', 'items': 1, 'agent_fraction': 1, 'review_minutes_per_100': 3, 'tokens': 2, 'elapsed_seconds': 1}])[0]['accuracy'] is None


def test_rdf_scoring_and_migration(tmp_path):
    from plantkg.evaluation.ontology.real_data import BRICK, score_bts, migration_result
    from plantkg.evaluation.ontology.bts_baseline import run
    gold = tmp_path / 'gold.ttl'
    gold.write_text(f'''@prefix brick: <{BRICK}> .
        <urn:building#parent> a brick:AHU .
        <urn:building#parent.middle.point> a brick:Sensor ; brick:isPointOf <urn:building#parent> .
        <urn:building#parent> brick:feeds <urn:building#other> .
        ''', encoding='utf8')
    result, predicted = run(gold, 42)
    pred = tmp_path / 'pred.nt'; pred.write_text(predicted, encoding='utf8')
    scored = score_bts(gold, pred)
    assert result['relations']['tp'] == scored['relations']['tp'] == 1
    assert scored['relation_by_type']['feeds']['fn'] == 1
    assert all(curve[-1]['relation_f1'] == 1 for curve in result['review_curves'].values())
    old = tmp_path / 'old.ttl'; new = tmp_path / 'new.ttl'
    old.write_text(f'<{BRICK}Old> a <http://www.w3.org/2002/07/owl#Class> .', encoding='utf8')
    new.write_text(f'''@prefix brick: <{BRICK}> . @prefix owl: <http://www.w3.org/2002/07/owl#> .
        brick:Old owl:deprecated true ; brick:isReplacedBy brick:New .''', encoding='utf8')
    assert migration_result(old, new, {BRICK+'Old': BRICK+'New'})['accuracy'] == 1
    assert migration_result(old, new, {})['missing'] == [BRICK+'Old']
