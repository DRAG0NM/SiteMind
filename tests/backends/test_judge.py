import json
from io import BytesIO
from urllib.error import HTTPError

import pytest
from pydantic import ValidationError

from plantkg.backends import FakeJudge, FallbackJudge, JevJudge, create_judge
from plantkg.backends.calibration import TemperatureCalibrator
from plantkg.backends.demo import main
from plantkg.backends.judge import JevTransport
from plantkg.core.config import ConfigError, JudgeConfig, check_runtime_secrets, load_settings
from plantkg.core.contracts import ErrorCode, FrozenSplit, JudgeResult, OntologyCandidate, PlantkgError
from plantkg.core.ports import Judge
from plantkg.knowledge.judge import dispatch_domains


OPTIONS = ("energy", "elevator", "hvac", "equipment")
DEFS = {key: f"Definition of {key}." for key in OPTIONS}
SCORES = dict(zip(OPTIONS, (0.6, 0.2, 0.05, 0.15)))


def question(options=OPTIONS):
    return OntologyCandidate(candidate_id="power-surge", layer="concept", subject="event:0805",
                             options=options, recall_source="vector")


def state(**extra):
    return {"definitions": DEFS, "content": "用电突增", "vector_top": "energy", "coverage": 0.9,
            "fake_distributions": [SCORES] * 3} | extra


def test_ontology_and_domain_gates_are_separate():
    judge = FakeJudge()
    accepted = judge.decide(state(), question(), OPTIONS)
    assert accepted.gate_accept and accepted.order_agree
    assert accepted.distribution == pytest.approx(SCORES)
    assert accepted.prob is None and accepted.calibration_version is None
    rejected = judge.decide(state(vector_top="hvac"), question(), OPTIONS)
    assert not rejected.gate_accept and rejected.cross_backend_agree is False
    missing = judge.decide(state(vector_top=None), question(), OPTIONS)
    assert not missing.gate_accept and missing.cross_backend_agree is None
    domains = dispatch_domains(judge, "event-0805", "用电突增", signals={"fake_distributions": [SCORES] * 3})
    assert domains.gate_kind == "domain" and domains.gate_accept
    assert domains.vector_top is None and domains.candidate_recall_source == "manual"
    assert sum(domains.distribution.values()) == pytest.approx(1)
    # 旧结果省略新字段仍按本体门控解释，不能借派域条件放行。
    old = accepted.model_dump(exclude={"gate_kind", "distribution"})
    assert JudgeResult.model_validate(old).gate_kind == "ontology"
    with pytest.raises(ValidationError, match="向量"):
        JudgeResult.model_validate(domains.model_dump() | {"gate_kind": "ontology"})


def test_disagreement_and_ties_never_accept_even_with_high_score():
    alternate = {"energy": 0.1, "elevator": 0.85, "hvac": 0.01, "equipment": 0.04}
    result = FakeJudge().decide(state(fake_distributions=[SCORES, SCORES, alternate]), question(), OPTIONS)
    assert not result.gate_accept and not result.order_agree
    tie = {key: 0.25 for key in OPTIONS}
    domain = question().model_copy(update={"recall_source": "manual"})
    result = FakeJudge().decide(state(fake_distributions=[tie] * 3, gate_kind="domain", vector_top=None), domain, OPTIONS)
    assert not result.gate_accept and all(o.choice is None for o in result.orders)


def test_domain_cannot_bypass_vector_gate_in_backend_or_contract():
    with pytest.raises(ValueError, match="派域题不使用向量"):
        FakeJudge().decide(state(gate_kind="domain", vector_top="hvac"), question(), OPTIONS)
    manual = question().model_copy(update={"recall_source": "manual"})
    with pytest.raises(ValueError, match="派域题不使用向量"):
        FakeJudge().decide(state(gate_kind="domain"), manual, OPTIONS)
    accepted = FakeJudge().decide(state(), question(), OPTIONS)
    forged = accepted.model_dump() | {"gate_kind": "domain", "vector_top": "hvac", "cross_backend_agree": False}
    with pytest.raises(ValidationError, match="派域题不使用向量"):
        JudgeResult.model_validate(forged)
    # 即使门控未放行，记录中的来源也不能伪装成派域输入。
    with pytest.raises(ValidationError, match="派域题不使用向量"):
        JudgeResult.model_validate(forged | {"gate_accept": False})


@pytest.mark.parametrize("large", [1e308, 10**400])
def test_large_finite_scores_are_normalized_without_overflow(large):
    values = {key: large for key in OPTIONS}
    result = FakeJudge().decide(state(fake_distributions=[values] * 3), question(), OPTIONS)
    assert result.distribution == pytest.approx({key: 0.25 for key in OPTIONS})
    assert not result.gate_accept and all(order.choice is None for order in result.orders)


