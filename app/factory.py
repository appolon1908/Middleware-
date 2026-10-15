"""The one FastAPI construction authority of the Middleware repository.

Every HTTP process is built here:

* the Middleware API on :8095 through :func:`create_app` (composed by
  :mod:`app.application`: profiles, router registry, request guard, error
  envelope, health and runtime ownership),
* each separately deployed internal service (event gateway, policy engine,
  controller, provisioning, email, verifier, ...) through
  :func:`create_service_app`.

No other module calls ``FastAPI(...)``; ``tests/test_m1_f01_application_factory_design.py``
enforces that over ``app/``. This module imports only FastAPI so a small
service image does not load the API's router graph; ``create_app`` and
``AppProfile`` resolve lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

if TYPE_CHECKING:
    from app.application import AppProfile, create_app

__all__ = ["AppProfile", "build_application", "create_app", "create_service_app"]

# Interactive documentation is opt-in per service; production processes never
# publish it implicitly.
_SERVICE_DEFAULTS: dict[str, Any] = {"docs_url": None, "redoc_url": None}


def build_application(**options: Any) -> FastAPI:
    """Construct the FastAPI object for the composed Middleware API."""
    return FastAPI(**options)


def create_service_app(title: str, **options: Any) -> FastAPI:
    """Construct a separately deployed internal service process.

    The caller owns its routes and lifespan; documentation endpoints stay off
    unless the service passes them explicitly.
    """
    return FastAPI(title=title, **{**_SERVICE_DEFAULTS, **options})


def __getattr__(name: str) -> Any:
    if name in {"AppProfile", "create_app"}:
        from app import application

        return getattr(application, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
