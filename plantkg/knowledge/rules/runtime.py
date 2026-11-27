"""读取指定运行清单中已经激活过、且对事件时刻可见的 B30 规则。"""
from datetime import datetime

from plantkg.core.contracts import ErrorCode, PlantkgError, Rule
from plantkg.knowledge.rules.normalise import entity_keys


def _subject_matches(subjects, entity_id):
    for subject in subjects:
        if str(subject).startswith('entity://') and str(entity_id).startswith('entity://'):
            if str(subject) == str(entity_id):
                return True
        elif set(entity_keys(subject)) & set(entity_keys(entity_id)):
            return True
    return False


def matches_combo_rule(rule, event_type, entity_id, summary):
    """Only the explicitly supported, subject-bound correction format is executable."""
    if rule.kind != 'unknown_combo' or event_type != 'unknown_combo' or not entity_id:
        return False
    condition = rule.condition
    actions = condition.get('actions')
    return bool(_subject_matches(rule.subject_ids, entity_id) and set(condition) <= {'event_type', 'observation_contains', 'actions'}
                and condition.get('event_type') == 'unknown_combo'
                and (not condition.get('observation_contains') or str(condition['observation_contains']) in (summary or ''))
                and isinstance(actions, list) and actions
                and all(isinstance(a, dict) and set(a) == {'entity_id', 'command'}
                        and isinstance(a['entity_id'], str) and a['command'] == 'repair' for a in actions))


def matches_combo_advice(rule, event_type, entity_id, summary):
    """A text correction can inform a person, but cannot authorize an action."""
    if rule.kind != 'unknown_combo' or event_type != 'unknown_combo' or not entity_id:
        return False
    condition = rule.condition
    return bool(_subject_matches(rule.subject_ids, entity_id)
                and set(condition) <= {'event_type', 'action', 'note'}
                and condition.get('event_type') == 'unknown_combo'
                and isinstance(condition.get('note'), str) and condition['note'].strip())


def all_rules_for_manifest(versions, artifacts, manifest_id: str) -> tuple[Rule, ...]:
    manifest = versions.get(manifest_id)
    if not manifest.ever_active or manifest.status not in ("active", "retired"):
        raise PlantkgError(ErrorCode.PRECONDITION, "未激活过的知识不能用于运行")
    component = manifest.components.get("rules")
    if component is None or not component.ref.startswith("b30-rules:"):
        return ()
    return tuple(Rule.model_validate(rule) for rule in artifacts.load(component)["rules"])


def rules_for_manifest(versions, artifacts, manifest_id: str, at: datetime, as_of: datetime | None = None) -> tuple[Rule, ...]:
    return tuple(rule for rule in all_rules_for_manifest(versions, artifacts, manifest_id)
                 if rule.compilable and rule.is_visible(as_of or at) and rule.applies_to(at))
