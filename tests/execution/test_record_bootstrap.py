"""Real B16 registration without altering disabled/mock entry points."""
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.ports import Executor, StateProvider
from plantkg.world.record.executor import RecordExecutor


def test_storage_registration_and_disabled_profile():
    for enabled in (False, True):
        services = build_services(load_settings(profile="mock", overrides={"storage.enabled": enabled}))
        try:
            if enabled:
                assert isinstance(services.executor, RecordExecutor)
                assert services.registry.resolve(Executor) is services.executor
                assert services.registry.resolve(StateProvider).projection_reader is not None
                assert services.gate.execution_basis_provider is None
            else:
                assert services.executor is None
            assert not any("execution" in name for name in services.registry.tools)
        finally:
            services.close()