@pytest.mark.parametrize("values", [
    {"energy": 0.9}, dict(SCORES, energy=-1), dict(SCORES, energy=float("nan")),
    dict(SCORES, energy=True), {key: 0 for key in OPTIONS},
])
def test_incomplete_or_invalid_distributions_fail(values):
    with pytest.raises(ValueError):
        FakeJudge().decide(state(fake_distributions=[values] * 3), question(), OPTIONS)


@pytest.mark.parametrize("options", [("energy",), ("energy", "elevator")])
def test_insufficient_distinct_orders_do_not_accept(options):
    domain = question(options).model_copy(update={"recall_source": "manual"})
    result = FakeJudge().decide({"definitions": {o: o for o in options}, "gate_kind": "domain"}, domain, options)
    assert not result.gate_accept and not result.order_agree and len(result.orders) == len(options)
    assert sum(result.distribution.values()) == pytest.approx(1)
    ontology = FakeJudge().decide({"definitions": {o: o for o in options}, "vector_top": options[0]}, question(options), options)
    assert not ontology.gate_accept and len(ontology.orders) == len(options)


def test_jev_protocol_mapping_normalization_and_metadata():
    calls = []

    def transport(body):
        calls.append(body)
        criteria = body["questions"]["decision"]["criteria"]
        probabilities = {key: SCORES[value.split(":")[0]] * 0.98 for key, value in criteria.items()}
        return {"answers": {"decision": {"type": "choice", "probabilities": probabilities}}}

    judge = JevJudge(transport=transport)
    result = judge.decide(state(), question(), OPTIONS)
    assert len(calls) == 3 and len({tuple(c["questions"]["decision"]["criteria"].values()) for c in calls}) == 3
    assert all(c["state"] == "用电突增" and c["model"] == "decision-model-preview" for c in calls)
    assert result.distribution == pytest.approx(SCORES)
    assert result.backend == "jev" and result.gate_accept
    assert result.prob is None  # 原始概率不是校准概率。
    assert len(result.top_k) == 3


@pytest.mark.parametrize("response", [{}, {"answers": None}, {"answers": {"decision": {"type": "choice", "probabilities": {"o0": 1}}}}])
def test_bad_jev_response_cannot_silently_fallback(response):
    with pytest.raises(PlantkgError, match="无效或不完整"):
        JevJudge(transport=lambda _: response).decide(state(), question(), OPTIONS)


