from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.social import RegisterMediaAsset


def _payload(storage_reference: str) -> dict[str, object]:
    return {
        "tenant_id": "11111111-1111-4111-8111-111111111111",
        "media_type": "video",
        "content_type": "video/mp4",
        "storage_reference": storage_reference,
        "checksum_sha256": "a" * 64,
        "metadata": {},
    }


def test_media_reference_accepts_codestra_video_namespace() -> None:
    model = RegisterMediaAsset.model_validate(
        _payload("codestra-video://exports/final/example.mp4")
    )
    assert model.storage_reference == "codestra-video://exports/final/example.mp4"


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.com/video.mp4",
        "file:///etc/passwd",
        "codestra-video://user@exports/example.mp4",
        "codestra-video://exports:443/example.mp4",
        "codestra-video://exports/../secret.mp4",
        "codestra-video://exports/%2e%2e/secret.mp4",
        "codestra-video://exports/example.mp4?token=secret",
        "codestra-video://exports/example.mp4#fragment",
        " codestra-video://exports/example.mp4",
        "codestra-video://exports",
    ],
)
def test_media_reference_rejects_external_or_ambiguous_locations(
    reference: str,
) -> None:
    with pytest.raises(ValidationError):
        RegisterMediaAsset.model_validate(_payload(reference))


def test_social_media_routes_are_served_by_canonical_integration_profile(
    test_settings,
) -> None:
    from app.application import AppProfile, create_app

    app = create_app(settings=test_settings, profile=AppProfile.INTEGRATION)
    paths = {route.path for route in app.routes}
    assert "/api/v1/social/media" in paths
    assert "/api/v1/social/media/{asset_id}" in paths


def test_social_api_uses_handler_keycloak_auth_not_shared_secret(test_settings) -> None:
    from app.core.request_guard import RequestGuard

    guard = RequestGuard(test_settings)
    assert guard.handler_authenticated("POST", "/api/v1/social/media") is True
    assert guard.handler_authenticated("GET", "/api/v1/social/providers") is True
    assert guard.openapi_auth_mode("POST", "/api/v1/social/media") == "handler-bearer"
    assert (
        guard.openapi_auth_mode("POST", "/api/v1/social/webhooks/{provider}")
        == "signed-ingress"
    )


def test_every_non_webhook_social_route_has_verified_principal_dependency() -> None:
    from fastapi.routing import APIRoute

    from app.api.v1.social import router
    from app.core.social_auth import require_social_principal

    for route in router.routes:
        if not isinstance(route, APIRoute) or "/webhooks/" in route.path:
            continue
        dependency_calls = {
            dependency.call for dependency in route.dependant.dependencies
        }
        assert require_social_principal in dependency_calls, route.path
