from fastapi.testclient import TestClient
from app.application import AppProfile, create_app
from app.core.config import Settings

def settings():
    return Settings.from_env({"APP_ENV":"test","ALLOW_IN_MEMORY_STORAGE":"true"})

def test_injected_runtime_is_not_owned():
    s=settings(); runtime=object()
    app=create_app(settings=s,runtime=runtime,profile=AppProfile.INTEGRATION)
    assert app.state.runtime is runtime
    assert app.state.runtime_state.runtime is runtime
    assert app.state.runtime_state.owns_runtime is False

def test_factory_lifespan_exposes_and_clears_runtime(monkeypatch):
    s=settings(); built=object(); closed=[]
    async def build(self):
        self.runtime=built; self.owns_runtime=True; return built
    async def close(self):
        closed.append(self.runtime); self.runtime=None; self.owns_runtime=False
    monkeypatch.setattr("app.core.health.RuntimeState.build",build)
    monkeypatch.setattr("app.core.health.RuntimeState.close",close)
    app=create_app(settings=s,profile=AppProfile.CONTROL_PLANE)
    assert app.state.runtime is None
    with TestClient(app):
        assert app.state.runtime is built
        assert app.state.runtime_state.runtime is built
    assert closed == [built]
    assert app.state.runtime is None
    assert app.state.runtime_state.runtime is None

def test_factory_build_is_effect_free_before_lifespan(monkeypatch):
    s=settings(); calls=[]
    async def build(self):
        calls.append("build"); raise AssertionError("runtime must not build during create_app")
    monkeypatch.setattr("app.core.health.RuntimeState.build",build)
    app=create_app(settings=s,profile=AppProfile.INTEGRATION)
    assert calls == []
    assert app.state.runtime is None