def test_transport_builds_beijing_endpoint_and_redacts_errors(monkeypatch):
    monkeypatch.setenv("TEST_WS", "workspace-123")
    monkeypatch.setenv("TEST_KEY", "sk-SECRET-SHOULD-NOT-APPEAR")
    calls = []

    def urlopen(request, timeout):
        calls.append((request, timeout))
        return BytesIO(b'{"answers": {}}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    transport = JevTransport(workspace_env="TEST_WS", api_key_env="TEST_KEY")
    assert transport({"state": "event"}) == {"answers": {}}
    assert calls[0][0].full_url == "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone"
    assert json.loads(calls[0][0].data) == {"state": "event"}
    assert calls[0][1] == 10

    def denied(request, timeout):
        raise HTTPError(request.full_url, 401, "sk-SECRET-SHOULD-NOT-APPEAR", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", denied)
    with pytest.raises(PlantkgError) as exc:
        transport({})
    assert "401" in str(exc.value) and "SECRET" not in str(exc.value) and "workspace-123" not in str(exc.value)


def test_jev_config_checks_workspace_and_runtime_errors_are_port_errors(monkeypatch):
    settings = load_settings(profile="dev").model_copy(update={
        "judge": JudgeConfig(backend="jev", jev={"workspace_env": "TEST_WS", "api_key_env": "TEST_KEY"})})
    with pytest.raises(ConfigError, match="TEST_WS"):
        check_runtime_secrets(settings, environ={"TEST_KEY": "secret"})
    with pytest.raises(ConfigError, match="TEST_WS") as exc:
        check_runtime_secrets(settings, environ={"TEST_KEY": "secret", "TEST_WS": "bad/value"})
    assert "bad/value" not in str(exc.value)
    check_runtime_secrets(settings, environ={"TEST_KEY": "secret", "TEST_WS": "valid-123"})
    monkeypatch.delenv("TEST_WS", raising=False)
    monkeypatch.setenv("TEST_KEY", "secret")
    judge = JevJudge(transport=JevTransport(workspace_env="TEST_WS", api_key_env="TEST_KEY"))
    with pytest.raises(PlantkgError) as exc:
        judge.decide(state(), question(), OPTIONS)
    assert exc.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE and "TEST_WS" in str(exc.value)
    monkeypatch.setenv("TEST_WS", "valid-123")
    monkeypatch.delenv("TEST_KEY")
    with pytest.raises(PlantkgError) as exc:
        judge.decide(state(), question(), OPTIONS)
    assert exc.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE and "TEST_KEY" in str(exc.value)


def test_jev_bad_orders_cli_reports_config_error_without_traceback(monkeypatch, capsys):
    from plantkg import __main__ as cli

    settings = load_settings(profile="dev").model_copy(update={
        "judge": JudgeConfig(backend="jev", jev={"orders": 2})})
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: settings)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")
    monkeypatch.setenv("BAILIAN_WS", "valid-123")
    assert cli.main(["--profile", "dev", "--dry-run"]) == 2
    error = capsys.readouterr().err
    assert "装配失败" in error and "三种顺序" in error and "Traceback" not in error


def test_config_switch_and_fallback_does_not_fabricate_scores():
    for backend, cls in (("fake", FakeJudge), ("fallback", FallbackJudge), ("jev", JevJudge)):
        judge = create_judge(JudgeConfig(backend=backend), transport=lambda _: {})
        assert isinstance(judge, cls) and isinstance(judge, Judge)
    fallback = FallbackJudge()
    with pytest.raises(ValueError, match="vector_scores"):
        fallback.decide(state(), question(), OPTIONS)
    result = fallback.decide(state(vector_scores=SCORES), question(), OPTIONS)
    assert result.gate_accept and result.backend == "fallback" and result.prob is None
    result = dispatch_domains(fallback, "domain", "用电突增", signals={"domain_scores": SCORES})
    assert result.gate_accept
    with pytest.raises(ConfigError):
        create_judge(JudgeConfig(backend="jev", jev={"orders": 1}))


def test_input_definitions_and_unknown_rejected_before_transport():
    with pytest.raises(ValueError, match="定义"):
        JevJudge(transport=lambda _: pytest.fail("must not call")).decide(state(definitions={}), question(), OPTIONS)
    with pytest.raises(ValidationError, match="Unknown"):
        question(("unknown", "energy", "hvac"))


def test_calibration_fitted_on_separate_split_saved_and_reloaded(tmp_path):
    results = [FakeJudge().decide(state(), question(), OPTIONS)]
    split = FrozenSplit(split_id="cal-synthetic-v1", purpose="calibration", days=("2026-07-01",),
                        frozen_at="2026-06-30T00:00:00Z", content_hash="sha256:" + "a" * 64)
    calibrator = TemperatureCalibrator()
    assert calibrator.predict(results[0]) is None
    version = calibrator.fit(results, ["elevator"], split)
    path = tmp_path / "calibration.json"
    calibrator.save(path)
    loaded = TemperatureCalibrator.load(path)
    result = FakeJudge(calibrator=loaded).decide(state(), question(), OPTIONS)
    assert result.calibration_version == version and result.prob != result.score
    assert result.gate_accept  # 校准数值不参与门控。
    assert loaded.predict(result.model_copy(update={"model": "different-version"})) is None
    with pytest.raises(ValueError, match="calibration"):
        calibrator.fit(results, ["energy"], split.model_copy(update={"purpose": "test"}))
    artifact = json.loads(path.read_text())
    artifact["temperature"] = 0.01
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="校验"):
        TemperatureCalibrator.load(path)


def test_offline_demo_export_contains_complete_probabilities_and_provenance(tmp_path, capsys):
    output = tmp_path / "domain.json"
    assert main(["--output", str(output)]) == 0
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["event_nature"] == record["result_origin"] == "synthetic"
    assert record["real_call_status"] == "pending_business_acceptance"
    result = JudgeResult.model_validate(record["result"])
    assert result.gate_accept and set(result.distribution) == set(OPTIONS)
    assert "0.6" in capsys.readouterr().out


def test_bootstrap_registers_configured_judge():
    from plantkg.bootstrap import build_services

    services = build_services(load_settings(profile="mock"))
    try:
        assert isinstance(services.registry.resolve(Judge), FakeJudge)
    finally:
        services.close()


def test_existing_export_stops_before_any_model_request(tmp_path, monkeypatch):
    output = tmp_path / "existing.json"
    output.write_text("preserve")
    monkeypatch.setattr("plantkg.backends.demo.create_judge", lambda *a, **k: pytest.fail("must not construct live judge"))
    with pytest.raises(SystemExit):
        main(["--backend", "jev", "--output", str(output)])
    assert output.read_text() == "preserve"
