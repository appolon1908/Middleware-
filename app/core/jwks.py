"""Process-wide JWKS key resolution for the Middleware identity verifiers.

Every Keycloak verifier resolves signing keys through :func:`jwks_client`, so
one JWK Set cache exists per (client class, JWKS URL, timeout) instead of one
per request. The cache policy is the identity contract
(``contracts/identity/middleware-identity.v1.json``):

* the JWK *set* is cached for :data:`JWKS_CACHE_LIFESPAN_SECONDS`; a key that
  Keycloak rotated out stops being trusted no later than one lifespan after
  the rotation (there is deliberately no per-``kid`` cache without expiry);
* an unknown ``kid`` forces at most one refetch per
  :data:`JWKS_REFRESH_COOLDOWN_SECONDS`, so forged key IDs cannot turn every
  request into a JWKS fetch while a freshly rotated key is still picked up;
* an unreachable or empty JWKS is :class:`JwksUnavailableError` (the caller
  answers 503), never a fallback to another authority or a stale key.

The client class is looked up on :mod:`jwt` at call time, so a test that
replaces ``jwt.PyJWKClient`` gets its own cache entry rather than a client
built by an earlier test.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import jwt
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError

JWKS_CACHE_LIFESPAN_SECONDS = 300
JWKS_REFRESH_COOLDOWN_SECONDS = 30
MAX_JWKS_CLIENTS = 32
_UNKNOWN_KID_PREFIX = "Unable to find a signing key"


class JwksUnavailableError(RuntimeError):
    """The configured JWKS authority could not supply a usable key set."""


class UnknownSigningKeyError(ValueError):
    """The token names a key the configured authority does not publish."""


_clients: OrderedDict[tuple[Any, str, float], Any] = OrderedDict()
_lock = threading.Lock()


def jwks_client(
    url: str,
    *,
    timeout: float,
    factory: Callable[..., Any] | None = None,
) -> Any:
    """The shared, bounded-lifespan JWKS client for ``url``."""
    if not url:
        raise JwksUnavailableError("JWKS URL is not configured")
    client_factory = factory if factory is not None else jwt.PyJWKClient
    key = (client_factory, url, float(timeout))
    with _lock:
        client = _clients.get(key)
        if client is None:
            client = client_factory(
                url,
                cache_keys=False,
                cache_jwk_set=True,
                lifespan=JWKS_CACHE_LIFESPAN_SECONDS,
                timeout=timeout,
                cooldown_duration=JWKS_REFRESH_COOLDOWN_SECONDS,
            )
            _clients[key] = client
            while len(_clients) > MAX_JWKS_CLIENTS:
                _clients.popitem(last=False)
        else:
            _clients.move_to_end(key)
        return client


def signing_key(client: Any, token: str) -> Any:
    """Resolve the token's signing key; classify failures for the caller.

    Raises :class:`UnknownSigningKeyError` for a key the authority does not
    publish (an invalid token) and :class:`JwksUnavailableError` when the
    authority itself is unreachable or publishes no signing keys.
    """
    try:
        return client.get_signing_key_from_jwt(token)
    except PyJWKClientConnectionError as exc:
        raise JwksUnavailableError("identity key authority is unreachable") from exc
    except PyJWKClientError as exc:
        if str(exc).startswith(_UNKNOWN_KID_PREFIX):
            raise UnknownSigningKeyError("token signing key is not published") from exc
        raise JwksUnavailableError("identity key authority is unusable") from exc
    except (PyJWKSetError, json.JSONDecodeError) as exc:
        raise JwksUnavailableError("identity key authority is unusable") from exc


def reset_jwks_clients() -> None:
    """Forget every cached client (tests and controlled key-rotation drills)."""
    with _lock:
        _clients.clear()
