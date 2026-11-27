import json
from pathlib import Path

import pytest
from jsonschema import ValidationError, validate

from plantkg.evaluation.ontology.batch_report import summarize
from plantkg.evaluation.ontology.evaluation import (ENTITY_LABELS, RELATION_LABELS,
                                                    maintie_records, stable_split,
                                                    validate_maintie_prediction)
from plantkg.evaluation.ontology.maintie_local import prompt_for
from plantkg.evaluation.ontology.maintie_repair import relation_schema
from plantkg.evaluation.ontology.study import summarize as study_records, totals as study_totals
from plantkg.evaluation.ontology.policy_impact import evaluate as policy_impact
from plantkg.evaluation.ontology.brick_lexical import generate as brick_lexical
from plantkg.evaluation.ontology.real_data import BRICK


def test_prediction_validation_and_document_denominator():
    index = next(i for i in range(100) if stable_split(i, 42) == 'test')
    gold = [{'text': 'pump failed', 'tokens': ['pump', 'failed'], 'entities': [], 'relations': []}
            for _ in range(index + 1)]
    prediction = {'entities': [{'start': 0, 'end': 1, 'type': 'PhysicalObject'}],
                  'relations': [], 'uncertainty': 1, 'impact': 1}
    rows = maintie_records(gold, {str(index): prediction}, 42, 'test')
    assert rows[0]['pred']['entities'] == [(0, 1, 'PhysicalObject')]
    # Validation uses the token count and public schema, not entity or relation gold.
    gold[index]['entities'] = [{'start': 1, 'end': 2, 'type': 'State'}]
    assert maintie_records(gold, {str(index): prediction}, 42, 'test')[0]['pred'] == rows[0]['pred']
    prediction['entities'][0]['end'] = 3
    with pytest.raises(ValueError, match='span'):
        maintie_records(gold, {str(index): prediction}, 42, 'test')

    base = {'batch_id': 'a', 'items': 50, 'agent_fraction': .5,
            'review_minutes_per_100': 20, 'tokens': 0, 'elapsed_seconds': 1}
    report = summarize([{**base, 'document_count': 10, 'review_minutes': 20},
                        {**base, 'batch_id': 'b'}])
    assert report[0]['review_minutes_per_100_documents'] == 200
    assert report[1]['review_minutes_per_100_documents'] is None
    assert report[1]['cumulative_document_count'] is None
    assert report[1]['cost'] is None and report[1]['accuracy'] is None


def test_local_prompt_receives_only_tokens():
    assert prompt_for(['pump', 'failed']) == prompt_for(['pump', 'failed'])
    assert 'gold' not in prompt_for(['pump', 'failed']).lower()
    schema = relation_schema(2)
    validate({'relations': [{'head': 0, 'tail': 1, 'type': 'hasPart'}]}, schema)
    with pytest.raises(ValidationError):
        validate({'relations': [{'head': 0, 'tail': 2, 'type': 'hasPart'}]}, schema)


def test_valid_classes_and_bad_relation():
    entities = [{'start': i, 'end': i + 1, 'type': label} for i, label in enumerate(ENTITY_LABELS)]
    prediction = {'entities': entities, 'relations': [{'head': 0, 'tail': 1, 'type': label}
                  for label in RELATION_LABELS], 'uncertainty': 1, 'impact': 1}
    schema = json.loads((Path(__file__).resolve().parents[3] / 'plantkg/evaluation/ontology/maintie-predictions.schema.json').read_text(encoding='utf8'))
    validate({'1': prediction}, schema)
    item = {'tokens': ['a'] * len(ENTITY_LABELS)}
    validate_maintie_prediction(item, prediction, '1')
    prediction['relations'][0]['tail'] = len(entities)
    with pytest.raises(ValueError, match='tail'):
        validate_maintie_prediction(item, prediction, '1')


def test_study_does_not_invent_cost_or_human_time():
    record = {'route': 'human', 'batch_id': 'batch-1', 'source_sha256': 'a' * 64}
    result = study_records([record])[0]
    assert result['api_fee'] is None and result['human_minutes'] is None
    assert study_totals([result])['human']['human_minutes'] is None
    with pytest.raises(ValueError, match='fee_evidence'):
        study_records([{**record, 'api_fee': 0}])


def test_policy_requires_independent_annotation_and_explicit_predictions():
    assert policy_impact([], [])['status'] == 'pending_annotation'
    labels = [{'change_id': 'change', 'annotator': 'reviewer', 'affected_rule_ids': ['a', 'b']}]
    with pytest.raises(ValueError, match='explicit prediction'):
        policy_impact(labels, [])
    score = policy_impact(labels, [{'change_id': 'change', 'affected_rule_ids': ['a', 'c']}])
    assert (score['tp'], score['fp'], score['fn'], score['recall']) == (1, 1, 1, .5)


def test_brick_prediction_ignores_replacement_assertions(tmp_path):
    old, new = tmp_path / 'old.ttl', tmp_path / 'new.ttl'
    old.write_text(f'<{BRICK}Old_Pump> a <http://www.w3.org/2002/07/owl#Class> .', encoding='utf8')
    content = (f'@prefix brick: <{BRICK}> . @prefix owl: <http://www.w3.org/2002/07/owl#> . '
               'brick:Old_Pump a owl:Class ; owl:deprecated true ; brick:isReplacedBy brick:TARGET . '
               'brick:New_Pump a owl:Class . brick:Other a owl:Class .')
    new.write_text(content.replace('TARGET', 'New_Pump'), encoding='utf8')
    first = brick_lexical(old, new)
    assert len(first) == 1
    assert first[f'{BRICK}Old_Pump'] == f'{BRICK}New_Pump'
    new.write_text(content.replace('TARGET', 'Other'), encoding='utf8')
    assert brick_lexical(old, new) == first
