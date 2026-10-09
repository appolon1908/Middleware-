"""Disposable, effect-disabled API fixture for the synthetic Newman collection.

Run only on localhost. This fixture accepts test-signed tokens and must never
be used as a deployment entrypoint.
"""

from app.core.config import Settings
from tests.test_platform_api import Stack

settings = Settings.from_env(
    {
        "APP_ENV": "test",
        "ALLOW_IN_MEMORY_STORAGE": "true",
        "EXTERNAL_EFFECTS": "false",
    }
)
stack = Stack(settings)
app = stack.app
