#!/usr/bin/env python3
"""Validate a normalized release intent without contacting any runtime."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from copy import deepcopy
from email.message import Message
from pathlib import Path
from typing import Any, cast


CONTRACT_PATH = Path(".codestra/production-orchestrator-contract.v1.json")
SCHEMA = "codestra.production-orchestrator-contract.v1"
WORKFLOW = ".github/workflows/manual-release-intent.yml"
CONTROLLER_REPOSITORY = "appolon1908/codestra-production-platform"
CONTROLLER_BRANCH = "release/production-activation"
INDEPENDENT_REVIEWER_ID = 77101516
CANDIDATE_SCHEMA = "codestra.manual-production-candidate.v1"
ZERO64 = "0" * 64
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,127}$")
IMAGE = re.compile(r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$")
IMAGE_REPOSITORY = re.compile(r"^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+$")
CONFIRMATIONS = {
    "plan": "PLAN_RELEASE_INTENT",
    "staging": "APPROVE_STAGING_INTENT",
    "canary": "APPROVE_CANARY_INTENT",
    "production": "APPROVE_PRODUCTION_INTENT",
}
PREVIOUS_PHASE = {"staging": "plan", "canary": "staging", "production": "canary"}
SAFETY_KEYS = {
    "external_effects_default",
    "live_email_delivery",
    "live_sms_delivery",
    "live_pstn_dialing",
    "odoo_write",
    "n8n_external_delivery",
    "live_trading",
    "payment_execution",
}
CANDIDATE_SAFETY_KEYS = (SAFETY_KEYS - {"external_effects_default"}) | {
    "external_effects_enabled"
}
CATALOG_REPOSITORIES = {
    "appolon1908/Infustruction-repo",
    "appolon1908/Keycloak",
    "appolon1908/Middleware-",
    "appolon1908/codestra",
    "appolon1908/beyvra-backend",
    "appolon1908/backend2",
    "appolon1908/beyvra-frontend",
    "appolon1908/scrapper",
    "appolon1908/Breero.com",
    "appolon1908/Moneybee-Backend",
    "appolon1908/Telnexa-web",
    CONTROLLER_REPOSITORY,
}
PR_ONLY_REQUIRED_CHECKS = {
    "appolon1908/Keycloak": frozenset({"bootstrap"}),
    "appolon1908/Middleware-": frozenset(
        {
            "Validate middleware merge result",
            "Validate middleware source head",
        }
    ),
}
EXPECTED_CHECK_WORKFLOWS = {
    "appolon1908/Infustruction-repo": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "validate": ".github/workflows/source-authority-matrix.yml",
        "validate-source": ".github/workflows/source-authority-matrix.yml",
        "validate-merge-result": ".github/workflows/source-authority-matrix.yml",
    },
    "appolon1908/Keycloak": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "validate": ".github/workflows/validate.yml",
        "validate-source": ".github/workflows/validate.yml",
        "validate-merge-result": ".github/workflows/validate.yml",
    },
    "appolon1908/Middleware-": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "validate": ".github/workflows/middleware-ci.yml",
        "connector-runtime-build": ".github/workflows/middleware-ci.yml",
        "docker-test-build": ".github/workflows/middleware-ci.yml",
        "docker-runtime-build": ".github/workflows/middleware-ci.yml",
        "container-security": ".github/workflows/middleware-ci.yml",
        "Disposable PostgreSQL Redis integration": ".github/workflows/middleware-ci.yml",
        "Disposable NATS JetStream integration": ".github/workflows/middleware-ci.yml",
        "Temporal critical workflow integration": ".github/workflows/middleware-ci.yml",
        "Synthetic no-effect acceptance E2E": ".github/workflows/middleware-ci.yml",
    },
    "appolon1908/codestra": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "verify": ".github/workflows/ci.yml",
        "container": ".github/workflows/ci.yml",
    },
    "appolon1908/beyvra-backend": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "container": ".github/workflows/ci.yml",
        "exact-head-base-ci": ".github/workflows/ci.yml",
        "secrets": ".github/workflows/ci.yml",
        "validate": ".github/workflows/ci.yml",
    },
    "appolon1908/backend2": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "validate": ".github/workflows/ci.yml",
        "container": ".github/workflows/ci.yml",
    },
    "appolon1908/beyvra-frontend": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "exact-head-base-ci": ".github/workflows/ci.yml",
        "secrets": ".github/workflows/ci.yml",
        "validate": ".github/workflows/ci.yml",
    },
    "appolon1908/scrapper": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "deployment-policy": ".github/workflows/ci.yml",
        "validate": ".github/workflows/ci.yml",
    },
    "appolon1908/Breero.com": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "quality": ".github/workflows/quality.yml",
    },
    "appolon1908/Moneybee-Backend": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "verify": ".github/workflows/ci.yml",
        "postgres-identity-tenancy": ".github/workflows/ci.yml",
        "containers (api)": ".github/workflows/ci.yml",
        "containers (worker)": ".github/workflows/ci.yml",
        "containers (migrate)": ".github/workflows/ci.yml",
        "application": ".github/workflows/secure-ci.yml",
        "deployment-policy": ".github/workflows/secure-ci.yml",
    },
    "appolon1908/Telnexa-web": {
        "orchestrator-contract": ".github/workflows/production-orchestrator-contract.yml",
        "validate-build-smoke": ".github/workflows/ci.yml",
        "docker-build": ".github/workflows/ci.yml",
    },
    CONTROLLER_REPOSITORY: {
        "checks-only-policy": ".github/workflows/production-merge-gate.yml",
        "diagnose": ".github/workflows/gitleaks-pr-diagnostics.yml",
        "production-gate": ".github/workflows/production-merge-gate.yml",
        "validate": ".github/workflows/platform-source-gate.yml",
    },
}
ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256 = (
    "5e968a824d9738ac8237dfd677bae1091aaecfe73f3f98d0c6c63f07a503968f"
)
EXPECTED_CHECK_WORKFLOW_SHA256 = {
    "appolon1908/Infustruction-repo": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/source-authority-matrix.yml": "1ca826d1f37c06b0ad2bc5a94a5b19516ad24fce486bdaa1b5b384e62f759221",
    },
    "appolon1908/Keycloak": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/validate.yml": "34e8692d93f3a30949e1e3de0543d4db93c508ce026538f6b5a8442d1a800f1a",
    },
    "appolon1908/Middleware-": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/middleware-ci.yml": "0f4d5367d2c5785394988403a368bab10a87c96fa2c0f2e9e99402dc63ea4897",
    },
    "appolon1908/codestra": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/ci.yml": "7b0a377343c86b1274ecb91c4cc2423d6c045c0197eae9eb1abe775d791a73d1",
    },
    "appolon1908/beyvra-backend": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/ci.yml": "1c2e654ffd1011f662985d261411502c392789b882b6d089ba18e182e53248d1",
    },
    "appolon1908/backend2": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/ci.yml": "e27367a06aa79f7adca93d148f7e9c88efe35e77a3893407ffc3988f1b36c217",
    },
    "appolon1908/beyvra-frontend": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/ci.yml": "7459a31c6b005e9345661b10ee8df45a570ac652280a2eacbcdfd4673fb115da",
    },
    "appolon1908/scrapper": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/ci.yml": "31d81c5be094a1510bc821ef4359bba591630d2273662f5de0683205d908c60d",
    },
    "appolon1908/Breero.com": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/quality.yml": "9e8367e853316594a325fbcb0f22f1e701c35c205b15e66a5228ae8b4ce10ce4",
    },
    "appolon1908/Moneybee-Backend": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/ci.yml": "0bed241476483a0ac38e0fc8bb2b06a23b076645a6b0b355cf0420fcf4d2f451",
        ".github/workflows/secure-ci.yml": "6ab4ebf30e47aee65ba3e1d7106ddd0c6feea546a57ebd289cf4fcfed9106e00",
    },
    "appolon1908/Telnexa-web": {
        ".github/workflows/production-orchestrator-contract.yml": ORCHESTRATOR_CONTRACT_WORKFLOW_SHA256,
        ".github/workflows/ci.yml": "1b8db51b1d607a04b9d1f578802c4f58d1eb824638cc5a9ac1bd114a9869a462",
    },
    CONTROLLER_REPOSITORY: {
        ".github/workflows/gitleaks-pr-diagnostics.yml": "2edfc97221b0fc0b8668075056b6205494eb77e37d037017fcfb2fa6d04ba732",
        ".github/workflows/platform-source-gate.yml": "e1de711a014a5056083aba8755289916b4a7fd6eadaf3ec2aa77077821c925ff",
        ".github/workflows/production-merge-gate.yml": "921eb777b8e6beb77a038b88edcc9a0b1ccba34d4e4cf8b68ce94768c4d5e47e",
    },
}
SHARED_PRODUCTION_VALIDATOR_SHA256 = (
    "6006bbc7850ce7666de926b6cad2585b"
    "83d2fce102104543b871530f11115f20"
)
KEYCLOAK_PRODUCTION_VALIDATOR_SHA256 = (
    "6006bbc7850ce7666de926b6cad2585b"
    "83d2fce102104543b871530f11115f20"
)
MIDDLEWARE_PRODUCTION_VALIDATOR_SHA256 = (
    "ef13fa345136da67e3c1d259e359e37f7782b09f5d3cd1443eb517c1860f321b"
)
BACKEND_PRODUCTION_VALIDATOR_SHA256 = (
    "6006bbc7850ce7666de926b6cad2585b"
    "83d2fce102104543b871530f11115f20"
)
EXPECTED_CHECK_WORKFLOW_EXECUTABLE_SHA256 = {
    "appolon1908/Infustruction-repo": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": SHARED_PRODUCTION_VALIDATOR_SHA256,
        },
    },
    "appolon1908/Keycloak": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": KEYCLOAK_PRODUCTION_VALIDATOR_SHA256,
        },
        ".github/workflows/validate.yml": {
            "Dockerfile": (
                "43c8c2347e91c23adffbeb01955b7bf3"
                "d0fce6c2b1e47b66f8eedc9a172ff86e"
            ),
            "compose.yaml": (
                "95f1f64a4383bcde88004363860adeb5"
                "16821e669ca0a473c5e11d5b803c6342"
            ),
            "scripts/test-plan-gate.sh": (
                "a1998a4a92a2535aea09f35c5de369f"
                "0675ab4276e86ab92a42f908590c0ca6d"
            ),
            "scripts/validate-governance.sh": (
                "cefd59aaba1446e9aa0d8b0fc5370eab"
                "90f1d629422a0be25fad675515440c80"
            ),
            "scripts/validate-service-integrations.py": (
                "8063ee5e69b6dd3da7682f56e76a8b9"
                "e92c1b0dab785a961b4e05372d9f9d132"
            ),
            "scripts/validate.sh": (
                "770f873d978b072dc86d5b8bec1c958f"
                "3a02d67b69bdda27f8cc77a3da6ee3d8"
            ),
        },
    },
    "appolon1908/Middleware-": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": MIDDLEWARE_PRODUCTION_VALIDATOR_SHA256,
        },
        ".github/workflows/middleware-ci.yml": {
            "Dockerfile.runtime": (
                "e6760021a1a8dda584819f731d5e0f10f"
            "8555c16802616963c37622c14650ea7"
            ),
            "scripts/integration_ci.sh": (
                "8d9327fd9ad51d6ba7243d051336f623"
                "a4f75d60c60e69fd012e65f598b12d4a"
            ),
            "scripts/nats_integration_ci.sh": (
                "88d843c665cece68e0fb56a931c295ee"
                "10490446cad7b64d9f5356c1cbf7263d"
            ),
            "scripts/run_ci.sh": (
                "64d7c92279dd442144c7e1f74c3e48f"
                "0ab5d5db105238a534dcf8ccd99e93138"
            ),
            "scripts/synthetic_acceptance_ci.sh": (
                "087dac2c5371f2013fa0a8dd22ed4024"
                "409ab5015231fb8801c75cf3203e3a8a"
            ),
            "scripts/temporal_integration_ci.sh": (
                "76a682cc1f5b15a0a3eb15a029d87206"
                "238dfe4a262eaf5fa2c79403f147d4d6"
            ),
            "scripts/verify_container_image.sh": (
                "61ca5bfe98f045adc300856a0679631f"
            "836c918a12a96597e72375439538cfe7"
            ),
        },
    },
    "appolon1908/codestra": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": SHARED_PRODUCTION_VALIDATOR_SHA256,
        },
    },
    "appolon1908/beyvra-backend": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": BACKEND_PRODUCTION_VALIDATOR_SHA256,
        },
    },
    "appolon1908/backend2": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": SHARED_PRODUCTION_VALIDATOR_SHA256,
        },
    },
    "appolon1908/beyvra-frontend": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": SHARED_PRODUCTION_VALIDATOR_SHA256,
        },
    },
    "appolon1908/scrapper": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": SHARED_PRODUCTION_VALIDATOR_SHA256,
        },
    },
    "appolon1908/Breero.com": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": SHARED_PRODUCTION_VALIDATOR_SHA256,
        },
        ".github/workflows/quality.yml": {
            ".github/workflows/backend-production.yml": (
                "45b2918627995cb3491f55b3a3b537e"
                "4a34598d7b32877879b9e2c912c266591"
            ),
            ".github/workflows/frontend-production.yml": (
                "ba5f99dcdbcdb78e4e638153fb740ab1"
                "04502cb2f8c75923a134d7d5e1e24b8d"
            ),
            ".github/workflows/release-images.yml": (
                "edf3678604b134a21bc69ba8e793f0bc"
                "542dd4e0005d79e3e71eb2a112563b5b"
            ),
            "apps/api/Dockerfile": (
                "9f2a4ea8ee572d02a238bdeb6aa5dc4d"
                "c122fd348dea23e32e92f9cf94453fe5"
            ),
            "deploy/frontend/Dockerfile": (
                "0f6ee0e77e353b56660fe317826f2fb8"
                "6a2eb4391b55f93e816312eb0f588717"
            ),
            "deploy/portals/Dockerfile": (
                "8ec9b8ac30f114bd65e2b70558ca4125"
                "2049c9fc5458a3d758cc5766daad4b9b"
            ),
            "scripts/ci/classify-quality-scope.sh": "7cc6cc7d213e4c962a8cda4ce052bbcfa51decc1af78ac663b1170c1b8c210c2",
            "scripts/ci/test-classify-quality-scope.sh": "0365cd71d85e00facf1a64c2f11734e413430af75e4cf39e0e52971d13d5c473",
            "scripts/ci/test-validate-breero-scope.sh": "ea29de36868e28ff82e3ec151f896aed388d2421f5907151c4c13480dae20bf8",
            "scripts/ci/validate-breero-scope.sh": "f8ffb8a3953c56d7d6722938825bfb33fced802ba162f4cefd3d12be8ffb9a1e",
        },
    },
    "appolon1908/Moneybee-Backend": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": SHARED_PRODUCTION_VALIDATOR_SHA256,
        },
    },
    "appolon1908/Telnexa-web": {
        ".github/workflows/production-orchestrator-contract.yml": {
            ".codestra/validate-production-orchestrator-contract.py": SHARED_PRODUCTION_VALIDATOR_SHA256,
        },
    },
}
RELEASE_VALIDATOR_SOURCE_PATH = ".codestra/validate-release-intent.py"
EXPECTED_REQUIRED_CHECK_SOURCE_CLOSURE_SHA256 = {
    "appolon1908/Infustruction-repo": (
        "218de46417be1425f8686cdf35c881ae"
        "cad750495cbe4576831de6aec3e642b2"
    ),
    "appolon1908/Keycloak": (
        "0a409c1c9cc8c6f43d2d83d5347b9433"
        "fcdd0d5c3832479b10deda8c6a6afca6"
    ),
    "appolon1908/Middleware-": (
        "bee3e212f9fd457c6012d258d0e2b4a3ff"
        "e8e43c31361c2a5aba39348d67a52d"
    ),
    "appolon1908/codestra": (
        "4e3ea69c3ec2a4bd6e4b50395672f44d"
        "ec4a75460ed8648186445f1e8793b016"
    ),
    "appolon1908/beyvra-backend": (
        "8a3a6eb731ece61cc83f8e0333689f70"
        "87be9db4f7e93698a37f860fdd453135"
    ),
    "appolon1908/backend2": (
        "fa191e95756aec0a8987425eb697eb8e"
        "2316d7b59ba77e6cd10bb52b10c2c43a"
    ),
    "appolon1908/beyvra-frontend": (
        "ce51e23c535871d23306264bb3806bb1"
        "3a710efeb649e0247e3377ac929ab5eb"
    ),
    "appolon1908/scrapper": (
        "783feb31fc0ada4b043a62bf53dbfc1f"
        "19ad25962daeeff809b9a9d391b1e2f0"
    ),
    "appolon1908/Breero.com": (
        "d59c04b6a621ab43c8e57c795880b050"
        "c27b77d05e43e4edb696db6aac677e60"
    ),
    "appolon1908/Moneybee-Backend": (
        "a283e388028892ced3ac8445893ec2fa"
        "8bd7c7373418284f08106c82b765c31a"
    ),
    "appolon1908/Telnexa-web": (
        "dbd17acc6862e74d9eb5ffbaf3a1f3f4"
        "9c41e364057a08e3aa57588afea236a3"
    ),
    CONTROLLER_REPOSITORY: (
        "4c7b54aa7cd59ac09703d235a264b830"
        "7c1757e4335a63a043b88d260b6d6a2b"
    ),
}


class PolicyError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


NO_REDIRECT_OPENER: Any = urllib.request.build_opener(NoRedirectHandler())


def api_request(
    endpoint: str,
    *,
    accept: str = "application/vnd.github+json",
    administration: bool = False,
) -> tuple[bytes, str | None]:
    url = endpoint if endpoint.startswith("https://") else f"https://api.github.com/{endpoint.lstrip('/')}"
    parsed = urllib.parse.urlparse(url)
    require(parsed.scheme == "https" and parsed.hostname == "api.github.com", "refusing non-GitHub API URL")
    token_name = "CODESTRA_ORCHESTRATOR_TOKEN" if administration else "GH_TOKEN"
    token = os.environ.get(token_name, "")
    require(bool(token), f"{token_name} is required for repository policy evidence")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with NO_REDIRECT_OPENER.open(request, timeout=30) as response:
            return response.read(), response.headers.get("Link")
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise PolicyError("unexpected redirect from GitHub API") from error
        raise


def validate_artifact_storage_url(location: str) -> str:
    parsed = urllib.parse.urlparse(location)
    hostname = parsed.hostname or ""
    allowed_host = (
        hostname.endswith(".blob.core.windows.net")
        or hostname.endswith(".actions.githubusercontent.com")
        or hostname in {"objects.githubusercontent.com", "github-releases.githubusercontent.com"}
    )
    require(
        parsed.scheme == "https"
        and allowed_host
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
        and bool(parsed.path)
        and not parsed.fragment,
        "artifact redirect target is not an approved HTTPS storage URL",
    )
    return location


def download_artifact_archive(endpoint: str) -> bytes:
    url = endpoint if endpoint.startswith("https://") else f"https://api.github.com/{endpoint.lstrip('/')}"
    parsed = urllib.parse.urlparse(url)
    require(parsed.scheme == "https" and parsed.hostname == "api.github.com", "refusing non-GitHub artifact API URL")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with NO_REDIRECT_OPENER.open(request, timeout=30):
            raise PolicyError("artifact API did not redirect to signed storage")
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location", "")
        error.close()
    storage_url = validate_artifact_storage_url(location)
    storage_request = urllib.request.Request(storage_url, headers={"Accept": "application/zip"})
    with NO_REDIRECT_OPENER.open(storage_request, timeout=30) as response:
        archive = response.read(10_000_001)
    require(len(archive) <= 10_000_000, "prior artifact archive exceeds size limit")
    return archive


def next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        fields = [field.strip() for field in part.split(";")]
        if len(fields) >= 2 and 'rel="next"' in fields[1:]:
            require(fields[0].startswith("<") and fields[0].endswith(">"), "invalid pagination link")
            return fields[0][1:-1]
    return None


def api_json(endpoint: str, *, administration: bool = False) -> Any:
    raw, _ = api_request(endpoint, administration=administration)
    return json.loads(raw)


def api_pages(endpoint: str, *, administration: bool = False) -> list[Any]:
    pages: list[Any] = []
    seen: set[str] = set()
    current: str | None = endpoint
    while current is not None:
        require(current not in seen, "GitHub API pagination loop detected")
        seen.add(current)
        raw, link = api_request(current, administration=administration)
        pages.append(json.loads(raw))
        current = next_link(link)
    return pages


def bind_required_check(bindings: dict[str, int], name: object, app_id: object, expected_app_id: int) -> None:
    if not isinstance(name, str) or not name:
        raise PolicyError("invalid required check name")
    if not isinstance(app_id, int):
        raise PolicyError(f"required check {name} has no GitHub App binding")
    require(app_id == expected_app_id, f"required check {name} is not bound to the expected GitHub App")
    require(bindings.get(name) in (None, app_id), f"conflicting app bindings for required check {name}")
    bindings[name] = app_id


def required_check_bindings(branch: dict[str, Any], rules_pages: list[Any], expected_app_id: int) -> tuple[list[str], dict[str, int]]:
    policy = branch.get("protection", {}).get("required_status_checks", {})
    contexts = policy.get("contexts", [])
    checks = policy.get("checks", [])
    require(isinstance(contexts, list) and all(isinstance(name, str) and name for name in contexts), "invalid branch required check contexts")
    require(isinstance(checks, list), "invalid branch required check bindings")
    bindings: dict[str, int] = {}
    for item in checks:
        require(isinstance(item, dict), "invalid branch required check binding")
        bind_required_check(bindings, item.get("context"), item.get("app_id"), expected_app_id)
    ruleset_names: set[str] = set()
    for page in rules_pages:
        require(isinstance(page, list), "effective branch rules page is invalid")
        for rule in page:
            if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
                ruleset_checks = rule.get("parameters", {}).get("required_status_checks", [])
                require(isinstance(ruleset_checks, list), "invalid ruleset required checks")
                for item in ruleset_checks:
                    require(isinstance(item, dict), "invalid ruleset required check binding")
                    name = item.get("context")
                    bind_required_check(bindings, name, item.get("integration_id"), expected_app_id)
                    ruleset_names.add(name)
    names = sorted(set(contexts) | ruleset_names)
    require(bool(names), "protected branch has no required checks")
    unbound = sorted(set(names) - set(bindings))
    require(not unbound, f"required checks are not app-bound by branch protection: {unbound}")
    return names, bindings


def head_applicable_required_checks(
    repository: str,
    branch_checks: list[str],
    contract_checks: list[str],
) -> list[str]:
    """Bind the contract to the complete effective branch policy."""

    branch_set = set(branch_checks)
    contract_set = set(contract_checks)
    pr_only = set(PR_ONLY_REQUIRED_CHECKS.get(repository, frozenset()))
    require(
        pr_only <= branch_set,
        "pinned PR-only required checks are absent from branch protection",
    )
    head_checks = branch_set - pr_only
    require(
        contract_set == head_checks,
        "contract exact-head checks do not match effective branch protection",
    )
    return sorted(head_checks)


def latest_check_conclusions(check_pages: list[Any]) -> dict[tuple[str, int], str | None]:
    runs: list[dict[str, Any]] = []
    for page in check_pages:
        require(isinstance(page, dict) and isinstance(page.get("check_runs"), list), "check-runs page is invalid")
        runs.extend(page["check_runs"])
    require(
        all(isinstance(item, dict) and isinstance(item.get("id"), int) for item in runs),
        "check run identity is invalid",
    )
    latest: dict[tuple[str, int], str | None] = {}
    for item in sorted(runs, key=lambda row: row["id"]):
        name = item.get("name")
        app_id = item.get("app", {}).get("id")
        if isinstance(name, str) and isinstance(app_id, int):
            latest[(name, app_id)] = item.get("conclusion")
    return latest


def action_run_and_job_ids(details_url: object, repository: str) -> tuple[int, int] | None:
    if not isinstance(details_url, str):
        return None
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/actions/runs/(\d+)/job/(\d+)",
        details_url,
    )
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def workflow_bound_check_conclusions(
    check_pages: list[Any],
    repository: str,
    source_sha: str,
    required_checks: list[str],
    bindings: dict[str, int],
    workflow_runs: dict[int, dict[str, Any]],
    jobs: dict[int, dict[str, Any]],
    protected_branch: str,
) -> dict[tuple[str, int], str | None]:
    expected = EXPECTED_CHECK_WORKFLOWS.get(repository)
    if not isinstance(expected, dict):
        raise PolicyError("required check workflow policy is missing")
    require(
        set(expected) >= set(required_checks),
        "required check workflow policy is incomplete",
    )
    candidates: dict[str, list[tuple[int, int, int, str | None]]] = {
        name: [] for name in required_checks
    }
    runs: list[dict[str, Any]] = []
    for page in check_pages:
        require(
            isinstance(page, dict) and isinstance(page.get("check_runs"), list),
            "check-runs page is invalid",
        )
        runs.extend(page["check_runs"])
    for item in runs:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        app_id = item.get("app", {}).get("id")
        if name not in candidates or app_id != bindings.get(str(name)):
            continue
        identities = action_run_and_job_ids(item.get("details_url"), repository)
        if identities is None:
            continue
        run_id, job_id = identities
        run = workflow_runs.get(run_id)
        job = jobs.get(job_id)
        if not isinstance(run, dict) or not isinstance(job, dict):
            continue
        if run.get("path") != expected[name]:
            # A same-name job from a different Actions workflow is not an
            # authoritative instance of the required check.
            continue
        require(
            run.get("head_sha") == source_sha
            and run.get("head_branch") == protected_branch
            and job.get("head_sha") == source_sha
            and job.get("run_id") == run_id
            and job.get("id") == job_id
            and job.get("name") == name,
            f"required check workflow identity is invalid: {name}",
        )
        allowed_events = (
            {"push", "workflow_dispatch"}
            if repository == CONTROLLER_REPOSITORY
            else {"push"}
        )
        require(
            run.get("event") in allowed_events,
            f"required check did not originate from the protected branch event: {name}",
        )
        attempt = job.get("run_attempt")
        check_id = item.get("id")
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt <= 0
            or not isinstance(check_id, int)
        ):
            raise PolicyError(f"required check job identity is invalid: {name}")
        candidates[name].append(
            (run_id, attempt, check_id, item.get("conclusion"))
        )
    latest: dict[tuple[str, int], str | None] = {}
    for name, values in candidates.items():
        if not values:
            continue
        newest = max(values, key=lambda item: item[2])
        latest_run, latest_attempt = newest[:2]
        current = [
            item
            for item in values
            if item[0] == latest_run and item[1] == latest_attempt
        ]
        require(
            len(current) == 1,
            f"required check has duplicate jobs in its authoritative workflow: {name}",
        )
        latest[(name, bindings[name])] = current[0][3]
    return latest


def load_workflow_bound_check_conclusions(
    repository: str,
    source_sha: str,
    required_checks: list[str],
    bindings: dict[str, int],
    protected_branch: str,
    *,
    administration: bool = False,
) -> dict[tuple[str, int], str | None]:
    pages = api_pages(
        f"repos/{repository}/commits/{source_sha}/check-runs?per_page=100",
        administration=administration,
    )
    workflow_runs: dict[int, dict[str, Any]] = {}
    jobs: dict[int, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            continue
        for item in page["check_runs"]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            app_id = item.get("app", {}).get("id")
            if name not in required_checks or app_id != bindings.get(str(name)):
                continue
            identities = action_run_and_job_ids(item.get("details_url"), repository)
            if identities is None:
                continue
            run_id, job_id = identities
            if run_id not in workflow_runs:
                workflow_runs[run_id] = api_json(
                    f"repos/{repository}/actions/runs/{run_id}",
                    administration=administration,
                )
            if job_id not in jobs:
                jobs[job_id] = api_json(
                    f"repos/{repository}/actions/jobs/{job_id}",
                    administration=administration,
                )
    return workflow_bound_check_conclusions(
        pages,
        repository,
        source_sha,
        required_checks,
        bindings,
        workflow_runs,
        jobs,
        protected_branch,
    )


def validate_workflow_definition_bytes(
    repository: str,
    source_sha: str,
    path: str,
    raw: bytes,
) -> None:
    expected = EXPECTED_CHECK_WORKFLOW_SHA256.get(repository, {}).get(path)
    require(
        isinstance(expected, str) and DIGEST.fullmatch(expected) is not None,
        f"required check workflow digest policy is missing: {repository}:{path}",
    )
    require(
        hashlib.sha256(raw).hexdigest() == expected,
        f"required check workflow definition drift: {repository}:{path}",
    )
    validate_workflow_action_references(repository, source_sha, path, raw)


def validate_local_action_dockerfile(
    repository: str,
    source_sha: str,
    action_path: str,
) -> None:
    dockerfile_path = (Path(action_path).parent / "Dockerfile").as_posix()
    raw = repository_file_bytes(repository, source_sha, dockerfile_path)
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PolicyError(
            f"required check local-action Dockerfile is not UTF-8: {dockerfile_path}"
        ) from error

    logical_lines: list[str] = []
    pending = ""
    for physical_line in source.splitlines():
        line = physical_line.rstrip()
        pending = f"{pending}{line.lstrip()}" if pending else line
        if pending.endswith("\\"):
            pending = f"{pending[:-1].rstrip()} "
            continue
        logical_lines.append(pending)
        pending = ""
    require(
        not pending,
        f"required check local-action Dockerfile has an incomplete instruction: {dockerfile_path}",
    )

    stages: set[str] = set()
    from_count = 0
    for logical_line in logical_lines:
        stripped = logical_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"(?i)^FROM\s+(.+)$", stripped)
        if match is None:
            continue
        from_count += 1
        arguments = match.group(1).split()
        while arguments and arguments[0].startswith("--"):
            arguments.pop(0)
        require(
            bool(arguments),
            f"required check local-action Dockerfile has an invalid FROM: {dockerfile_path}",
        )
        base = arguments.pop(0)
        normalized_base = base.lower()
        require(
            "$" not in base
            and (
                normalized_base == "scratch"
                or normalized_base in stages
                or re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", base) is not None
            ),
            f"required check local-action Dockerfile uses a mutable base: {dockerfile_path}:{base}",
        )
        if len(arguments) >= 2 and arguments[0].lower() == "as":
            stage = arguments[1].lower()
            require(
                re.fullmatch(r"[a-z0-9_.-]+", stage) is not None,
                f"required check local-action Dockerfile has an invalid stage: {dockerfile_path}",
            )
            stages.add(stage)
    require(
        from_count > 0,
        f"required check local-action Dockerfile has no FROM instruction: {dockerfile_path}",
    )


def validate_workflow_action_references(
    repository: str,
    source_sha: str,
    path: str,
    raw: bytes,
    seen: frozenset[str] = frozenset(),
) -> None:
    require(path not in seen, f"required check local action cycle: {path}")
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PolicyError(f"required check workflow is not UTF-8: {path}") from error
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError as error:
        raise PolicyError("pinned workflow parser is unavailable") from error
    try:
        document = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise PolicyError(f"required check workflow YAML is invalid: {path}") from error
    require(isinstance(document, dict), f"required check workflow is invalid: {path}")
    runs = document.get("runs")
    if isinstance(runs, dict) and runs.get("using") == "docker":
        image = runs.get("image")
        if not isinstance(image, str) or not image:
            raise PolicyError(
                f"required check local container action image is invalid: {path}"
            )
        if image == "Dockerfile":
            validate_local_action_dockerfile(repository, source_sha, path)
        else:
            require(
                re.fullmatch(
                    r"docker://[^\s@]+@sha256:[0-9a-f]{64}",
                    image,
                )
                is not None,
                f"required check local container action image is mutable: {path}:{image}",
            )
    references: list[str] = []

    def collect_references(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "uses":
                    require(
                        isinstance(child, str) and bool(child),
                        f"required check action reference is invalid: {path}",
                    )
                    references.append(child)
                else:
                    collect_references(child)
        elif isinstance(value, list):
            for child in value:
                collect_references(child)

    collect_references(document)
    for reference in references:
        reference = reference.split(" #", 1)[0].strip()
        if reference.startswith("./"):
            relative = reference[2:].rstrip("/")
            require(
                bool(relative)
                and not relative.startswith("/")
                and all(part not in {"", ".", ".."} for part in relative.split("/")),
                f"required check local action path is invalid: {path}:{reference}",
            )
            candidates = (
                [relative]
                if relative.endswith((".yml", ".yaml"))
                else [f"{relative}/action.yml", f"{relative}/action.yaml"]
            )
            nested_raw: bytes | None = None
            nested_path = ""
            for candidate in candidates:
                try:
                    nested_raw = repository_file_bytes(
                        repository,
                        source_sha,
                        candidate,
                    )
                except urllib.error.HTTPError as error:
                    if error.code != 404 or candidate == candidates[-1]:
                        raise
                    continue
                nested_path = candidate
                break
            require(
                nested_raw is not None and bool(nested_path),
                f"required check local action is missing: {path}:{reference}",
            )
            validate_workflow_action_references(
                repository,
                source_sha,
                nested_path,
                cast(bytes, nested_raw),
                seen | {path},
            )
            continue
        if reference.startswith("docker://"):
            require(
                re.fullmatch(
                    r"docker://[^\s@]+@sha256:[0-9a-f]{64}",
                    reference,
                )
                is not None,
                f"required check uses a mutable container action: {path}:{reference}",
            )
            continue
        require(
            re.fullmatch(r"[^\s@]+@[0-9a-f]{40}", reference) is not None,
            f"required check uses a mutable external action: {path}:{reference}",
        )


def validate_workflow_executable_bytes(
    repository: str,
    workflow_path: str,
    executable_path: str,
    raw: bytes,
) -> None:
    expected = EXPECTED_CHECK_WORKFLOW_EXECUTABLE_SHA256.get(repository, {}).get(
        workflow_path,
        {},
    ).get(executable_path)
    require(
        isinstance(expected, str) and DIGEST.fullmatch(expected) is not None,
        f"required check executable digest policy is missing: {repository}:{executable_path}",
    )
    require(
        hashlib.sha256(raw).hexdigest() == expected,
        f"required check executable drift: {repository}:{executable_path}",
    )


def is_exact_local_repository_source(
    repository: str,
    source_sha: str,
    local_repository: str | None,
    checkout_sha: str,
) -> bool:
    return repository == local_repository and source_sha == checkout_sha


def exact_local_repository_file_bytes(
    repository: str,
    source_sha: str,
    path: str,
) -> bytes | None:
    local_repository = os.environ.get("GITHUB_REPOSITORY")
    if repository != local_repository:
        return None
    checkout_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if not is_exact_local_repository_source(
        repository,
        source_sha,
        local_repository,
        checkout_sha,
    ):
        return None
    local_path = Path(path)
    require(
        local_path.is_file() and not local_path.is_symlink(),
        f"required source file is missing or unsafe: {path}",
    )
    return local_path.read_bytes()


def repository_file_bytes(
    repository: str,
    source_sha: str,
    path: str,
    *,
    administration: bool = False,
) -> bytes:
    require(SHA.fullmatch(source_sha) is not None, "repository file source SHA is invalid")
    local_bytes = exact_local_repository_file_bytes(repository, source_sha, path)
    if local_bytes is not None:
        return local_bytes
    encoded_path = urllib.parse.quote(path, safe="/")
    value = api_json(
        f"repos/{repository}/contents/{encoded_path}?ref={source_sha}",
        administration=administration,
    )
    require(
        isinstance(value, dict)
        and value.get("type") == "file"
        and value.get("encoding") == "base64"
        and isinstance(value.get("content"), str),
        f"required source file evidence is invalid: {repository}:{path}",
    )
    try:
        return base64.b64decode("".join(value["content"].split()), validate=True)
    except ValueError as error:
        raise PolicyError(
            f"required source file encoding is invalid: {repository}:{path}"
        ) from error
def required_check_source_paths(repository: str) -> set[str]:
    workflow_bindings = EXPECTED_CHECK_WORKFLOW_SHA256.get(repository)
    executable_bindings = EXPECTED_CHECK_WORKFLOW_EXECUTABLE_SHA256.get(
        repository,
        {},
    )
    require(
        isinstance(workflow_bindings, dict) and bool(workflow_bindings),
        f"required workflow source policy is missing: {repository}",
    )
    require(
        isinstance(executable_bindings, dict),
        f"required executable source policy is invalid: {repository}",
    )
    validated_workflows = cast(dict[str, str], workflow_bindings)
    validated_executables = cast(
        dict[str, dict[str, str]],
        executable_bindings,
    )
    paths = set(validated_workflows)
    for workflow, bindings in validated_executables.items():
        require(
            workflow in validated_workflows and isinstance(bindings, dict),
            f"required executable source policy is detached: {repository}:{workflow}",
        )
        paths.update(bindings)
    require(
        RELEASE_VALIDATOR_SOURCE_PATH not in paths,
        "release validator cannot bind its own source closure",
    )
    return paths


def source_closure_fingerprint(
    entries: list[dict[str, Any]],
    repository: str,
) -> str:
    """Fingerprint the complete exact-source tree used by required checks.

    The release validator excludes itself to avoid a literal hash cycle. The
    controller's reviewed candidate JSON is also excluded because its
    controller source binding is an already-reviewed policy-base ancestor.
    Every other blob/commit is included, so an omitted direct or transitive
    check executable cannot leave the closure unchanged.
    """

    records: dict[str, bytes] = {}
    for entry in entries:
        require(isinstance(entry, dict), "required source tree entry is invalid")
        path = entry.get("path")
        mode = entry.get("mode")
        kind = entry.get("type")
        sha = entry.get("sha")
        require(
            isinstance(path, str)
            and path != ""
            and "\0" not in path
            and "\n" not in path
            and isinstance(mode, str)
            and mode in {"100644", "100755", "120000", "160000"}
            and kind in {"blob", "commit"}
            and isinstance(sha, str)
            and SHA.fullmatch(sha) is not None,
            "required source tree entry is invalid",
        )
        validated_path = cast(str, path)
        if validated_path == RELEASE_VALIDATOR_SOURCE_PATH or (
            repository == CONTROLLER_REPOSITORY
            and validated_path.startswith("config/releases/")
            and validated_path.endswith(".json")
        ):
            continue
        require(validated_path not in records, "required source tree contains duplicate paths")
        records[validated_path] = f"{mode}\0{kind}\0{validated_path}\0{sha}\n".encode()
    require(bool(records), "required source tree is empty")
    return hashlib.sha256(b"".join(records[path] for path in sorted(records))).hexdigest()


def local_source_tree_entries(source_sha: str) -> list[dict[str, Any]]:
    raw = subprocess.check_output(
        ["git", "ls-tree", "-r", "-z", source_sha],
    )
    entries: list[dict[str, Any]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode, kind, sha = metadata.decode("ascii").split()
            path = encoded_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise PolicyError("local required source tree is invalid") from error
        entries.append({"mode": mode, "type": kind, "sha": sha, "path": path})
    return entries


def validate_required_check_source_closure(
    repository: str,
    source_sha: str,
    *,
    administration: bool = False,
) -> None:
    expected = EXPECTED_REQUIRED_CHECK_SOURCE_CLOSURE_SHA256.get(repository)
    require(
        isinstance(expected, str) and DIGEST.fullmatch(expected) is not None,
        f"required source closure policy is missing: {repository}",
    )
    local_repository = os.environ.get("GITHUB_REPOSITORY")
    checkout_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if is_exact_local_repository_source(
        repository,
        source_sha,
        local_repository,
        checkout_sha,
    ):
        entries = local_source_tree_entries(source_sha)
    else:
        commit = api_json(
            f"repos/{repository}/git/commits/{source_sha}",
            administration=administration,
        )
        require(
            isinstance(commit, dict)
            and commit.get("sha") == source_sha
            and isinstance(commit.get("tree"), dict)
            and isinstance(commit["tree"].get("sha"), str)
            and SHA.fullmatch(commit["tree"]["sha"]) is not None,
            f"required source commit tree is invalid: {repository}",
        )
        tree = api_json(
            f"repos/{repository}/git/trees/{commit['tree']['sha']}?recursive=1",
            administration=administration,
        )
        require(
            isinstance(tree, dict)
            and tree.get("truncated") is False
            and isinstance(tree.get("tree"), list),
            f"required source tree is incomplete: {repository}",
        )
        entries = [
            entry
            for entry in tree["tree"]
            if isinstance(entry, dict) and entry.get("type") != "tree"
        ]
    observed = source_closure_fingerprint(entries, repository)
    require(observed == expected, f"required source closure drift: {repository}")


def validate_required_check_workflow_definitions(
    repository: str,
    source_sha: str,
    required_checks: list[str],
    *,
    administration: bool = False,
) -> None:
    validate_required_check_source_closure(
        repository,
        source_sha,
        administration=administration,
    )
    paths = EXPECTED_CHECK_WORKFLOWS.get(repository)
    if not isinstance(paths, dict):
        raise PolicyError("required check workflow policy is missing")
    required_paths: set[str] = set()
    for name in required_checks:
        workflow_path = paths.get(name)
        if not isinstance(workflow_path, str):
            raise PolicyError("required check workflow policy is incomplete")
        required_paths.add(workflow_path)
    for path in sorted(required_paths):
        raw = repository_file_bytes(
            repository,
            source_sha,
            path,
            administration=administration,
        )
        validate_workflow_definition_bytes(repository, source_sha, path, raw)
        executable_policy = EXPECTED_CHECK_WORKFLOW_EXECUTABLE_SHA256.get(
            repository,
            {},
        ).get(path, {})
        if path == ".github/workflows/production-orchestrator-contract.yml":
            require(
                bool(executable_policy),
                f"required check executable digest policy is missing: {repository}:{path}",
            )
        for executable_path, expected_hash in executable_policy.items():
            require(
                DIGEST.fullmatch(expected_hash) is not None,
                f"required check executable digest is invalid: {repository}:{executable_path}",
            )
            executable = repository_file_bytes(
                repository,
                source_sha,
                executable_path,
                administration=administration,
            )
            validate_workflow_executable_bytes(
                repository,
                path,
                executable_path,
                executable,
            )


def validate_environment_document(value: object, environment: str) -> None:
    if not isinstance(value, dict):
        raise PolicyError("protected environment is missing")
    require(value.get("name") == environment, "protected environment is missing")
    rules = value.get("protection_rules")
    if not isinstance(rules, list):
        raise PolicyError("protected environment rules are invalid")
    reviewer_rules = [
        item
        for item in rules
        if isinstance(item, dict) and item.get("type") == "required_reviewers"
    ]
    require(len(reviewer_rules) == 1, "protected environment must have one required-reviewer rule")
    reviewer_rule = reviewer_rules[0]
    reviewers = reviewer_rule.get("reviewers")
    if not isinstance(reviewers, list) or not reviewers:
        raise PolicyError("protected environment has no required reviewers")
    reviewer_identities: set[tuple[str, int]] = set()
    for item in reviewers:
        reviewer_type = item.get("type") if isinstance(item, dict) else None
        reviewer = item.get("reviewer") if isinstance(item, dict) else None
        reviewer_id = reviewer.get("id") if isinstance(reviewer, dict) else None
        if (
            reviewer_type not in {"User", "Team"}
            or not isinstance(reviewer_id, int)
            or isinstance(reviewer_id, bool)
            or reviewer_id <= 0
        ):
            raise PolicyError("protected environment reviewer identity is invalid")
        reviewer_identities.add((str(reviewer_type), reviewer_id))
    require(
        len(reviewer_identities) == len(reviewers),
        "protected environment contains duplicate reviewers",
    )
    require(
        reviewer_identities == {("User", INDEPENDENT_REVIEWER_ID)},
        "protected environment approved reviewer identity drift",
    )
    require(reviewer_rule.get("prevent_self_review") is True, "protected environment permits self-review")
    require(value.get("can_admins_bypass") is False, "protected environment permits administrator bypass")
    branch_policy = value.get("deployment_branch_policy")
    require(
        isinstance(branch_policy, dict)
        and branch_policy.get("protected_branches") is True
        and branch_policy.get("custom_branch_policies") is False,
        "protected environment is not restricted to protected branches",
    )


def validate_environment_protection(repository: str, environment: str) -> None:
    encoded = urllib.parse.quote(environment, safe="")
    value = api_json(
        f"repos/{repository}/environments/{encoded}",
        administration=True,
    )
    validate_environment_document(value, environment)


def validate_repository_gates(
    contract: dict[str, Any],
    source_sha: str,
    phase: str,
    environment: str,
) -> tuple[list[str], dict[str, int]]:
    repository = os.environ["GITHUB_REPOSITORY"]
    branch_name = contract.get("default_branch")
    repository_data = api_json(f"repos/{repository}")
    require(repository_data.get("id") == contract.get("repository_id"), "stable repository ID mismatch")
    require(repository_data.get("default_branch") == branch_name, "default branch drift")
    require(repository_data.get("archived") is False and repository_data.get("disabled") is False, "repository unavailable")
    branch = api_json(f"repos/{repository}/branches/{branch_name}")
    require(branch.get("protected") is True, "default branch must be protected")
    require(branch.get("commit", {}).get("sha") == source_sha, "source SHA is not current protected branch head")
    if contract.get("require_verified_commit") is True:
        commit = api_json(f"repos/{repository}/commits/{source_sha}")
        require(commit.get("commit", {}).get("verification", {}).get("verified") is True, "exact source commit is not GitHub-verified")
    contract_checks = contract.get("required_checks")
    if not isinstance(contract_checks, list):
        raise PolicyError("invalid required_checks")
    require(all(isinstance(item, str) and item for item in contract_checks), "invalid required_checks")
    expected_app_id = contract.get("required_check_app_id")
    if not isinstance(expected_app_id, int) or expected_app_id <= 0:
        raise PolicyError("required check app ID is invalid")
    branch_checks, bindings = required_check_bindings(
        branch,
        api_pages(
            f"repos/{repository}/rules/branches/{branch_name}?per_page=100",
            administration=True,
        ),
        expected_app_id,
    )
    required_checks = head_applicable_required_checks(
        repository,
        branch_checks,
        contract_checks,
    )
    validate_required_check_workflow_definitions(
        repository,
        source_sha,
        required_checks,
    )
    latest = load_workflow_bound_check_conclusions(
        repository,
        source_sha,
        required_checks,
        bindings,
        str(branch_name),
    )
    missing = [name for name in required_checks if latest.get((name, bindings[name])) != "success"]
    require(not missing, f"required exact-head checks are not successful from the bound app: {missing}")
    if phase != "plan":
        require(bool(environmen{žØ§z\m®éÜj×ß^4é¼­zÆæBÒ²&6÷6–vâ"Â'fW&–g’"Â¦6öÖÖöåÐ¢–bW†7E÷6÷W&6S ¢6öÖÖæBæW‡FVæB…²"ÒÖææ÷FF–öç2"Âb&6öFW7G&ç6÷W&6U÷6†×·6÷W&6U÷6†Ò%Ò¢7V'&ö6W72ç'Vâ…²¦6öÖÖæBÂ–ÖvUÒÂ6†V6³ÕG'VRÂ7FF÷WC×7V'&ö6W72äDUdåTÄÂ¢–b&WV—&U÷6&öÓ ¢7V'&ö6W72ç'Vâ…²&6÷6–vâ"Â'fW&–g’ÖGFW7FF–öâ"Â"Ò×G—R"Â'7G†§6öâ"Â¦6öÖÖöâÂ–ÖvUÒÂ6†V6³ÕG'VRÂ7FF÷WC×7V'&ö6W72äDUdåTÄÂ¢–b&WV—&U÷&÷fVææ6S ¢&÷fVææ6RÒ7V'&ö6W72æ6†V6µö÷WGWB€¢²&6÷6–vâ"Â'fW&–g’ÖGFW7FF–öâ"Â"Ò×G—R"Â'6Ç6&÷fVææ6S"Â¦6öÖÖöâÂ–ÖvUÒÀ¢FW‡CÕG'VRÀ¢¢7FFVÖVçG2Ò6÷6–vå÷7FFVÖVçG2‡&÷fVææ6R¢6÷W&6Uö6öÖÖ—G2Ò°¢6öÖÖ—@¢f÷"7FFVÖVçB–â7FFVÖVçG0¢f÷"FWVæFVæ7’–â7FFVÖVçBævWB‚'&VF–6FR"Â·Ò’ævWB‚&'V–ÆDFVf–æ—F–öâ"Â·Ò’ævWB‚'&W6öÇfVDFWVæFVæ6–W2"ÂµÒ¢–b—6–ç7Fæ6R†FWVæFVæ7’ÂF–7B¢f÷"6öÖÖ—B–â¶FWVæFVæ7’ævWB‚&F–vW7B"Â·Ò’ævWB‚&v—D6öÖÖ—B"•Ð¢–b—6–ç7Fæ6R†6öÖÖ—BÂ7G"’æB4„ægVÆÆÖF6‚†6öÖÖ—B¢Ð¢–bW†7E÷6÷W&6S ¢&WV—&R€¢6÷W&6U÷6†–â6÷W&6Uö6öÖÖ—G2À¢b'6–væVB&÷fVææ6RFöW2æ÷B&–æB6÷W&6R4„f÷"¶–ÖvWÒ"À¢¢VÇ6S ¢&WV—&R€¢&ööÂ‡6÷W&6Uö6öÖÖ—G2’À¢b'&öÆÆ&6²&÷fVææ6RFöW2æ÷B&–æB6÷W&6R4„f÷"¶–ÖvWÒ"À¢  ¦FVbÖ–â‚’Óâ–çC ¢†6RÒ÷2æVçf—&öå²%„4R%Ð¢6÷W&6U÷6†Ò÷2æVçf—&öå²%4õU$4Uõ4„%Ð¢&VÆV6Uö–BÒ÷2æVçf—&öå²%$TÄT4Uô”B%Ð¢6æF–FFU÷6†#SbÒ÷2æVçf—&öå²$4äD”DDUõ4„#Sb%Ð¢&–÷%ö†6‚Ò÷2æVçf—&öå²%$”õ%ôUd”DTä4Uõ4„#Sb%Ð¢&–÷%÷'Vå÷FW‡BÒ÷2æVçf—&öå²%$”õ%ôUd”DTä4Uõ%Tåô”B%Ð¢&WV—&R‡†6R–â4ôäd•$ÔD”ôå2Â'Vç7W÷'FVB&VÆV6R†6R"¢&WV—&R†÷2æVçf—&öå²$4ôäd•$ÔD”ôâ%ÒÓÒ4ôäd•$ÔD”ôå5·†6UÒÂ&6öæf—&ÖF–öâÖ—6ÖF6‚"¢&WV—&R…4„ægVÆÆÖF6‚‡6÷W&6U÷6†’—2æ÷BæöæRæB6÷W&6U÷6†Ò#"¢CÂ'6÷W&6U÷6†×W7B&Ræöç¦W&òÆ÷vW&66RCÖ†W‚"¢&WV—&R„D”tU5BægVÆÆÖF6‚†6æF–FFU÷6†#Sb’—2æ÷BæöæRæB6æF–FFU÷6†#SbÒ¤U$ócBÂ&6æF–FFU÷6†#Sb×W7B&Ræöç¦W&òÆ÷vW&66RcBÖ†W‚"¢&WV—&R„D”tU5BægVÆÆÖF6‚‡&–÷%ö†6‚’—2æ÷BæöæRÂ'&–÷"Wf–FVæ6R×W7B&RÆ÷vW&66RcBÖ†W‚"¢&WV—&R‡&–÷%÷'Vå÷FW‡Bæ—6F–v—B‚’Â'&–÷"Wf–FVæ6R'Vâ”B×W7B&RFV6–ÖÂ"¢&WV—&R…$TÄT4RægVÆÆÖF6‚‡&VÆV6Uö–B’—2æ÷BæöæRÂ&–çfÆ–B&VÆV6Uö–B"¢&–÷%÷'Våö–BÒ–çB‡&–÷%÷'Vå÷FW‡B¢–b†6RÓÒ'Æâ# ¢&WV—&R‡&–÷%ö†6‚ÓÒ¤U$ócBæB&–÷%÷'Våö–BÓÒÂ'Æâ×W7BW6R¦W&ò&–÷"Wf–FVæ6R"¢VÇ6S ¢&WV—&R‡&–÷%ö†6‚Ò¤U$ócBæB&–÷%÷'Våö–BâÂb'·†6WÒ&WV—&W2&–÷"×†6RWf–FVæ6R"¢&WV—&R‡&–÷%÷'Vå÷FW‡BÒ÷2æVçf—&öå²$t•D…T%õ%Tåô”B%ÒÂ'&–÷"Wf–FVæ6R6ææ÷B6öÖRg&öÒF†R7W'&VçB'Vâ" ¢&WV—&R„4ôåE$5EõD‚æ—5öf–ÆR‚’æBæ÷B4ôåE$5EõD‚æ—5÷7–ÖÆ–æ²‚’Â'&VÆV6R6öçG&7B—2Ö—76–ær÷"Vç6fR"¢6öçG&7Eö'—FW2Ò4ôåE$5EõD‚ç&VEö'—FW2‚¢6öçG&7BÒ§6öâæÆöG2†6öçG&7Eö'—FW2¢&W÷6—F÷'’Ò÷2æVçf—&öå²$t•D…T%õ$Uõ4•Dõ%’%Ð¢&WV—&R†—6–ç7Fæ6R†6öçG&7BÂF–7B’æB6öçG&7BævWB‚'66†VÖ÷fW'6–öâ"’ÓÒ44„TÔÂ'VæW‡V7FVB&VÆV6R6öçG&7B66†VÖ"¢&WV—&R†6öçG&7BævWB‚'&W÷6—F÷'’"’ÓÒ&W÷6—F÷'’Â&6öçG&7B&W÷6—F÷'’Ö—6ÖF6‚"¢&WV—&R†6öçG&7BævWB‚'&VÆV6Uö–çFVçE÷v÷&¶fÆ÷r"’ÓÒtõ$´dÄõrÂ&6öçG&7Bv÷&¶fÆ÷rÖ—6ÖF6‚"¢'&æ6…öæÖRÒ6öçG&7BævWB‚&FVfVÇEö'&æ6‚"¢&WV—&R†'&æ6…öæÖRÓÒ÷2æVçf—&öå²$t•D…T%õ$TeôäÔR%ÒæB÷2æVçf—&öå²$t•D…T%õ$Tb%ÒÓÒb'&Vg2ö†VG2÷¶'&æ6…öæÖWÒ"Â'v÷&¶fÆ÷r×W7B'Vâg&öÒF†R6öçG&7BFVfVÇB'&æ6‚"¢6†V6¶÷WE÷6†Ò7V'&ö6W72æ6†V6µö÷WGWB…²&v—B"Â'&Wb×'6R"Â$„TB%ÒÂFW‡CÕG'VR’ç7G&—‚¢&WV—&R†6†V6¶÷WE÷6†ÓÒ6÷W&6U÷6†Â&6†V6¶÷WBFöW2æ÷BÖF6‚6÷W&6U÷6†"¢&WV—&R†æ÷B7V'&ö6W72æ6†V6µö÷WGWB…²&v—B"Â'7FGW2"Â"Ò×÷&6VÆ–â%ÒÂFW‡CÕG'VR’ç7G&—‚’Â'v÷&·76R—2F—'G’" ¢7W÷'FVBÒ6öçG&7BævWB‚'7W÷'FVE÷†6W2"¢&WV—&R†—6–ç7Fæ6R‡7W÷'FVBÂÆ—7B’æB†6R–â7W÷'FVBÂb'†6R·†6WÒ—2æ÷B7W÷'FVB"¢FWÆ÷–ÖVçEöWF†÷&—G’Ò6öçG&7BævWB‚&FWÆ÷–ÖVçEöWF†÷&—G’"’—2G'VP¢–b†6RÒ'Æâ# ¢&WV—&R†FWÆ÷–ÖVçEöWF†÷&—G’Â'&W÷6—F÷'’—2æ÷BFWÆ÷–ÖVçBWF†÷&—G’"¢&Æö6¶W'2ÒfÆ–FFU÷†6Uö&Æö6¶W'2†6öçG&7BævWB‚&&Æö6¶W'2"’Â†6R¢Vçf—&öæÖVçBÒ" ¢–b†6RÒ'Æâ# ¢Vçf—&öæÖVçBÒ6öçG&7BævWB‚&Vçf—&öæÖVçG2"Â·Ò’ævWB‡†6RÂ""¢W‡V7FVEöVçf—&öæÖVçBÒ²'7Fv–ær#¢'7Fv–ær×&VFöæÇ’"Â&6æ'’#¢'&öGV7F–öâ×&VFöæÇ’Ö6æ'’"Â'&öGV7F–öâ#¢'&öGV7F–öâ'Õ·†6UÐ¢&WV—&R†Vçf—&öæÖVçBÓÒW‡V7FVEöVçf—&öæÖVçBÂb'·†6WÒ&÷FV7FVBVçf—&öæÖVçBÖ—6ÖF6‚"¢&WV—&VEö6†V6·2Â&–æF–æw2ÒfÆ–FFU÷&W÷6—F÷'•övFW2€¢6öçG&7BÀ¢6÷W&6U÷6†À¢†6RÀ¢Vçf—&öæÖVçBÀ¢ ¢G'“ ¢–ÖvW2Ò§6öâæÆöG2†÷2æVçf—&öå²$”ÔtU5ô¥4ôâ%Ò¢&Wf–÷W5ö–ÖvW2Ò§6öâæÆöG2†÷2æVçf—&öå²%$Ud”õU5ô”ÔtU5ô¥4ôâ%Ò¢W†6WB§6öâä¥4ôäFV6öFTW'&÷"2W'&÷# ¢&—6RöÆ–7”W'&÷"†b&–ÖvR–çWB—2æ÷BfÆ–B¥4ôã¢¶W'&÷'Ò"’g&öÒW'&÷ ¢&WV—&R†—6–ç7Fæ6R†–ÖvW2ÂÆ—7B’æB—6–ç7Fæ6R‡&Wf–÷W5ö–ÖvW2ÂÆ—7B’Â&–ÖvR–çWG2×W7B&R'&—2"¢öÆ–7’Ò6öçG&7BævWB‚&'F–f7E÷öÆ–7’"¢&WV—&R†—6–ç7Fæ6R‡öÆ–7’ÂF–7B’Â&'F–f7E÷öÆ–7’×W7B&Râö&¦V7B"¢&WV—&R‡öÆ–7’ævWB‚'&WV—&UöF–vW7B"’—2G'VRÂ&F–vW7BÖöæÇ’'F–f7G2&R&WV—&VB"¢&WV—&R‡öÆ–7’ævWB‚&ÆÆ÷u÷&V'V–ÆEögFW%÷7Fv–ær"’—2fÇ6RÂ'&V'V–ÆBgFW"7Fv–ær×W7B&VÖ–âf÷&&–FFVâ"¢&WV—&R‡öÆ–7’ævWB‚&ÆÆ÷u÷&WFuögFW%÷7Fv–ær"’—2fÇ6RÂ'&WFrgFW"7Fv–ær×W7B&VÖ–âf÷&&–FFVâ"¢fÆ–FFUö–ÖvW2†–ÖvW2Â&Wf–÷W5ö–ÖvW2ÂöÆ–7’¢6öçG&7E÷6†#SbÒ†6†Æ–"ç6†#Sb†6öçG&7Eö'—FW2’æ†W†F–vW7B‚¢6öçG&öÆÆW%ö6æF–FFUö†VBÒF÷væÆöEöæE÷fÆ–FFUö6æF–FFR€¢6æF–FFU÷6†#SbÀ¢&VÆV6Uö–BÀ¢&W÷6—F÷'’À¢6÷W&6U÷6†À¢6öçG&7E÷6†#SbÀ¢–ÖvW2À¢&Wf–÷W5ö–ÖvW2À¢¢fW&–g•÷7WÇ•ö6†–â†–ÖvW2ÂöÆ–7’Â&W÷6—F÷'’Â'&æ6…öæÖRÂ6÷W&6U÷6†ÂW†7E÷6÷W&6SÕG'VR¢fW&–g•÷7WÇ•ö6†–â‡&Wf–÷W5ö–ÖvW2ÂöÆ–7’Â&W÷6—F÷'’Â'&æ6…öæÖRÂ6÷W&6U÷6†ÂW†7E÷6÷W&6SÔfÇ6R ¢6fWG’Ò6öçG&7BævWB‚'6fWG’"¢&WV—&R†—6–ç7Fæ6R‡6fWG’ÂF–7B’æB4dUE•ô´U•2ÃÒ6WB‡6fWG’’Â'6fWG’6öçG&7B—2–æ6ö×ÆWFR"¢&WV—&R†ÆÂ‡6fWG’ævWB†¶W’’—2fÇ6Rf÷"¶W’–â4dUE•ô´U•2’Â&WfW'’W‡FW&æÂöÆ—fRVffV7B×W7B&VÖ–âF—6&ÆVB"¢–b†6RÒ'Æâ# ¢&Wf–÷W5÷†6RÒ$Ud”õU5õ„4U·†6UÐ¢&–÷"ÒF÷væÆöE÷&–÷%öWf–FVæ6R‡&W÷6—F÷'’Â&–÷%÷'Våö–BÂb&6öFW7G&×&VÆV6RÖ–çFVçB×·&Wf–÷W5÷†6WÒ×·6÷W&6U÷6†Ò"Â&–÷%ö†6‚¢'VâÒ•ö§6öâ†b'&W÷2÷·&W÷6—F÷'—Òö7F–öç2÷'Vç2÷·&–÷%÷'Våö–GÒ"¢&WV—&R‡'VâævWB‚&†VE÷6†"’ÓÒ6÷W&6U÷6†Â'&–÷"Wf–FVæ6R'VâW6VBF–ffW&VçB6÷W&6R4„"¢fÆ–FFU÷&–÷"€¢&–÷"À¢°¢'66†VÖ÷fW'6–öâ#¢&6öFW7G&ææ÷&ÖÆ—¦VB×&VÆV6RÖ–çFVçBçc"À¢'&W÷6—F÷'’#¢&W÷6—F÷'’À¢'&W÷6—F÷'•ö–B#¢6öçG&7E²'&W÷6—F÷'•ö–B%ÒÀ¢'†6R#¢&Wf–÷W5÷†6RÀ¢'&VÆV6Uö–B#¢&VÆV6Uö–BÀ¢'6÷W&6U÷6†#¢6÷W&6U÷6†À¢&6æF–FFU÷6†#Sb#¢6æF–FFU÷6†#SbÀ¢&6æF–FFUö–ÖvW2#¢–ÖvW2À¢'&Wf–÷W5ö–ÖvW2#¢&Wf–÷W5ö–ÖvW2À¢''VçF–ÖUö6öçF7FVB#¢fÇ6RÀ¢'&öGV7F–öåö6†ævVB#¢fÇ6RÀ¢&W‡FW&æÅöVffV7G5öVæ&ÆVB#¢fÇ6RÀ¢'&÷FV7FVEöVçf—&öæÖVçEö&÷fVB#¢fÇ6RÀ¢'&÷FV7FVEöVçf—&öæÖVçEö¦ö%ö6ö×ÆWFVB#¢&Wf–÷W5÷†6RÒ'Æâ"À¢'7FGW2#¢%52"À¢ÒÀ¢ ¢f–æÅö6öçG&öÆÆW%ö6æF–FFUö†VBÒF÷væÆöEöæE÷fÆ–FFUö6æF–FFR€¢6æF–FFU÷6†#SbÀ¢&VÆV6Uö–BÀ¢&W÷6—F÷'’À¢6÷W&6U÷6†À¢6öçG&7E÷6†#SbÀ¢–ÖvW2À¢&Wf–÷W5ö–ÖvW2À¢¢&WV—&R€¢f–æÅö6öçG&öÆÆW%ö6æF–FFUö†VBÓÒ6öçG&öÆÆW%ö6æF–FFUö†VBÀ¢&6öçG&öÆÆW"&÷FV7FVB†VB6†ævVBGW&–ær&VÆV6RÖ–çFVçBfÆ–FF–öâ"À¢¢&WV—&VEö6†V6·2Â&–æF–æw2ÒfÆ–FFU÷&W÷6—F÷'•övFW2€¢6öçG&7BÀ¢6÷W&6U÷6†À¢†6RÀ¢Vçf—&öæÖVçBÀ¢¢÷7EövFUö6öçG&öÆÆW%ö6æF–FFUö†VBÒF÷væÆöEöæE÷fÆ–FFUö6æF–FFR€¢6æF–FFU÷6†#SbÀ¢&VÆV6Uö–BÀ¢&W÷6—F÷'’À¢6÷W&6U÷6†À¢6öçG&7E÷6†#SbÀ¢–ÖvW2À¢&Wf–÷W5ö–ÖvW2À¢¢&WV—&R€¢÷7EövFUö6öçG&öÆÆW%ö6æF–FFUö†VBÓÒ6öçG&öÆÆW%ö6æF–FFUö†VBÀ¢&6öçG&öÆÆW"&÷FV7FVB†VB6†ævVBGW&–ærf–æÂ6÷W&6RÖvFRfÆ–FF–öâ"À¢¢÷7Eö6öçG&öÆÆW%÷&WV—&VEö6†V6·2Â÷7Eö6öçG&öÆÆW%ö&–æF–æw2ÒfÆ–FFU÷&W÷6—F÷'•övFW2€¢6öçG&7BÀ¢6÷W&6U÷6†À¢†6RÀ¢Vçf—&öæÖVçBÀ¢¢&WV—&R€¢÷7Eö6öçG&öÆÆW%÷&WV—&VEö6†V6·2ÓÒ&WV—&VEö6†V6·0¢æB÷7Eö6öçG&öÆÆW%ö&–æF–æw2ÓÒ&–æF–æw2À¢'6÷W&6R&÷FV7FVB†VB÷"6†V6²öÆ–7’6†ævVBGW&–ærf–æÂ6öçG&öÆÆW"fÆ–FF–öâ"À¢ ¢Wf–FVæ6RÒ°¢'66†VÖ÷fW'6–öâ#¢&6öFW7G&ææ÷&ÖÆ—¦VB×&VÆV6RÖ–çFVçBçc"À¢'&W÷6—F÷'’#¢&W÷6—F÷'’À¢'&W÷6—F÷'•ö–B#¢6öçG&7E²'&W÷6—F÷'•ö–B%ÒÀ¢'&öÆR#¢6öçG&7E²'&öÆR%ÒÀ¢'†6R#¢†6RÀ¢'&VÆV6Uö–B#¢&VÆV6Uö–BÀ¢'6÷W&6U÷6†#¢6÷W&6U÷6†À¢&6æF–FFU÷6†#Sb#¢6æF–FFU÷6†#SbÀ¢'&–÷%öWf–FVæ6U÷6†#Sb#¢&–÷%ö†6‚À¢'&–÷%öWf–FVæ6U÷'Våö–B#¢&–÷%÷'Våö–BÀ¢&6öçG&7E÷6†#Sb#¢6öçG&7E÷6†#SbÀ¢&6öçG&öÆÆW%ö6æF–FFUö†VE÷6†#¢÷7EövFUö6öçG&öÆÆW%ö6æF–FFUö†VBÀ¢&6öçG&7Eö&Æö6¶W'2#¢&Æö6¶W'2À¢'&WV—&VEö6†V6·2#¢&WV—&VEö6†V6·2À¢'&WV—&VEö6†V6µö2#¢¶æÖS¢&–æF–æw5¶æÖUÒf÷"æÖR–â&WV—&VEö6†V6·7ÒÀ¢&6æF–FFUö–ÖvW2#¢–ÖvW2À¢'&Wf–÷W5ö–ÖvW2#¢&Wf–÷W5ö–ÖvW2À¢&FWÆ÷–ÖVçEöWF†÷&—G’#¢FWÆ÷–ÖVçEöWF†÷&—G’À¢'&÷FV7FVEöVçf—&öæÖVçB#¢Vçf—&öæÖVçB÷"æöæRÀ¢'&÷FV7FVEöVçf—&öæÖVçEö&÷fVB#¢fÇ6RÀ¢'&÷FV7FVEöVçf—&öæÖVçEö¦ö%ö6ö×ÆWFVB#¢fÇ6RÀ¢''VçF–ÖUö6öçF7FVB#¢fÇ6RÀ¢'&öGV7F–öåö6†ævVB#¢fÇ6RÀ¢&W‡FW&æÅöVffV7G5öVæ&ÆVB#¢fÇ6RÀ¢'7FGW2#¢%52"À¢Ð¢Væ6öFVBÒ§6öâæGV×2†Wf–FVæ6RÂ6÷'Eö¶W—3ÕG'VRÂ6W&F÷'3Ò‚"Â"Â#¢"’’æVæ6öFR‚¢v—F‚F‚†÷2æVçf—&öå²$t•D…T%ôõUEUB%Ò’æ÷Vâ‚&"ÂVæ6öF–æsÒ'WFbÓ‚"’27G&VÓ ¢7G&VÒçw&—FR†b&Vçf—&öæÖVçC×¶Vçf—&öæÖVçGÕÆâ"¢7G&VÒçw&—FR†b&FWÆ÷–ÖVçEöWF†÷&—G“×·7G"†FWÆ÷–ÖVçEöWF†÷&—G’’æÆ÷vW"‚—ÕÆâ"¢7G&VÒçw&—FR†b&Wf–FVæ6Uö#cC×¶&6ScBæ#cFVæ6öFR†Væ6öFVB’æFV6öFR‚—ÕÆâ"¢7G&VÒçw&—FR†b&Wf–FVæ6U÷6†#Sc×¶†6†Æ–"ç6†#Sb†Væ6öFVB’æ†W†F–vW7B‚—ÕÆâ"¢&WGW&â   ¦FVb6VÆe÷FW7B‚’Óâ–çC ¢vÆö&Âäõõ$TD•$T5EôõTäU ¢Æö6Å÷&W÷6—F÷'’Ò§6öâæÆöG2„4ôåE$5EõD‚ç&VE÷FW‡B†Væ6öF–æsÒ'WFbÓ‚"’•²'&W÷6—F÷'’%Ð¢6†V6¶÷WE÷6†Ò7V'&ö6W72æ6†V6µö÷WGWB…²&v—B"Â'&Wb×'6R"Â$„TB%ÒÂFW‡CÕG'VR’ç7G&—‚¢÷2æVçf—&öâç6WFFVfVÇB‚$t•D…T%õ$Uõ4•Dõ%’"ÂÆö6Å÷&W÷6—F÷'’ ¢&WV—&R€¢6WB„U…T5DTEô4„T4µõtõ$´dÄõu2¢ÓÒ6WB„U…T5DTEõ$UT•$TEô4„T4µõ4õU$4Uô4Äõ5U$Uõ4„#Sb’À¢'&WV—&VB6÷W&6R6Æ÷7W&R6FÆör—2–æ6ö×ÆWFR"À¢¢fÆ–FFU÷v÷&¶fÆ÷uö7F–öå÷&VfW&Væ6W2€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢'7–çF†WF–2×&WV—&VBÖ6†V6²ç–ÖÂ"À¢€¢"&¦ö'3¥ÆâFW7C¥Æâ7FW3¥Æâ ¢""ÒW6W3¢7F–öç2ö6†V6¶÷WD#3CScsƒ“#3CScsƒ“#3CScsƒ“#3CScsƒ•Æâ ¢""ÒW6W3¢Fö6¶W#¢òöW†×ÆRö6†V6´6†#Sc¢ ¢²"&"¢c@¢²"%Æâ ¢’À¢¢f÷"×WF&ÆU÷v÷&¶fÆ÷r–â€¢"&¦ö'3¥ÆâFW7C¥Æâ7FW3¥ÆâÒW6W3¢7F–öç2ö6†V6¶÷WDcuÆâ"À¢"&¦ö'3¥ÆâFW7C¥Æâ7FW3¥ÆâÒW6W3¢Fö6¶W#¢òöW†×ÆRö6†V6³¦ÆFW7EÆâ"À¢"&¦ö'3¢·FW7C¢·7FW3¢··W6W3¢÷væW"ö7F–öäcÕ××ÕÆâ"À¢"&¦ö'3¥ÆâFW7C¥Æâ7FW3¥ÆâÒ²wW6W2s¢÷væW"ö7F–öäÖ–çÕÆâ"À¢“ ¢G'“ ¢fÆ–FFU÷v÷&¶fÆ÷uö7F–öå÷&VfW&Væ6W2€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢'7–çF†WF–2×&WV—&VBÖ6†V6²ç–ÖÂ"À¢×WF&ÆU÷v÷&¶fÆ÷rÀ¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fR×WF&ÆR&WV—&VBÖ6†V6²7F–öâ&Vw&W76–öâ76VB"¢&W÷6—F÷'•÷&ö÷BÒF‚æ7vB‚¢v—F‚FV×f–ÆRåFV×÷&'”F—&V7F÷'’€¢&Vf—ƒÒ"æ6öFW7G&ÖÆö6ÂÖ7F–öâÒ"À¢F—#×&W÷6—F÷'•÷&ö÷BÀ¢’2F—&V7F÷'“ ¢7F–öåöF—&V7F÷'’ÒF‚†F—&V7F÷'’¢†7F–öåöF—&V7F÷'’ò&7F–öâç–ÖÂ"’çw&—FU÷FW‡B€¢&æÖS¢Vç6fRÖÆö6ÂÖ7F–öåÆç'Vç3¥ÆâW6–æs¢6ö×÷6—FUÆâ7FW3¥Æâ ¢"ÒW6W3¢÷væW"ö7F–öäÖ–åÆâ"À¢Væ6öF–æsÒ'WFbÓ‚"À¢¢&VÆF—fUö7F–öâÒ7F–öåöF—&V7F÷'’ç&VÆF—fU÷Fò‡&W÷6—F÷'•÷&ö÷B’æ5÷÷6—‚‚¢G'“ ¢fÆ–FFU÷v÷&¶fÆ÷uö7F–öå÷&VfW&Væ6W2€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢'7–çF†WF–2ÖÆö6ÂÖ7F–öâç–ÖÂ"À¢€¢&¦ö'3¥ÆâFW7C¥Æâ7FW3¥Æâ ¢b"ÒW6W3¢â÷·&VÆF—fUö7F–öçÕÆâ ¢’æVæ6öFR‚’À¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fR&V7W'6—fRÆö6ÂÖ7F–öâ–â&Vw&W76–öâ76VB"¢†7F–öåöF—&V7F÷'’ò&7F–öâç–ÖÂ"’çw&—FU÷FW‡B€¢&æÖS¢Vç6fRÖ6öçF–æW"Ö7F–öåÆç'Vç3¥ÆâW6–æs¢Fö6¶W%Æâ ¢"–ÖvS¢Fö6¶W#¢òöW†×ÆRö6†V6³¦ÆFW7EÆâ"À¢Væ6öF–æsÒ'WFbÓ‚"À¢¢G'“ ¢fÆ–FFU÷v÷&¶fÆ÷uö7F–öå÷&VfW&Væ6W2€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢b'·&VÆF—fUö7F–öçÒö7F–öâç–ÖÂ"À¢†7F–öåöF—&V7F÷'’ò&7F–öâç–ÖÂ"’ç&VEö'—FW2‚’À¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fRÆö6Â6öçF–æW"Ö7F–öâ–â&Vw&W76–öâ76VB"¢†7F–öåöF—&V7F÷'’ò&7F–öâç–ÖÂ"’çw&—FU÷FW‡B€¢&æÖS¢Vç6fRÖFö6¶W&f–ÆRÖ7F–öåÆç'Vç3¥ÆâW6–æs¢Fö6¶W%Æâ ¢"–ÖvS¢Fö6¶W&f–ÆUÆâ"À¢Væ6öF–æsÒ'WFbÓ‚"À¢¢†7F–öåöF—&V7F÷'’ò$Fö6¶W&f–ÆR"’çw&—FU÷FW‡B€¢$e$ôÒW†×ÆRö&6S¦ÆFW7EÆâ"À¢Væ6öF–æsÒ'WFbÓ‚"À¢¢G'“ ¢fÆ–FFU÷v÷&¶fÆ÷uö7F–öå÷&VfW&Væ6W2€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢b'·&VÆF—fUö7F–öçÒö7F–öâç–ÖÂ"À¢†7F–öåöF—&V7F÷'’ò&7F–öâç–ÖÂ"’ç&VEö'—FW2‚’À¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fRÆö6ÂÖ7F–öâFö6¶W&f–ÆR–â&Vw&W76–öâ76VB"¢†7F–öåöF—&V7F÷'’ò$Fö6¶W&f–ÆR"’çw&—FU÷FW‡B€¢$e$ôÒW†×ÆRö&6T6†#Sc¢"²&"¢cB²"2'V–ÆEÆâ ¢$e$ôÒ'V–ÆEÆâ"À¢Væ6öF–æsÒ'WFbÓ‚"À¢¢fÆ–FFU÷v÷&¶fÆ÷uö7F–öå÷&VfW&Væ6W2€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢b'·&VÆF—fUö7F–öçÒö7F–öâç–ÖÂ"À¢†7F–öåöF—&V7F÷'’ò&7F–öâç–ÖÂ"’ç&VEö'—FW2‚’À¢¢6÷W&6Uöf—‡GW&U÷&W÷6—F÷'’Ò4ôåE$ôÄÄU%õ$Uõ4•Dõ%¢6÷W&6Uöf—‡GW&RÒ°¢°¢&ÖöFR#¢#cCB"À¢'G—R#¢&&Æö""À¢'F‚#¢F‚À¢'6†#¢#""¢CÀ¢Ð¢f÷"F‚–â6÷'FVB‡&WV—&VEö6†V6µ÷6÷W&6U÷F‡2‡6÷W&6Uöf—‡GW&U÷&W÷6—F÷'’’¢Ð¢6÷W&6Uöf—‡GW&RæVæB€¢°¢&ÖöFR#¢#cCB"À¢'G—R#¢&&Æö""À¢'F‚#¢'FööÇ2÷G&ç6—F—fR×&WV—&VBÖ6†V6²Ö–çWBç’"À¢'6†#¢#R"¢CÀ¢Ð¢¢6÷W&6Uöf—‡GW&RæW‡FVæB€¢°¢°¢&ÖöFR#¢#cCB"À¢'G—R#¢&&Æö""À¢'F‚#¢$TÄT4UõdÄ”DDõ%õ4õU$4UõD‚À¢'6†#¢#"¢CÀ¢ÒÀ¢°¢&ÖöFR#¢#cCB"À¢'G—R#¢&&Æö""À¢'F‚#¢&6öæf–r÷&VÆV6W2÷&VÆV6R×FW7BÓæ§6öâ"À¢'6†#¢#2"¢CÀ¢ÒÀ¢Ð¢¢6÷W&6Uöf–ævW'&–çBÒ6÷W&6Uö6Æ÷7W&Uöf–ævW'&–çB€¢6÷W&6Uöf—‡GW&RÀ¢6÷W&6Uöf—‡GW&U÷&W÷6—F÷'’À¢¢&WV—&R€¢6÷W&6Uöf–ævW'&–ç@¢ÓÒ6÷W&6Uö6Æ÷7W&Uöf–ævW'&–çB€¢6÷W&6Uöf—‡GW&U³¢Ó%Ð¢²·²¢§6÷W&6Uöf—‡GW&U²Ó%ÒÂ'6†#¢#B"¢CÒÂ6÷W&6Uöf—‡GW&U²ÓÕÒÀ¢6÷W&6Uöf—‡GW&U÷&W÷6—F÷'’À¢’À¢'&VÆV6RfÆ–FF÷"W†6ÇW6–öâ7&VFVB6VÆb×&VfW&VçF–Â6÷W&6R6Æ÷7W&R"À¢¢&WV—&R€¢6÷W&6Uöf–ævW'&–ç@¢ÓÒ6÷W&6Uö6Æ÷7W&Uöf–ævW'&–çB€¢6÷W&6Uöf—‡GW&U³¢ÓÐ¢²·²¢§6÷W&6Uöf—‡GW&U²ÓÒÂ'6†#¢#B"¢CÕÒÀ¢6÷W&6Uöf—‡GW&U÷&W÷6—F÷'’À¢’À¢&6æF–FFR&Wf–Wr7&VFVB6VÆb×&VfW&VçF–Â6÷W&6R6Æ÷7W&R"À¢¢&WV—&R€¢6÷W&6Uöf–ævW'&–çBÒ6÷W&6Uö6Æ÷7W&Uöf–ævW'&–çB€¢·²¢§6÷W&6Uöf—‡GW&U³ÒÂ'6†#¢#B"¢CÒÂ§6÷W&6Uöf—‡GW&U³¥ÕÒÀ¢6÷W&6Uöf—‡GW&U÷&W÷6—F÷'’À¢’À¢'&WV—&VBÖ6†V6²W†V7WF&ÆRG&–gBF–Bæ÷B6†ævRF†R6÷W&6R6Æ÷7W&R"À¢¢&WV—&R€¢6÷W&6Uöf–ævW'&–ç@¢Ò6÷W&6Uö6Æ÷7W&Uöf–ævW'&–çB€¢6÷W&6Uöf—‡GW&U³¢Ó5Ð¢²·²¢§6÷W&6Uöf—‡GW&U²Ó5ÒÂ'6†#¢#B"¢CÕÐ¢²6÷W&6Uöf—‡GW&U²Ó#¥ÒÀ¢6÷W&6Uöf—‡GW&U÷&W÷6—F÷'’À¢’À¢'VæÆ—7FVBG&ç6—F—fRW†V7WF&ÆRG&–gBF–Bæ÷B6†ævRF†R6÷W&6R6Æ÷7W&R"À¢¢'&VW&õ÷VÆ—G•ö6Æ÷7W&RÒ°¢"æv—F‡V"÷v÷&¶fÆ÷w2ö&6¶VæB×&öGV7F–öâç–ÖÂ"À¢"æv—F‡V"÷v÷&¶fÆ÷w2ög&öçFVæB×&öGV7F–öâç–ÖÂ"À¢"æv—F‡V"÷v÷&¶fÆ÷w2÷&VÆV6RÖ–ÖvW2ç–ÖÂ"À¢&2ö’ôFö6¶W&f–ÆR"À¢&FWÆ÷’ög&öçFVæBôFö6¶W&f–ÆR"À¢&FWÆ÷’÷÷'FÇ2ôFö6¶W&f–ÆR"À¢'67&—G2ö6’ö6Æ76–g’×VÆ—G’×66÷Rç6‚"À¢'67&—G2ö6’÷FW7BÖ6Æ76–g’×VÆ—G’×66÷Rç6‚"À¢'67&—G2ö6’÷FW7B×fÆ–FFRÖ'&VW&ò×66÷Rç6‚"À¢'67&—G2ö6’÷fÆ–FFRÖ'&VW&ò×66÷Rç6‚"À¢Ð¢&WV—&R€¢'&VW&õ÷VÆ—G•ö6Æ÷7W&P¢ÃÒ6WB€¢U…T5DTEô4„T4µõtõ$´dÄõuôU„T5UD$ÄUõ4„#Se²&öÆöã“‚ô'&VW&òæ6öÒ%Õ°¢"æv—F‡V"÷v÷&¶fÆ÷w2÷VÆ—G’ç–ÖÂ ¢Ð¢’À¢$'&VW&òVÆ—G’v÷&¶fÆ÷rW†V7WF&ÆR6Æ÷7W&R—2–æ6ö×ÆWFR"À¢¢v÷&¶fÆ÷u÷F‚Ò"æv—F‡V"÷v÷&¶fÆ÷w2÷&öGV7F–öâÖ÷&6†W7G&F÷"Ö6öçG&7Bç–ÖÂ ¢v÷&¶fÆ÷uö'—FW2ÒF‚‡v÷&¶fÆ÷u÷F‚’ç&VEö'—FW2‚¢&WV—&R€¢—5öW†7EöÆö6Å÷&W÷6—F÷'•÷6÷W&6R€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢’À¢&W†7BÆö6Â6÷W&6RÖf–ÆR&–æF–ær&Vw&W76–öâf–ÆVB"À¢¢7FÆU÷6†Ò‚#"–b6†V6¶÷WE÷6†³ÒÒ#"VÇ6R#"’²6†V6¶÷WE÷6†³¥Ð¢&WV—&R€¢æ÷B—5öW†7EöÆö6Å÷&W÷6—F÷'•÷6÷W&6R€¢Æö6Å÷&W÷6—F÷'’À¢7FÆU÷6†À¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢’À¢'7FÆR6÷W&6R4„–æ6÷'&V7FÇ’W6VBÆö6Âv÷&¶fÆ÷r'—FW2"À¢¢fÆ–FFU÷v÷&¶fÆ÷uöFVf–æ—F–öåö'—FW2€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢v÷&¶fÆ÷u÷F‚À¢v÷&¶fÆ÷uö'—FW2À¢¢G'“ ¢fÆ–FFU÷v÷&¶fÆ÷uöFVf–æ—F–öåö'—FW2€¢Æö6Å÷&W÷6—F÷'’À¢6†V6¶÷WE÷6†À¢v÷&¶fÆ÷u÷F‚À¢v÷&¶fÆ÷uö'—FW2²"%Æâ"À¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fR&WV—&VBÖ6†V6²v÷&¶fÆ÷rF–vW7B&Vw&W76–öâ76VB"¢W†V7WF&ÆU÷F‚Ò"æ6öFW7G&÷fÆ–FFR×&öGV7F–öâÖ÷&6†W7G&F÷"Ö6öçG&7Bç’ ¢W†V7WF&ÆUö'—FW2ÒF‚†W†V7WF&ÆU÷F‚’ç&VEö'—FW2‚¢fÆ–FFU÷v÷&¶fÆ÷uöW†V7WF&ÆUö'—FW2€¢Æö6Å÷&W÷6—F÷'’À¢v÷&¶fÆ÷u÷F‚À¢W†V7WF&ÆU÷F‚À¢W†V7WF&ÆUö'—FW2À¢¢G'“ ¢fÆ–FFU÷v÷&¶fÆ÷uöW†V7WF&ÆUö'—FW2€¢Æö6Å÷&W÷6—F÷'’À¢v÷&¶fÆ÷u÷F‚À¢W†V7WF&ÆU÷F‚À¢W†V7WF&ÆUö'—FW2²"%Æâ"À¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fR&WV—&VBÖ6†V6²W†V7WF&ÆRF–vW7B&Vw&W76–öâ76VB"¢&WV—&R€¢†VEöÆ–6&ÆU÷&WV—&VEö6†V6·2€¢&öÆöã“‚ô–ægW7G'V7F–öâ×&Wò"À¢²&÷&6†W7G&F÷"Ö6öçG&7B"Â'fÆ–FFR"Â'fÆ–FFR×6÷W&6R%ÒÀ¢²&÷&6†W7G&F÷"Ö6öçG&7B"Â'fÆ–FFR"Â'fÆ–FFR×6÷W&6R%ÒÀ¢¢ÓÒ²&÷&6†W7G&F÷"Ö6öçG&7B"Â'fÆ–FFR"Â'fÆ–FFR×6÷W&6R%ÒÀ¢&W†7B'&æ6‚×&WV—&VB6†V6²&Vw&W76–öâf–ÆVB"À¢¢G'“ ¢†VEöÆ–6&ÆU÷&WV—&VEö6†V6·2€¢&öÆöã“‚ô–ægW7G'V7F–öâ×&Wò"À¢²&÷&6†W7G&F÷"Ö6öçG&7B"Â'fÆ–FFR"Â'fÆ–FFR×6÷W&6R%ÒÀ¢²&÷&6†W7G&F÷"Ö6öçG&7B"Â'fÆ–FFR×6÷W&6R%ÒÀ¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fR7FÆR&WV—&VBÖ6†V6²6öçG&7B&Vw&W76–öâ76VB"¢&WV—&R€¢†VEöÆ–6&ÆU÷&WV—&VEö6†V6·2€¢&öÆöã“‚ôÖ–FFÆWv&RÒ"À¢°¢%fÆ–FFRÖ–FFÆWv&RÖW&vR&W7VÇB"À¢%fÆ–FFRÖ–FFÆWv&R6÷W&6R†VB"À¢&6öææV7F÷"×'VçF–ÖRÖ'V–ÆB"À¢&÷&6†W7G&F÷"Ö6öçG&7B"À¢'fÆ–FFR"À¢ÒÀ¢²&6öææV7F÷"×'VçF–ÖRÖ'V–ÆB"Â&÷&6†W7G&F÷"Ö6öçG&7B"Â'fÆ–FFR%ÒÀ¢¢ÓÒ²&6öææV7F÷"×'VçF–ÖRÖ'V–ÆB"Â&÷&6†W7G&F÷"Ö6öçG&7B"Â'fÆ–FFR%ÒÀ¢%"ÖöæÇ’&WV—&VBÖ6†V6²6Æ76–f–6F–öâ&Vw&W76–öâf–ÆVB"À¢¢¶W–6Æöµö†VEö6†V6·2Ò°¢&÷&6†W7G&F÷"Ö6öçG&7B"À¢'fÆ–FFR"À¢'fÆ–FFRÖÖW&vR×&W7VÇB"À¢'fÆ–FFR×6÷W&6R"À¢Ð¢&WV—&R€¢†VEöÆ–6&ÆU÷&WV—&VEö6†V6·2€¢&öÆöã“‚ô¶W–6Æö²"À¢¶W–6Æöµö†VEö6†V6·2²²&&ö÷G7G&%ÒÀ¢¶W–6Æöµö†VEö6†V6·2À¢¢ÓÒ¶W–6Æöµö†VEö6†V6·2À¢$¶W–6Æö²†VBõ"6†V6²Æ–væÖVçBf–ÆVB"À¢ ¢F–vW7EöÒ&v†7"æ–òöW†×ÆRö6†#Sc¢"²&"¢c@¢F–vW7Eö"Ò&v†7"æ–òöW†×ÆRö6†#Sc¢"²&""¢c@¢öÆ–7’Ò²&Ö–æ–×VÕö–ÖvW2#¢Â&Ö†–×VÕö–ÖvW2#¢Â&–ÖvU÷&W÷6—F÷&–W2#¢²&v†7"æ–òöW†×ÆRö%×Ð¢fÆ–FFUö–ÖvW2…¶F–vW7EöÒÂ¶F–vW7Eö%ÒÂöÆ–7’¢f÷"6æF–FFRÂÖW76vR–â€¢…²&v†7"æ–òöW†×ÆRö¦ÆFW7B%ÒÂ&×WF&ÆR–ÖvRFr"’À¢…µÒÂ&–æ6ö×ÆWFR6æF–FFR–ÖvW2"’À¢…²&v†7"æ–òöW†×ÆR÷w&öæt6†#Sc¢"²&"¢cEÒÂ'w&öær–ÖvR&W÷6—F÷'’"’À¢“ ¢G'“ ¢fÆ–FFUö–ÖvW2†6æF–FFRÂ¶F–vW7Eö%ÒÂöÆ–7’¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"†b&æVvF—fR&Vw&W76–öâ76VC¢¶ÖW76vWÒ"¢&V÷&FW&VE÷öÆ–7’Ò°¢&Ö–æ–×VÕö–ÖvW2#¢"À¢&Ö†–×VÕö–ÖvW2#¢"À¢&–ÖvU÷&W÷6—F÷&–W2#¢²&v†7"æ–òöW†×ÆRö"Â&v†7"æ–òöW†×ÆR÷v÷&¶W"%ÒÀ¢Ð¢öÒ&v†7"æ–òöW†×ÆRö6†#Sc¢"²&"¢c@¢v÷&¶W%ö"Ò&v†7"æ–òöW†×ÆR÷v÷&¶W$6†#Sc¢"²&""¢c@¢G'“ ¢fÆ–FFUö–ÖvW2…¶öÂv÷&¶W%ö%ÒÂ·v÷&¶W%ö"ÂöÒÂ&V÷&FW&VE÷öÆ–7’¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fR&V÷&FW&VB&öÆÆ&6²&Vw&W76–öâ76VB"¢fÆ–FFUö'F–f7E÷7F÷&vU÷W&Â‚&‡GG3¢ò÷&öGV7F–öç&W7VÇG76æ&Æö"æ6÷&Rçv–æF÷w2ææWBö7F–öç2÷&W7VÇG2ç¦—÷6–s×FW7B"¢f÷"Æö6F–öâ–â€¢&‡GG¢ò÷&öGV7F–öç&W7VÇG76æ&Æö"æ6÷&Rçv–æF÷w2ææWB÷&W7VÇG2ç¦—"À¢&‡GG3¢òö’æv—F‡V"æ6öÒ÷&W÷2öW†×ÆRö&6†—fRç¦—"À¢&‡GG3¢òöWf–ÂæW†×ÆR÷&W7VÇG2ç¦—"À¢“ ¢G'“ ¢fÆ–FFUö'F–f7E÷7F÷&vU÷W&Â†Æö6F–öâ¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fR'F–f7B&VF—&V7B&Vw&W76–öâ76VB"¢÷&–v–æÅö÷VæW"Òäõõ$TD•$T5EôõTäU  ¢6Æ72&VF—&V7EFW7D÷VæW# ¢FVbõö–æ—Eõò‡6VÆb’ÓâæöæS ¢6VÆbç&WVW7G3¢Æ—7E·W&ÆÆ–"ç&WVW7Bå&WVW7EÒÒµÐ ¢FVb÷Vâ‡6VÆbÂ&WVW7C¢W&ÆÆ–"ç&WVW7Bå&WVW7BÂF–ÖV÷WC¢–çB’Óâ–òä'—FW4”ó ¢FVÂF–ÖV÷W@¢6VÆbç&WVW7G2æVæB‡&WVW7B¢–b&WVW7BægVÆÅ÷W&Âç7F'G7v—F‚‚&‡GG3¢òö’æv—F‡V"æ6öÒò"“ ¢&WV—&R€¢&WVW7Bæ†VFW'2ævWB‚$WF†÷&—¦F–öâ"’ÓÒ$&V&W"FW7B×Fö¶Vâ"À¢&'F–f7B’&WVW7BöÖ—GFVBWF†÷&—¦F–öâ"À¢¢†VFW'2ÒÖW76vR‚¢†VFW'5²$Æö6F–öâ%ÒÒ€¢&‡GG3¢ò÷&öGV7F–öç&W7VÇG76æ&Æö"æ6÷&Rçv–æF÷w2ææWBò ¢&7F–öç2÷&W7VÇG2ç¦—÷6–s×FW7B ¢¢&—6RW&ÆÆ–"æW'&÷"ä…EEW'&÷"€¢&WVW7BægVÆÅ÷W&ÂÀ¢3"À¢$f÷VæB"À¢†VFW'2À¢æöæRÀ¢¢&WV—&R€¢&WVW7Bæ†VFW'2ævWB‚$WF†÷&—¦F–öâ"’—2æöæRÀ¢$v—D‡V"WF†÷&—¦F–öâÆV¶VBFò'F–f7B7F÷&vR"À¢¢&WGW&â–òä'—FW4”ò†"'FW7BÖ&6†—fR" ¢&VF—&V7Eö÷VæW"Ò&VF—&V7EFW7D÷VæW"‚¢äõõ$TD•$T5EôõTäU"Ò&VF—&V7Eö÷VæW ¢÷2æVçf—&öâç6WFFVfVÇB‚$t…õDô´Tâ"Â'FW7B×Fö¶Vâ"¢G'“ ¢&WV—&R€¢F÷væÆöEö'F–f7Eö&6†—fR‚'&W÷2öW†×ÆRö7F–öç2ö'F–f7G2ó÷¦—"¢ÓÒ"'FW7BÖ&6†—fR"À¢&'F–f7B&VF—&V7B&Vw&W76–öâ&WGW&æVBVæW‡V7FVB6öçFVçB"À¢¢&WV—&R†ÆVâ‡&VF—&V7Eö÷VæW"ç&WVW7G2’ÓÒ"Â&'F–f7B&VF—&V7BF–Bæ÷BW6RGvòW‡Æ–6—B&WVW7G2"¢f–æÆÇ“ ¢äõõ$TD•$T5EôõTäU"Ò÷&–v–æÅö÷VæW ¢W‡V7FVBÒ²'†6R#¢'Æâ"Â&6æF–FFU÷6†#Sb#¢&2"¢cBÂ'&öGV7F–öåö6†ævVB#¢fÇ6WÐ¢fÆ–FFU÷&–÷"†FVW6÷’†W‡V7FVB’ÂW‡V7FVB¢f÷"¶W’ÂfÇVR–â‚‚'†6R"Â'7Fv–ær"’Â‚&6æF–FFU÷6†#Sb"Â&B"¢cB’Â‚'&öGV7F–öåö6†ævVB"ÂG'VR’“ ¢×WFF–öâÒFVW6÷’†W‡V7FVB¢×WFF–öå¶¶W•ÒÒfÇVP¢G'“ ¢fÆ–FFU÷&–÷"†×WFF–öâÂW‡V7FVB¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"†b&æVvF—fR&–÷"ÖWf–FVæ6R&Vw&W76–öâ76VC¢¶¶W—Ò"¢7FFVÖVçBÒ²'&VF–6FR#¢²&'V–ÆDFVf–æ—F–öâ#¢²'&W6öÇfVDFWVæFVæ6–W2#¢·²&F–vW7B#¢²&v—D6öÖÖ—B#¢&R"¢C×Õ×××Ð¢–ÆöBÒ&6ScBæ#cFVæ6öFR†§6öâæGV×2‡7FFVÖVçB’æVæ6öFR‚’’æFV6öFR‚¢&WV—&R†6÷6–vå÷7FFVÖVçG2†§6öâæGV×2‡²'–ÆöB#¢–ÆöGÒ’’ÓÒ·7FFVÖVçEÒÂ&6÷6–vâ7FFVÖVçB&Vw&W76–öâf–ÆVB"¢G'“ ¢6÷6–vå÷7FFVÖVçG2†§6öâæGV×2‡²'–ÆöB#¢&æ÷BÖ&6ScB'Ò’¢W†6WB…öÆ–7”W'&÷"ÂfÇVTW'&÷"“ ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fRÖÆf÷&ÖVB6÷6–vâGFW7FF–öâ&Vw&W76–öâ76VB"¢'&æ6ƒ¢F–7E·7G"Âç•ÒÒ²'&÷FV7F–öâ#¢²'&WV—&VE÷7FGW5ö6†V6·2#¢²&6öçFW‡G2#¢²'fÆ–FFR%ÒÂ&6†V6·2#¢·²&6öçFW‡B#¢'fÆ–FFR"Â&ö–B#¢S3c‡Õ×××Ð¢æÖW2Â&–æF–æw2Ò&WV—&VEö6†V6µö&–æF–æw2†'&æ6‚ÂµµÕÒÂS3c‚¢&WV—&R†æÖW2ÓÒ²'fÆ–FFR%ÒæB&–æF–æw2ÓÒ²'fÆ–FFR#¢S3c‡ÒÂ&Ö&–æF–ær÷6—F—fR&Vw&W76–öâf–ÆVB"¢w&öærÒFVW6÷’†'&æ6‚¢w&öæu²'&÷FV7F–öâ%Õ²'&WV—&VE÷7FGW5ö6†V6·2%Õ²&6†V6·2%Õ³Õ²&ö–B%ÒÒ¢G'“ ¢&WV—&VEö6†V6µö&–æF–æw2‡w&öærÂµµÕÒÂS3c‚¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fRw&öærÖ&Vw&W76–öâ76VB"¢ÆFW7BÒÆFW7Eö6†V6µö6öæ6ÇW6–öç2€¢°¢°¢&6†V6µ÷'Vç2#¢°¢°¢&–B#¢À¢&æÖR#¢'fÆ–FFR"À¢&#¢²&–B#¢S3c‡ÒÀ¢'7FGW2#¢&6ö×ÆWFVB"À¢&6öæ6ÇW6–öâ#¢'7V66W72"À¢'7F'FVEöB#¢###bÓ’Ó…C££¢"À¢&6ö×ÆWFVEöB#¢###bÓ’Ó…C££¢"À¢ÒÀ¢°¢&–B#¢À¢&æÖR#¢'fÆ–FFR"À¢&#¢²&–B#¢S3c‡ÒÀ¢'7FGW2#¢&–å÷&öw&W72"À¢&6öæ6ÇW6–öâ#¢æöæRÀ¢'7F'FVEöB#¢###bÓ’Ó…C£S£¢"À¢&6ö×ÆWFVEöB#¢æöæRÀ¢ÒÀ¢Ð¢Ð¢Ð¢¢&WV—&R€¢ÆFW7E²‚'fÆ–FFR"ÂS3c‚•Ò—2æöæRÀ¢&æWvW7BVæF–ær6†V6²×'Vâ&Vw&W76–öâf–ÆVB"À¢¢v÷&¶fÆ÷u÷&W÷6—F÷'’Ò&öÆöã“‚ôÖöæW–&VRÔ&6¶VæB ¢v÷&¶fÆ÷u÷6†Ò&"¢C ¢v÷&¶fÆ÷uö6†V6·2Ò°¢°¢&–B#¢#À¢&æÖR#¢&÷&6†W7G&F÷"Ö6öçG&7B"À¢&#¢²&–B#¢S3c‡ÒÀ¢&6öæ6ÇW6–öâ#¢'7V66W72"À¢&FWF–Ç5÷W&Â#¢€¢b&‡GG3¢òöv—F‡V"æ6öÒ÷·v÷&¶fÆ÷u÷&W÷6—F÷'—Òö7F–öç2÷'Vç2ó#ö¦ö"ó# ¢’À¢ÒÀ¢°¢&–B#¢#À¢&æÖR#¢&÷&6†W7G&F÷"Ö6öçG&7B"À¢&#¢²&–B#¢S3c‡ÒÀ¢&6öæ6ÇW6–öâ#¢'7V66W72"À¢&FWF–Ç5÷W&Â#¢€¢b&‡GG3¢òöv—F‡V"æ6öÒ÷·v÷&¶fÆ÷u÷&W÷6—F÷'—Òö7F–öç2÷'Vç2ó#ö¦ö"ó# ¢’À¢ÒÀ¢Ð¢v÷&¶fÆ÷u÷'Vç2Ò°¢#¢°¢'F‚#¢"æv—F‡V"÷v÷&¶fÆ÷w2÷&öGV7F–öâÖ÷&6†W7G&F÷"Ö6öçG&7Bç–ÖÂ"À¢&†VE÷6†#¢v÷&¶fÆ÷u÷6†À¢&†VEö'&æ6‚#¢&Ö–â"À¢&WfVçB#¢'W6‚"À¢ÒÀ¢#¢°¢'F‚#¢"æv—F‡V"÷v÷&¶fÆ÷w2÷7ööbç–ÖÂ"À¢&†VE÷6†#¢v÷&¶fÆ÷u÷6†À¢&†VEö'&æ6‚#¢&Ö–â"À¢&WfVçB#¢'W6‚"À¢ÒÀ¢Ð¢¦ö'2Ò°¢#¢°¢&–B#¢#À¢''Våö–B#¢#À¢''VåöGFV×B#¢À¢&æÖR#¢&÷&6†W7G&F÷"Ö6öçG&7B"À¢&†VE÷6†#¢v÷&¶fÆ÷u÷6†À¢ÒÀ¢#¢°¢&–B#¢#À¢''Våö–B#¢#À¢''VåöGFV×B#¢À¢&æÖR#¢&÷&6†W7G&F÷"Ö6öçG&7B"À¢&†VE÷6†#¢v÷&¶fÆ÷u÷6†À¢ÒÀ¢Ð¢v÷&¶fÆ÷uöÆFW7BÒv÷&¶fÆ÷uö&÷VæEö6†V6µö6öæ6ÇW6–öç2€¢·²&6†V6µ÷'Vç2#¢v÷&¶fÆ÷uö6†V6·7ÕÒÀ¢v÷&¶fÆ÷u÷&W÷6—F÷'’À¢v÷&¶fÆ÷u÷6†À¢²&÷&6†W7G&F÷"Ö6öçG&7B%ÒÀ¢²&÷&6†W7G&F÷"Ö6öçG&7B#¢S3c‡ÒÀ¢v÷&¶fÆ÷u÷'Vç2À¢¦ö'2À¢&Ö–â"À¢¢&WV—&R€¢v÷&¶fÆ÷uöÆFW7E²‚&÷&6†W7G&F÷"Ö6öçG&7B"ÂS3c‚•ÒÓÒ'7V66W72"À¢'v÷&¶fÆ÷rÖ&÷VæB6†V6²&Vw&W76–öâf–ÆVB"À¢¢G'“ ¢v÷&¶fÆ÷uö&÷VæEö6†V6µö6öæ6ÇW6–öç2€¢·²&6†V6µ÷'Vç2#¢·v÷&¶fÆ÷uö6†V6·5³Õ×ÕÒÀ¢v÷&¶fÆ÷u÷&W÷6—F÷'’À¢v÷&¶fÆ÷u÷6†À¢²&÷&6†W7G&F÷"Ö6öçG&7B%ÒÀ¢²&÷&6†W7G&F÷"Ö6öçG&7B#¢S3c‡ÒÀ¢³#¢²¢§v÷&¶fÆ÷u÷'Vç5³#ÒÂ&WfVçB#¢'VÆÅ÷&WVW7B'×ÒÀ¢³#¢¦ö'5³#×ÒÀ¢&Ö–â"À¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fRVÆÂ×&WVW7B&WV—&VBÖ6†V6²&Vw&W76–öâ76VB"¢GWÆ–6FRÒFVW6÷’‡v÷&¶fÆ÷uö6†V6·5³Ò¢GWÆ–6FU²&–B%ÒÒ# ¢GWÆ–6FU²&FWF–Ç5÷W&Â%ÒÒ€¢b&‡GG3¢òöv—F‡V"æ6öÒ÷·v÷&¶fÆ÷u÷&W÷6—F÷'—Òö7F–öç2÷'Vç2ó#ö¦ö"ó# ¢¢GWÆ–6FUö¦ö'2Ò°¢¢¦¦ö'2À¢#¢²¢¦¦ö'5³#ÒÂ&–B#¢#ÒÀ¢Ð¢G'“ ¢v÷&¶fÆ÷uö&÷VæEö6†V6µö6öæ6ÇW6–öç2€¢·²&6†V6µ÷'Vç2#¢·v÷&¶fÆ÷uö6†V6·5³ÒÂGWÆ–6FU×ÕÒÀ¢v÷&¶fÆ÷u÷&W÷6—F÷'’À¢v÷&¶fÆ÷u÷6†À¢²&÷&6†W7G&F÷"Ö6öçG&7B%ÒÀ¢²&÷&6†W7G&F÷"Ö6öçG&7B#¢S3c‡ÒÀ¢v÷&¶fÆ÷u÷'Vç2À¢GWÆ–6FUö¦ö'2À¢&Ö–â"À¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fRGWÆ–6FR&WV—&VBÖ6†V6²¦ö"&Vw&W76–öâ76VB"¢&W'Våö6†V6²Ò°¢¢§v÷&¶fÆ÷uö6†V6·5³ÒÀ¢&–B#¢#2À¢&6öæ6ÇW6–öâ#¢æöæRÀ¢&FWF–Ç5÷W&Â#¢€¢b&‡GG3¢òöv—F‡V"æ6öÒ÷·v÷&¶fÆ÷u÷&W÷6—F÷'—Òö7F–öç2÷'Vç2ó“’ö¦ö"ó““ ¢’À¢Ð¢&W'VåöÆFW7BÒv÷&¶fÆ÷uö&÷VæEö6†V6µö6öæ6ÇW6–öç2€¢·²&6†V6µ÷'Vç2#¢·v÷&¶fÆ÷uö6†V6·5³ÒÂ&W'Våö6†V6µ×ÕÒÀ¢v÷&¶fÆ÷u÷&W÷6—F÷'’À¢v÷&¶fÆ÷u÷6†À¢²&÷&6†W7G&F÷"Ö6öçG&7B%ÒÀ¢²&÷&6†W7G&F÷"Ö6öçG&7B#¢S3c‡ÒÀ¢°¢¢§v÷&¶fÆ÷u÷'Vç2À¢““¢°¢'F‚#¢"æv—F‡V"÷v÷&¶fÆ÷w2÷&öGV7F–öâÖ÷&6†W7G&F÷"Ö6öçG&7Bç–ÖÂ"À¢&†VE÷6†#¢v÷&¶fÆ÷u÷6†À¢&†VEö'&æ6‚#¢&Ö–â"À¢&WfVçB#¢'W6‚"À¢ÒÀ¢ÒÀ¢°¢¢¦¦ö'2À¢““¢°¢&–B#¢““À¢''Våö–B#¢“’À¢''VåöGFV×B#¢"À¢&æÖR#¢&÷&6†W7G&F÷"Ö6öçG&7B"À¢&†VE÷6†#¢v÷&¶fÆ÷u÷6†À¢ÒÀ¢ÒÀ¢&Ö–â"À¢¢&WV—&R€¢&W'VåöÆFW7E²‚&÷&6†W7G&F÷"Ö6öçG&7B"ÂS3c‚•Ò—2æöæRÀ¢&æWvW7B6†V6²–FVçF—G’F–Bæ÷B7WW'6VFR†–v†W"v÷&¶fÆ÷r'Vâ”B"À¢¢&÷FV7FVEöVçf—&öæÖVçC¢F–7E·7G"Âç•ÒÒ°¢&æÖR#¢'&öGV7F–öâ"À¢&6åöFÖ–ç5ö'—72#¢fÇ6RÀ¢'&÷FV7F–öå÷'VÆW2#¢°¢°¢'G—R#¢'&WV—&VE÷&Wf–WvW'2"À¢'&WfVçE÷6VÆe÷&Wf–Wr#¢G'VRÀ¢'&Wf–WvW'2#¢°¢°¢'G—R#¢%W6W""À¢'&Wf–WvW"#¢°¢&–B#¢”äDUTäDTåEõ$Ud”UtU%ô”BÀ¢&Æöv–â#¢&¶¦ãSSR"À¢ÒÀ¢Ð¢ÒÀ¢Ð¢ÒÀ¢&FWÆ÷–ÖVçEö'&æ6…÷öÆ–7’#¢°¢'&÷FV7FVEö'&æ6†W2#¢G'VRÀ¢&7W7FöÕö'&æ6…÷öÆ–6–W2#¢fÇ6RÀ¢ÒÀ¢Ð¢fÆ–FFUöVçf—&öæÖVçEöFö7VÖVçB‡&÷FV7FVEöVçf—&öæÖVçBÂ'&öGV7F–öâ"¢f÷"×WFF–öåöæÖRÂ×WFF–öâ–â€¢€¢&Ö—76–ærVçf—&öæÖVçB&Wf–WvW'2"À¢°¢¢§&÷FV7FVEöVçf—&öæÖVçBÀ¢'&÷FV7F–öå÷'VÆW2#¢µÒÀ¢ÒÀ¢’À¢€¢'7V'7F—GWFVBVçf—&öæÖVçB&Wf–WvW""À¢°¢¢§&÷FV7FVEöVçf—&öæÖVçBÀ¢'&÷FV7F–öå÷'VÆW2#¢°¢°¢¢§&÷FV7FVEöVçf—&öæÖVçE²'&÷FV7F–öå÷'VÆW2%Õ³ÒÀ¢'&Wf–WvW'2#¢°¢°¢'G—R#¢%W6W""À¢'&Wf–WvW"#¢°¢&–B#¢”äDUTäDTåEõ$Ud”UtU%ô”B²À¢&Æöv–â#¢'7V'7F—GWFVB×&Wf–WvW""À¢ÒÀ¢Ð¢ÒÀ¢Ð¢ÒÀ¢ÒÀ¢’À¢€¢&Vçf—&öæÖVçB6VÆb×&Wf–Wr"À¢°¢¢§&÷FV7FVEöVçf—&öæÖVçBÀ¢'&÷FV7F–öå÷'VÆW2#¢°¢°¢¢§&÷FV7FVEöVçf—&öæÖVçE²'&÷FV7F–öå÷'VÆW2%Õ³ÒÀ¢'&WfVçE÷6VÆe÷&Wf–Wr#¢fÇ6RÀ¢Ð¢ÒÀ¢ÒÀ¢’À¢€¢&Vçf—&öæÖVçBFÖ–æ—7G&F÷"'—72"À¢°¢¢§&÷FV7FVEöVçf—&öæÖVçBÀ¢&6åöFÖ–ç5ö'—72#¢G'VRÀ¢ÒÀ¢’À¢€¢&Vçf—&öæÖVçBVç&÷FV7FVB'&æ6‚"À¢°¢¢§&÷FV7FVEöVçf—&öæÖVçBÀ¢&FWÆ÷–ÖVçEö'&æ6…÷öÆ–7’#¢°¢'&÷FV7FVEö'&æ6†W2#¢fÇ6RÀ¢&7W7FöÕö'&æ6…÷öÆ–6–W2#¢G'VRÀ¢ÒÀ¢ÒÀ¢’À¢“ ¢G'“ ¢fÆ–FFUöVçf—&öæÖVçEöFö7VÖVçB†×WFF–öâÂ'&öGV7F–öâ"¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"†b&æVvF—fR¶×WFF–öåöæÖWÒ&Vw&W76–öâ76VB"¢6æF–FFUöFö7VÖVçBÒ°¢'66†VÖ÷fW'6–öâ#¢4äD”DDUõ44„TÔÀ¢'FV×ÆFR#¢fÇ6RÀ¢'&VÆV6Uö–B#¢'&VÆV6R×FW7BÓ"À¢&6öçG&öÆÆW%÷6†#¢#"¢CÀ¢'6÷W&6UöÆö6µ÷6†#¢#""¢CÀ¢''VçF–ÖUö6æF–FFU÷6†#Sb#¢#2"¢cBÀ¢'6fWG’#¢¶¶W“¢fÇ6Rf÷"¶W’–â4äD”DDUõ4dUE•ô´U•7ÒÀ¢&6ö×öæVçG2#¢°¢°¢'&W÷6—F÷'’#¢&W÷6—F÷'’À¢'6÷W&6U÷6†#¢#"¢C–b&W÷6—F÷'’ÓÒ4ôåE$ôÄÄU%õ$Uõ4•Dõ%’VÇ6R#B"¢CÀ¢&6öçG&7E÷6†#Sb#¢#R"¢cBÀ¢&–ÖvW2#¢µÒÀ¢'&Wf–÷W5ö–ÖvW2#¢µÒÀ¢&Væ&ÆVB#¢G'VRÀ¢Ð¢f÷"&W÷6—F÷'’–â6÷'FVB„4DÄôuõ$Uõ4•Dõ$”U2¢ÒÀ¢Ð¢6æF–FFU÷&rÒ§6öâæGV×2†6æF–FFUöFö7VÖVçBÂ6÷'Eö¶W—3ÕG'VR’æVæ6öFR‚¢6æF–FFUö†6‚Ò†6†Æ–"ç6†#Sb†6æF–FFU÷&r’æ†W†F–vW7B‚¢fÆ–FFUö6æF–FFUöFö7VÖVçB€¢6æF–FFU÷&rÀ¢6æF–FFUö†6‚À¢'&VÆV6R×FW7BÓ"À¢&öÆöã“‚ô–ægW7G'V7F–öâ×&Wò"À¢#B"¢CÀ¢#R"¢cBÀ¢µÒÀ¢µÒÀ¢¢G'“ ¢fÆ–FFUö6æF–FFUöFö7VÖVçB€¢6æF–FFU÷&rÀ¢#b"¢cBÀ¢'&VÆV6R×FW7BÓ"À¢&öÆöã“‚ô–ægW7G'V7F–öâ×&Wò"À¢#B"¢CÀ¢#R"¢cBÀ¢µÒÀ¢µÒÀ¢¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fR6æF–FFR†6‚&Vw&W76–öâ76VB"¢fÆ–FFU÷†6Uö&Æö6¶W'2…²''VçF–ÖR&V6÷fW'’Wf–FVæ6R—2Ö—76–ær%ÒÂ'Æâ"¢G'“ ¢fÆ–FFU÷†6Uö&Æö6¶W'2…²''VçF–ÖR&V6÷fW'’Wf–FVæ6R—2Ö—76–ær%ÒÂ'7Fv–ær"¢W†6WBöÆ–7”W'&÷# ¢70¢VÇ6S ¢&—6RöÆ–7”W'&÷"‚&æVvF—fRVç&W6öÇfVB&Æö6¶W"&Vw&W76–öâ76VB"¢&–çB‚%$TÄT4Uô”åDTåEõ4TÄeõDU5CÕ52"¢&WGW&â   ¦–bõöæÖUõòÓÒ%õöÖ–åõò# ¢G'“ ¢–b7—2æ&we³¥ÒÓÒ²"Ò×6VÆb×FW7B%Ó ¢&—6R7—7FVÔW†—B‡6VÆe÷FW7B‚’¢–b7—2æ&we³¥ÒÓÒ²"Ò×&V6†V6²×&÷FV7FVBÖvFW2%Ó ¢&—6R7—7FVÔW†—B‡&V6†V6µ÷&÷FV7FVEövFW2‚’¢&WV—&R†æ÷B7—2æ&we³¥ÒÂ'Vç7W÷'FVB&wVÖVçG2"¢&—6R7—7FVÔW†—B†Ö–â‚’¢W†6WBöÆ–7”W'&÷"2W'&÷# ¢&—6R7—7FVÔW†—B‡7G"†W'&÷"’’g&öÒW'&÷ 