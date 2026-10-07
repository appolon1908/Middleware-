#!/usr/bin/env python3
"""Fail-closed validation for the repository-owned production contract."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / ".codestra/production-orchestrator-contract.v1.json"
INTENT_PATH = ROOT / ".github/workflows/manual-release-intent.yml"
RELEASE_VALIDATOR_PATH = ROOT / ".codestra/validate-release-intent.py"
RELEASE_VALIDATOR_NON_SELF_REFERENTIAL_BINDINGS = frozenset(
    {
        "SHARED_PRODUCTION_VALIDATOR_SHA256",
        "KEYCLOAK_PRODUCTION_VALIDATOR_SHA256",
        "MIDDLEWARE_PRODUCTION_VALIDATOR_SHA256",
        "BACKEND_PRODUCTION_VALIDATOR_SHA256",
        "EXPECTED_REQUIRED_CHECK_SOURCE_CLOSURE_SHA256",
    }
)
STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256 = (
    "15dbaa6d571a1d1e72c09ca417cc9419"
    "8d8f21260babfae5eaedbdd46472b1ec"
)
MIDDLEWARE_RELEASE_VALIDATOR_SECURITY_SHA256 = (
    "4c2cba2fae0abccfce66b32c45e10f9a77509921a13435441096abdf216a8548"
)
BACKEND_RELEASE_VALIDATOR_SECURITY_SHA256 = (
    "15dbaa6d571a1d1e72c09ca417cc9419"
    "8d8f21260babfae5eaedbdd46472b1ec"
)
MONEYBEE_RELEASE_VALIDATOR_SECURITY_SHA256 = (
    "15dbaa6d571a1d1e72c09ca417cc9419"
    "8d8f21260babfae5eaedbdd46472b1ec"
)
EXPECTED_RELEASE_VALIDATOR_SECURITY_SHA256 = {
    "appolon1908/Infustruction-repo": STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/Keycloak": STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/Middleware-": MIDDLEWARE_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/codestra": STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/beyvra-backend": BACKEND_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/backend2": STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/beyvra-frontend": STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/scrapper": STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/Breero.com": STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/Moneybee-Backend": MONEYBEE_RELEASE_VALIDATOR_SECURITY_SHA256,
    "appolon1908/Telnexa-web": STANDARD_RELEASE_VALIDATOR_SECURITY_SHA256,
}
MANUAL_RELEASE_INTENT_SHA256 = (
    "e4f8353bf03b1d327cd2f995d5bcea442239ec32b2e58df6bd72f7d4f25eee54"
)
SCHEMA = "codestra.production-orchestrator-contract.v1"
PHASES = ["plan", "staging", "canary", "production"]
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
ALLOWED_ACTIONS = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6",
}
PINNED_WORKFLOW_PARSER_INSTALL = (
    "python3 -m pip install --disable-pip-version-check --no-input PyYAML==6.0.3"
)
RUNTIME_TOOLS = {
    "ansible-playbook",
    "chroot",
    "docker",
    "helm",
    "kubectl",
    "podman",
    "scp",
    "script",
    "ssh",
    "terraform",
    "tofu",
}
SHELL_INTERPRETERS = {"bash", "dash", "eval", "ksh", "sh", "zsh"}
SCRIPT_INTERPRETERS = {"node", "perl", "php", "python", "python3", "ruby"}
SAFE_EXTERNAL_PYTHON_MODULES = {
    "compileall",
    "http.server",
    "json.tool",
    "pip",
    "py_compile",
    "pytest",
    "ruff",
    "unittest",
    "venv",
}
EXECUTABLE_STARTUP_ENV = {
    "BASH_ENV",
    "ENV",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "NODE_OPTIONS",
    "PATH",
    "PERL5OPT",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "RUBYOPT",
}
SHELL_WRAPPERS = {
    "!",
    "builtin",
    "command",
    "chrt",
    "env",
    "exec",
    "flock",
    "ionice",
    "nice",
    "nohup",
    "parallel",
    "run-parts",
    "setpriv",
    "setsid",
    "sg",
    "stdbuf",
    "su",
    "sudo",
    "systemd-run",
    "taskset",
    "time",
    "timeout",
    "prlimit",
    "unshare",
    "watch",
}
SHELL_SEPARATORS = {"\n", "&", "&&", "(", ")", ";", "|", "||", "{", "}"}
GENERIC_NETWORK_CLIENTS = {
    "curl",
    "ftp",
    "lftp",
    "nc",
    "ncat",
    "netcat",
    "sftp",
    "socat",
    "telnet",
    "wget",
}
RELEASE_INTENT_ALLOWED_COMMANDS = {
    "base64",
    "cut",
    "gh",
    "jq",
    "printf",
    "python3",
    "set",
    "sha256sum",
    "test",
    "umask",
}
RELEASE_INTENT_ALLOWED_GH_API = {
    (
        "api",
        "repos/${GITHUB_REPOSITORY}",
        "--jq",
        ".default_branch",
    ),
    (
        "api",
        "repos/${GITHUB_REPOSITORY}/branches/${branch}",
        "--jq",
        ".commit.sha",
    ),
}
KUBECTL_MUTATIONS = {
    "annotate",
    "apply",
    "autoscale",
    "cordon",
    "cp",
    "create",
    "delete",
    "debug",
    "drain",
    "edit",
    "exec",
    "expose",
    "label",
    "patch",
    "replace",
    "rollout",
    "run",
    "scale",
    "set",
    "taint",
    "uncordon",
}
HELM_MUTATIONS = {"install", "rollback", "uninstall", "upgrade"}
TERRAFORM_MUTATIONS = {"apply", "destroy", "import", "taint", "untaint"}
CONTAINER_MUTATIONS = {"down", "kill", "rm", "start", "stop", "restart", "up"}
HTTP_MUTATION_FLAGS = {
    "--data",
    "--data-ascii",
    "--data-binary",
    "--data-raw",
    "--data-urlencode",
    "--data-urlencode",
    "--form",
    "--form-string",
    "--json",
    "--upload-file",
    "-d",
}
HTTP_MUTATION_METHODS = {"delete", "patch", "post", "put"}
NETWORK_MUTATION_METHODS = {
    "connect",
    "connect_ex",
    "delete",
    "endheaders",
    "mkd",
    "patch",
    "post",
    "put",
    "putrequest",
    "rename",
    "rmd",
    "send",
    "sendfile",
    "sendmsg",
    "send_message",
    "sendall",
    "sendto",
    "sendmail",
    "sendcmd",
    "storbinary",
    "storlines",
    "voidcmd",
    "write",
    "writelines",
}
NETWORK_CLIENT_HINTS = {
    "aiohttp",
    "api",
    "api_client",
    "client",
    "connection",
    "ftp",
    "ftplib",
    "http",
    "http_client",
    "httpx",
    "requests",
    "session",
    "smtp",
    "smtplib",
    "sock",
    "socket",
    "urllib3",
}
DATABASE_MUTATION_METHODS = {
    "add",
    "bulk_write",
    "bulk_insert_mappings",
    "bulk_save_objects",
    "commit",
    "create",
    "delete",
    "delete_many",
    "delete_one",
    "executemany",
    "flush",
    "find_one_and_delete",
    "find_one_and_replace",
    "find_one_and_update",
    "insert",
    "insert_many",
    "insert_one",
    "save",
    "update",
    "update_many",
    "update_one",
    "upsert",
    "replace_one",
}
DATABASE_CLIENT_HINTS = {
    "asyncpg",
    "conn",
    "connection",
    "cursor",
    "database",
    "db",
    "engine",
    "mongo",
    "mongodb",
    "psycopg",
    "psycopg2",
    "pymongo",
    "pymysql",
    "session",
    "sqlalchemy",
}
SCRIPT_SUFFIXES = {".bash", ".cjs", ".js", ".mjs", ".php", ".pl", ".py", ".rb", ".sh"}
SQL_MUTATION = re.compile(
    r"\b(?:alter|create|delete|drop|grant|insert|merge|revoke|truncate|update)\b",
    re.IGNORECASE,
)
MUTATING_ACTION_MARKERS = {
    "ansible",
    "cloudformation",
    "deploy",
    "helm",
    "kubectl",
    "kubernetes",
    "scp",
    "ssh",
    "terraform",
}
SAFE_NATIVE_ACTION_PREFIXES = {
    "actions/attest-build-provenance@",
    "actions/attest@",
    "actions/cache@",
    "actions/checkout@",
    "actions/download-artifact@",
    "actions/setup-node@",
    "actions/setup-python@",
    "actions/upload-artifact@",
    "anchore/sbom-action@",
    "anchore/scan-action@",
    "aquasecurity/setup-trivy@",
    "aquasecurity/trivy-action@",
    "docker/build-push-action@",
    "docker/login-action@",
    "docker/setup-buildx-action@",
    "github/codeql-action/",
    "gitleaks/gitleaks-action@",
    "pnpm/action-setup@",
    "pypa/gh-action-pip-audit@",
    "sigstore/cosign-installer@",
}
EXPECTED_IDENTITIES: dict[str, tuple[int, str, bool, bool]] = {
    "appolon1908/Infustruction-repo": (1350724865, "infrastructure", True, True),
    "appolon1908/Keycloak": (1347523366, "identity", True, False),
    "appolon1908/Middleware-": (1347559071, "canonical-middleware", False, False),
    "appolon1908/codestra": (1319808791, "application", True, False),
    "appolon1908/beyvra-backend": (1319831182, "application", True, False),
    "appolon1908/backend2": (1319903950, "application", True, False),
    "appolon1908/beyvra-frontend": (1320246591, "application", True, False),
    "appolon1908/scrapper": (1329513537, "migration-evidence", False, False),
    "appolon1908/Breero.com": (1331354808, "application", True, False),
    "appolon1908/Moneybee-Backend": (1343760409, "application", True, False),
    "appolon1908/Telnexa-web": (1346958528, "application", True, False),
    "appolon1908/codestra-production-platform": (1314230781, "controller", False, False),
}
EXPECTED_ARTIFACT_POLICIES: dict[
    str, tuple[tuple[str, ...], bool, bool, bool, str | None, str | None]
] = {
    "appolon1908/Infustruction-repo": ((), False, False, False, None, None),
    "appolon1908/Keycloak": ((), False, False, False, None, None),
    "appolon1908/Middleware-": (
        ("ghcr.io/appolon1908/codestra-middleware",),
        True,
        True,
        True,
        "cosign",
        "oci",
    ),
    "appolon1908/codestra": (
        ("ghcr.io/appolon1908/codestra",),
        True,
        True,
        False,
        "github",
        "github",
    ),
    "appolon1908/beyvra-backend": (
        (
            "ghcr.io/appolon1908/beyvra-backend",
            "ghcr.io/appolon1908/beyvra-backend-edge",
        ),
        True,
        True,
        False,
        "github",
        "oci",
    ),
    "appolon1908/backend2": (
        ("ghcr.io/appolon1908/backend2",),
        True,
        True,
        False,
        "github",
        "github",
    ),
    "appolon1908/beyvra-frontend": (
        ("ghcr.io/appolon1908/beyvra-frontend",),
        True,
        True,
        False,
        "github",
        "oci",
    ),
    "appolon1908/scrapper": ((), False, False, False, None, None),
    "appolon1908/Breero.com": (
        (
            "ghcr.io/appolon1908/breero-api",
            "ghcr.io/appolon1908/breero-frontend",
            "ghcr.io/appolon1908/breero-partner",
            "ghcr.io/appolon1908/breero-ops",
            "ghcr.io/appolon1908/breero-admin",
        ),
        True,
        True,
        False,
        "github",
        "github",
    ),
    "appolon1908/Moneybee-Backend": (
        (
            "ghcr.io/appolon1908/moneybee-api",
            "ghcr.io/appolon1908/moneybee-worker",
            "ghcr.io/appolon1908/moneybee-migrate",
        ),
        True,
        True,
        False,
        "github",
        "github",
    ),
    "appolon1908/Telnexa-web": (
        ("ghcr.io/appolon1908/telnexa-web",),
        True,
        True,
        False,
        "github",
        "github",
    ),
}
APPROVED_COMPLEX_SCRIPT_SHA256: dict[str, dict[str, str]] = {
    "appolon1908/Keycloak": {
        "scripts/ci/audit_keycloak_pull_requests.py": "fa0c559a3dccfd4ced2a73ebcb2e1858724dcdba654fe84198045a6abbfc358b",
        "tests/test_audit_keycloak_pull_requests.py": "0d1065ef132a324ab52694c61e2f47c24fa0787e1a92334f6d34ba0f9b952325",
        "scripts/bootstrap_release_trust_root.py": "265c4d1b9bd365d0cb14d933ec4fa22a269295952874781413befd87a241a294",
        "scripts/review-plan.sh": "65fe10f82d6fdb51ebca45e0453d5288baf05fa78ddce8754946e702432b50c4",
        "scripts/runtime-preflight.sh": "67bff10567f1c9763794f17d378f1dc3785e18569bda7432d0003d872239a052",
        "scripts/runner-systemd-preflight.sh": "d49eec2b037067dbede30aac8b49328025189e6a8883b0b4314ce613a7bd37be",
        "scripts/test-backup-contract.sh": "48ac288ce0e2eb29f220b5e701ef6a11a5a6cfe3eb058c89ac74b505d3c62d62",
        "scripts/test-ephemeral-docker-auth.sh": (
            "44ed657edf1d82b7aa1d2508d75955ae"
            "30b71399c30055db198e2ea8a62ca277"
        ),
        "scripts/test-plan-gate.sh": "a1998a4a92a2535aea09f35c5de369f0675ab4276e86ab92a42f908590c0ca6d",
        "scripts/test-runtime-preflight.sh": "e4fae06b294f0385d6006d35107463eaa65ec1099fae45ef032dffa1d3f65471",
        "scripts/validate-governance.sh": "8e2fb48c36e849f61c838699726e29a6a57ba5d73e6c6e8048737b1627ec5823",
        "scripts/validate-workflows.py": (
            "946687f92f5f437c3b2beebd3bc3e4b0"
            "2e494375846f03c0f9c8ee9a7b88fd80"
        ),
        "scripts/validate.sh": "3783706062b23eb83b6323aae3be9d5b568de57eac81e3d557cca8c13bba2ace",
    },
    "appolon1908/Middleware-": {
        "scripts/apply_portfolio_release_reviewer_access.py": (
            "fbeb2e7cd6bb2c76c0ad5fadf384ffeb9027c9333f02cabfdfacf021754a240f"
        ),
        "scripts/integration_ci.sh": "8d9327fd9ad51d6ba7243d051336f623a4f75d60c60e69fd012e65f598b12d4a",
        "scripts/validate_middleware_authority_convergence.py": (
            "32de4c58a22737dfdea7f42149e3a589"
            "1756d777b2e890cb568cda4e57799e53"
        ),
        "scripts/validate-order-orchestration.py": (
            "a9d3688d3175661f54d86d113c8e03fa"
            "74bf96a7db3813e00f5e5cd5be40b2e8"
        ),
        "scripts/nats_integration_ci.sh": "88d843c665cece68e0fb56a931c295ee10490446cad7b64d9f5356c1cbf7263d",
        "scripts/project_ci.sh": "12a529ea96f39baec5f1eeb287209dc9db355e5dca000cbbfd7494303501b2ae",
        "scripts/release_manifest.py": "92515822529d4d8ceab6233391ab812e7c9a20761e119d8907eb7ea2f474f32b",
        "scripts/run_ci.sh": "64d7c92279dd442144c7e1f74c3e48f0ab5d5db105238a534dcf8ccd99e93138",
        "scripts/synthetic_acceptance_ci.sh": "087dac2c5371f2013fa0a8dd22ed4024409ab5015231fb8801c75cf3203e3a8a",
        "scripts/temporal_integration_ci.sh": "76a682cc1f5b15a0a3eb15a029d87206238dfe4a262eaf5fa2c79403f147d4d6",
        "scripts/verify_container_image.sh": (
            "61ca5bfe98f045adc300856a0679631f"
            "836c918a12a96597e72375439538cfe7"
        ),
        "services/connector-runtime/scripts/test_postgres.sh": "b9b31391d7a04aa8b3362e182a43f880e46f9e85b4d2f5c3c66cb9a9fe88f867",
        "tests/integration/campaign_extension_concurrency.py": "5699be2ee6b9af5a2aed7d39c68086bc09dc9764e8ed8ffb063ec20fd6aea86a",
        "tests/integration/campaign_identity_concurrency.py": "234d97cf48cf29f0ec26bd4cfd48f61d031f46e1250cee477088abb7a190be76",
        "tests/test_calling_api.py": (
            "9e09c6fcda80a97ac2988f73a5be9ec6"
            "caebd3d812220edba3ebfaf3e4b10902"
        ),
        "tests/test_calling_contract.py": (
            "b8e1f705cfc175348ba6455879f42f78"
            "74e52948786f71fb4a9103e13bc072a7"
        ),
        "tests/test_calling_postgres.py": (
            "49891b89afde1955f66a411facb59fa19"
            "f0c81e17c4e492d45aacf625af2e84e"
        ),
        "tests/test_reconciliation_activity.py": (
            "9573de3bf0b5ad457d6c0673dfc27e53"
            "05746161f11df7b2aa330d196bf8f1b3"
        ),
        "tests/test_vicidial_internal_call_adapter.py": (
            "7d15e3fbd540c8e129062e9b870cfa90"
            "f965d8c98ef3a846b52188c39ab22f99"
        ),
        "tests/pairing/test_selected_server_b.py": (
            "2d30e63fef9c9621a2082418ac22ab8e"
            "30eee34a9b0d17312314822a15185b93"
        ),
        "tests/recording/test_api_contract.py": (
            "9aff153756e954091ac21d2831028a06e"
            "00d42fd24b31343c18695a7193fb66a"
        ),
        "tests/recording/test_odoo_hmac.py": (
            "8f02f5b50fc3728c3f9c1a94e77ca48"
            "d01a19dbfdea38fc4805978bc7824a998"
        ),
        "tests/recording/test_source_gates.py": (
            "b9c11f169acc87d720764ccb580561988"
            "5cf3fa126d86b925d3a8412fd806b3a"
        ),
    },
    "appolon1908/codestra": {
        "scripts/deploy/read-only-runtime-discovery.sh": "14cd8ce2653da1e284da480408ba071fd989279ba889a22d0b1f21ec887e1d13",
        "scripts/ci/check-runtime-discovery.mjs": "a0ccd39eb918093ba7715c9fc40fc9facaef6feef95210c46e9fead61323708f",
        "scripts/ci/test-runtime-discovery-fixture.sh": "a7b6557ed6dc927f6dc78a45440c3cf8deda6a2410a3bf94c70231be3bda751d",
        "scripts/ci/test-runtime-discovery-host-proxy.sh": "c934ec0ff3aa97a08940c0475139edfa155fb839b43017a5a881ffde480ac9e9",
    },
    "appolon1908/beyvra-frontend": {
        "client-portal/scripts/audit-gate.mjs": (
            "8f50a0920735449fe65aa988cf2f4f290"
            "aa744362bbc848830992da7752e2fd6"
        ),
        "client-portal/scripts/check-api-contract.mjs": (
            "df9cf9d60aab7c8296008f4356084610"
            "ebc7ce5430600068d8112e86a6095f30"
        ),
    },
    "appolon1908/beyvra-backend": {
        "FX/release-init-prod.sh": "cef5fadd788f5ae5c9ba28a857bfe516e47b671e36aafcf3817b4c20b9e5115b",
        "operations/verify_release_identity.py": "8aadfc14fa376ba46483216c6323d589d4603c71d42f5a77c29286edb5b5cf0a",
        "scripts/certify_staging_api.py": (
            "a154c1bcd011c42442263d06b530e243"
            "901ec8487c6b34f032e53810f12a5a30"
        ),
    },
    "appolon1908/scrapper": {
        "apps/operations-dashboard/test/api-client.test.mjs": (
            "430b330930d546cdc5270b6d3ffe10955"
            "0e9e725d646e737a54c2654d92b7646"
        ),
        "scripts/validate-gateway.sh": "d0c9888cc7bde27682a32d00fcb00d55d0ff6fc3ad711650cb6647f9dfccdc2a",
        "scripts/validate-deployment-scaffolding.sh": "03db69454e0ea0f62923ab43307ce95ab08c3586d3eb455f53552b65e052cfb9",
        "scripts/validate-workflow-policy.rb": "5cfa66e2126849121a263e0d651b5885ae0535156fb45c47a3ec5f1ca8f587c0",
        "scripts/verify-release-context.sh": "7f1799aed294208d9ad3d86d7f6d246ebf9293ab75fd8df8c16a076f86f9e7ba",
        "test/integration-delivery-replay.test.mjs": (
            "c251545621b2c4a3706ee70b5c376cbc"
            "61f83135f2ab3c56a10ddbef394e55b1"
        ),
        "test/integration-runtime.test.mjs": (
            "57acccbee1522daa07b513a23a212bc7"
            "62353ef1d0998cbbe1fe603927b56697"
        ),
        "test/unit-discovery-import.test.mjs": (
            "9fd2939f0fba91d88e47b2acb76cdc53"
            "360a428172c050d94dacc5a174737a86"
        ),
        "test/unit-document-governance.test.mjs": (
            "7b4d7d5d9e5d5cd383bff5a18c72387d"
            "13c356af729efb6fab331828898d8f08"
        ),
        "test/unit-gateway-routes.test.mjs": (
            "012e916599563c9363e073d0268551ba"
            "31e3a6f77223f631c5556defd811c5be"
        ),
        "test/unit-job-view.test.mjs": (
            "1f20f7c1d6e6051c3f77e173216f850a"
            "b0d77526c256e3285a02d948142251e1"
        ),
        "test/unit-schema.test.mjs": (
            "84e894c7980a53b5295ab66933758fd1"
            "06e6c6c9705c4c6339a462da5b6691db"
        ),
        "test/unit-url-policy.test.mjs": (
            "5218de57f97201b47e51e10943151037"
            "b93ba8b7b883c5ed1462578f76a44849"
        ),
    },
    "appolon1908/Breero.com": {
        "apps/api/scripts/check_schema_drift.py": "746760dea22319cd64c486a08b82ebbccee1dc256566fa6b24cee7f02ff68b47",
        "apps/api/scripts/generate_openapi.py": "7e1ad9606a113b556752222b2782da01b66651b2f8107d3108014b9d45f29a66",
        "scripts/ci/test-classify-quality-scope.sh": "0365cd71d85e00facf1a64c2f11734e413430af75e4cf39e0e52971d13d5c473",
        "scripts/ci/test-validate-breero-scope.sh": "ea29de36868e28ff82e3ec151f896aed388d2421f5907151c4c13480dae20bf8",
        "scripts/ci/validate-breero-scope.sh": "f8ffb8a3953c56d7d6722938825bfb33fced802ba162f4cefd3d12be8ffb9a1e",
    },
    "appolon1908/Moneybee-Backend": {
        "scripts/generate_endpoint_catalog.py": (
            "174a22ef99c72a9432ede92e1c5117e7"
            "092aaf2f9e6dbf40af79087503c30f0a"
        ),
        "ops/stage-bank-credential-references.py": (
            "ea78c91ccc0d779260b13ccead92ca31"
            "16b5b0ac5f3ede028e86a8aa197e3cca"
        ),
        "ops/verify-compose-contract.py": "5b9c78f82de3784af3d68945be43abadbbe3eab7e73f3edf0f27ef7042e7e674",
        "scripts/smoke_api.py": (
            "62b60fa9fb0331d5227b51b9b2c542d"
            "4ec96da9f683a5f678a60d5f27996c692"
        ),
        "scripts/verify_openapi_contract.py": (
            "b4dd40045e2781a6a741787b0c1a51d"
            "30b96248027a0e058f0123a9853a784a0"
        ),
    },
    "appolon1908/Telnexa-web": {
        "deployment/scripts/validate-compliance.sh": "a29fa2c3586332016ec468a710487bca7e5362244c6feec63ae1bde47f4f0f75",
        "scripts/smoke-local.mjs": "13d7f9fcd9bdcc1ac598018a0ca2aab3b3478c08d845b3d0f366b533e4142313",
        "scripts/validate-compliance.mjs": "cd174eebb976c8995545ceb07cd761e53ff1a54ac30c2a4b015bbd92a0768306",
        "scripts/validate-contracts.mjs": (
            "d1fa0b7327863cfcfcf3d00cbc275b45"
            "5155efbd2e54f35589e4b4f6b9b3f162"
        ),
        "tests/contracts/compliance.test.mjs": "1278421b46f690974e087af11fc989eecef21ad605f9707d8da81180391b0478",
    },
}
APPROVED_COMPLEX_SCRIPT_DEPENDENCY_SCAN: dict[str, frozenset[str]] = {
    "appolon1908/Keycloak": frozenset(
        {"scripts/review-plan.sh", "scripts/validate.sh"}
    ),
    "appolon1908/Middleware-": frozenset({"scripts/run_ci.sh"}),
    "appolon1908/codestra": frozenset(
        {
            "scripts/ci/test-runtime-discovery-fixture.sh",
            "scripts/ci/test-runtime-discovery-host-proxy.sh",
        }
    ),
}
APPROVED_CONTROL_PLANE_WORKFLOW_SHA256: dict[str, dict[str, str]] = {
    "appolon1908/Middleware-": {
        ".github/workflows/codestra-hierarchy-governance.yml": (
            "1fd7ab66359de998f5d0ee892d969c15"
            "ec52954b583d540f7b8f5cb8d864b1dc"
        ),
        ".github/workflows/portfolio-production-ruleset-apply.yml": (
            "01c638afd5345837983556b5945a604d"
            "b09dbfc841ca3eaf7fe13c381bcd1659"
        ),
        ".github/workflows/portfolio-main-release-authorities.yml": (
            "393b612783e6daaf9105932e1f6e0b389"
            "9e21c8a67466533112117d6cf671ad4"
        ),
        ".github/workflows/exact-main-production-release.yml": (
            "baa4961c6e409b269599228c78cfe4c38"
            "8faff7e5fe9283d315e159f3dcc63ff"
        ),
        ".github/workflows/lead-automation-n8n-source-v1.yml": (
            "6b0cb7126987c14757bd1f48667bf81d"
            "50caaed769389cb30fc725766ea6bed6"
        ),
        ".github/workflows/middleware-ci.yml": (
            "0f4d5367d2c5785394988403a368bab10"
            "a87c96fa2c0f2e9e99402dc63ea4897"
        ),
        ".github/workflows/integration-main-release-authorities.yml": (
            "fd412e7da3d0c1f3ffb24b88c48a62050"
            "65da2f7dd6f501d2e35fd6aba5d46f2"
        ),
        ".github/workflows/production-reviewer-access.yml": (
            "875b015c37ae7bf486f454e38e174cc5"
            "b904922d2c472a6edbbb2da4621c4be9"
        ),
        ".github/workflows/python-quality-baseline.yml": (
            "cb89cb69636dc79a6a03e5df98abeb798"
            "6a823e30c2d52b1d03980dddac58cca"
        ),
        ".github/workflows/required-ci.yml": "67d29b7c00d232ed78bacae64606f90081753d8786a876cf3222faf22e15cf30",
        ".github/workflows/production-route-contract.yml": (
            "1c6f903907549ff12660d2380260b1d9"
            "51e0cc5a16766bf985e8f22c7dca4d85"
        ),
        ".github/workflows/release-component-ci.yml": (
            "7489c1bcc2361af047cba2d51a0500"
            "c47f4c870e9518f81ba4d8541383ccc7d2"
        ),
    },
    "appolon1908/beyvra-backend": {
        ".github/workflows/ci.yml": "fffbdd8b7aad8b2679bcc608f0b487bf976c033a07786a0af5262d893867211a",
    },
    "appolon1908/beyvra-frontend": {
        ".github/workflows/ci.yml": "7459a31c6b005e9345661b10ee8df45a570ac652280a2eacbcdfd4673fb115da",
    },
    "appolon1908/scrapper": {
        ".github/workflows/ci.yml": "31d81c5be094a1510bc821ef4359bba591630d2273662f5de0683205d908c60d",
        ".github/workflows/dashboard-ci.yml": (
            "1f4c4add5bae50bc11fc2c7693c9a7ed"
            "e79f89300da81b9f7a6904d30462dac7"
        ),
        ".github/workflows/release-readiness.yml": (
            "22fb9e9447770c5b463b028d9ef6195d"
            "f53fbc99b2e8a467ba11e7a2b58b167b"
        ),
    },
    "appolon1908/Breero.com": {
        ".github/workflows/quality.yml": "9e8367e853316594a325fbcb0f22f1e701c35c205b15e66a5228ae8b4ce10ce4",
    },
    "appolon1908/Moneybee-Backend": {
        ".github/workflows/ci.yml": (
            "0bed241476483a0ac38e0fc8bb2b06a2"
            "3b076645a6b0b355cf0420fcf4d2f451"
        ),
        ".github/workflows/release-backend-images.yml": (
            "1f14d41e27212554bb750403597ce726"
            "642527f61babf4e072d2cf2c03ab1345"
        ),
        ".github/workflows/secure-ci.yml": (
            "6ab4ebf30e47aee65ba3e1d7106ddd0c"
            "6feea546a57ebd289cf4fcfed9106e00"
        ),
    },
}
APPROVED_JOB_EXECUTABLE_CONFIGURATION_SHA256: dict[str, dict[str, str]] = {
    "appolon1908/Middleware-": {
        ".github/workflows/connector-runtime-api-ci.yml": "e62adfc9ea616b3a550987c9fa959c9a5c9cfeb66cbf54bc2becdc3e9d67a374",
        ".github/workflows/connector-storage-ci.yml": "39df4c72bffce26b181b1857d408664419164fd0dab10283d4fb98f52b5d3329",
        ".github/workflows/lead-automation-v1.yml": "68e8e4bf4820b13ff3cc0c2497404ebb217984728b1d2e8a64d6e862142b7bc5",
        ".github/workflows/integrated-monitoring.yml": "2afe0af0eadd53d8d5b61588781edab1d775bf17009266c4678fe3a9c41a39ec",
        ".github/workflows/odoo-calling-contract.yml": (
            "24c5daa68552448e498dcbceab8dc77df"
            "595d94ea868a13f5c942b57e949c10f"
        ),
    },
    "appolon1908/beyvra-backend": {
        ".github/workflows/email-boundary-ci.yml": "13ec97e8fb3cf77dcea400c2c8d4d5f089a567852ebcfa7f8efc581efa6f1fd6",
        ".github/workflows/enterprise-api.yml": "0d41ab216db01c92761c261f303ddc949b7eba43d4c7d323144028089e9ac99d",
        ".github/workflows/registration-safety-ci.yml": "8359be31987dbfc7b3d570ce12201fd0e94a21c71c8dec303052ef44a87fb25c",
        ".github/workflows/security-command-ci.yml": "a47a0f78eb38348b2c23e784e9048b1311297ffe30080c4b063048b296e66d41",
        ".github/workflows/workspace-api.yml": "abf3d41bfe718ecd343cd540ea27cd330c16294b192a7aa37f0435b895cc90b5",
    },
    "appolon1908/scrapper": {
        ".github/workflows/ci.yml": "31d81c5be094a1510bc821ef4359bba591630d2273662f5de0683205d908c60d",
        ".github/workflows/release-readiness.yml": "22fb9e9447770c5b463b028d9ef6195df53fbc99b2e8a467ba11e7a2b58b167b",
    },
    "appolon1908/Breero.com": {
        ".github/workflows/backend-production.yml": (
            "45b2918627995cb3491f55b3a3b537e"
            "4a34598d7b32877879b9e2c912c266591"
        ),
    },
}
APPROVED_OFFLINE_RUN_SHA256: dict[str, dict[str, frozenset[str]]] = {
    "appolon1908/Middleware-": {
        ".github/workflows/trusted-production-orchestrator-gate.yml": frozenset(
            {"6ceced166ce773d56bb54f544dee4508a60803465987e93cb8d719ee96df6df3"}
        ),
        ".github/workflows/production-orchestrator-contract.yml": frozenset(
            {"42481d485e47eb31f2e133ba690417a5a5927fccfb40ecd18836e5ad1ee3b1a9"}
        ),
    },
    "appolon1908/beyvra-backend": {
        ".github/workflows/certification-ci.yml": frozenset(
            {"90342c1a6aff24d02b18a064f6fc1affcddbe05fb894ccb22122b9a649358387"}
        ),
    },
}
APPROVED_DEFAULT_TEST_DISCOVERY_SOURCE_SHA256 = {
    "appolon1908/Middleware-": (
        "4bc320b1ae18cb4e0d97a1a3d5710638"
        "37476f3bcafde2086100687d89ea7954"
    ),
}
APPROVED_CONTROL_PLANE_DEPENDENCY_SHA256: dict[
    str, dict[str, dict[str, str]]
] = {
    "appolon1908/Middleware-": {
        ".github/workflows/portfolio-production-ruleset-apply.yml": {
            "config/ai-production-branch-ruleset.v1.json": "52db5e583b88edb069ba1d7b829d1f49ad820d0bb90e41bcf5b94e4074403ae1",
            "config/portfolio-repositories.v1.json": "48fca9da2480254051c2f5ca15292ef70e26847ee11dc8dcd102518f30a20c56",
            "scripts/apply_portfolio_production_ruleset.py": "31663d6f3101e593310b25a38035620d47a317d6a088193f6b550b730d0d39b0",
            "scripts/portfolio_ruleset/__init__.py": "054ac3779dc21008042eada02c91f85c57612afc37163592f1aa90b9ee4b6b18",
            "scripts/portfolio_ruleset/common.py": "59a8dd0ea3f6db4cc38eaa644fa9a8db5170cefd99ed4484dfcb3374ef24f0e7",
            "scripts/portfolio_ruleset/github_api.py": (
                "e0625083ed35b7a1fd46f67b3f91b7d"
                "166887f7d054d989dd2cac1d3d04dae6d"
            ),
            "scripts/portfolio_ruleset/rollout.py": "91ccf5b6b8f4b119dbb3dc451c42200ab00026b91751ce9f014bd4a0c4275022",
            "tests/test_portfolio_production_ruleset.py": "2101c6e8e141d8532efce9662cfaf2ad2aea4035044320d1a080254e8b4171ca",
        },
        ".github/workflows/portfolio-main-release-authorities.yml": {
            "config/portfolio-main-release-authorities.v1.json": (
                "4e06c764a7df13e3058fe9491039a7e3"
                "d6f9b3a1e2bc7bb2b28eba75a50c61df"
            ),
            "scripts/apply_portfolio_main_release_authorities.py": (
                "0615a2777ca1a00351b8710f014b40784dd74c0686557803941a27a1fbeee026"
            ),
            "scripts/apply_portfolio_release_reviewer_access.py": (
                "fbeb2e7cd6bb2c76c0ad5fadf384ffeb9027c9333f02cabfdfacf021754a240f"
            ),
            "tests/test_portfolio_main_release_authorities.py": (
                "5d3a931ddad6f2cb68a85deee345d10a045762e6c1433f93383c4644e071d664"
            ),
            "tests/test_portfolio_release_reviewer_access.py": (
                "60a604c93811083ef75b573e99501c4dd"
                "497495647dbf3d9998c0b7e8c102195"
            ),
        },
        ".github/workflows/integration-main-release-authorities.yml": {
            "config/integration-main-release-authorities.v1.json": (
                "4c05f84e3574c3a72f3b4ab589ab86d5"
                "4b7565e3dbeab51e66d8024b7d774d6e"
            ),
            "scripts/apply_integration_main_release_authorities.py": (
                "95b9c27abd309b1efe579672fdb8b89fe"
                "aed55e8e9334c50913b2e422ad771a7"
            ),
            "scripts/apply_integration_main_release_authorities_base.py": (
                "d8e632c5ae31354523943ebffae7d8ea"
            "6b59aef8e53c86e204c5ff513051c15e"
            ),
            "scripts/apply_integration_main_release_authorities_v2.py": (
                "7fc58fdc9311a2f97c8e12af218fcf16"
            "3e98213cc31a1988a8de3c6b6954abf4"
            ),
        },
        ".github/workflows/production-reviewer-access.yml": {
            "config/production-reviewer-access.v1.json": (
                "ef0f873f94d1be1943db1ef8ee23c926"
            "7bb2c153d897b7f33735cd2b545c4d8d"
            ),
            "scripts/apply_production_reviewer_access.py": (
                "c8e027b08636c9d35ac1e80fc2a5014f9105dd8df9329f6b2eaffd805b2f52dd"
            ),
            "scripts/apply_production_reviewer_access_base.py": (
                "c51a08b0bcdb070a4917e5d7ebf319cd"
            "fab60cd8a618235c0ffb23af9789ccdb"
            ),
        },
    },
}
APPROVED_UNRESOLVED_SCRIPT_TARGETS: dict[str, frozenset[str]] = {
    # Nuxt emits this checked-build output before the CI smoke-test step.
    # Repository-owned and working-directory-relative scripts are resolved and
    # inspected below; no deployment script belongs in this exception list.
    "appolon1908/Telnexa-web": frozenset({".output/server/index.mjs"}),
}
APPROVED_READ_ONLY_SCRIPT_INVOCATIONS: dict[
    str, dict[str, tuple[str, frozenset[tuple[str, ...]]]]
] = {
    "appolon1908/Keycloak": {
        "scripts/validate-repository-name-authority.py": (
            "d56d85a41734dc468efecb99d590d0d33267dcd1c84f4ff4dd3fa93c2076bd96",
            frozenset({(), ("--live",)}),
        ),
    },
    "appolon1908/Middleware-": {
        "scripts/run_ci.sh": (
            "64d7c92279dd442144c7e1f74c3e48f0ab5d5db105238a534dcf8ccd99e93138",
            frozenset({()}),
        ),
        "scripts/validate_middleware_authority_convergence.py": (
            "32de4c58a22737dfdea7f42149e3a5891756d777b2e890cb568cda4e57799e53",
            frozenset({()}),
        ),
        "scripts/apply_portfolio_main_release_authorities.py": (
            "0615a2777ca1a00351b8710f014b40784dd74c0686557803941a27a1fbeee026",
            frozenset({("--mode", "validate")}),
        ),
        "scripts/apply_portfolio_release_reviewer_access.py": (
            "fbeb2e7cd6bb2c76c0ad5fadf384ffeb9027c9333f02cabfdfacf021754a240f",
            frozenset({("--mode", "validate")}),
        ),
        "scripts/audit_release_endpoints.py": (
            "922655600ccaa1a0ba72fefd721bd6e370e4f0da430b7b4b0d858ed2826f067a",
            frozenset({()}),
        ),
        "scripts/apply_integration_main_release_authorities.py": (
            "95b9c27abd309b1efe579672fdb8b89feaed55e8e9334c50913b2e422ad771a7",
            frozenset({("--mode", "validate")}),
        ),
        "scripts/apply_integration_main_release_authorities_v2.py": (
            "7fc58fdc9311a2f97c8e12af218fcf163e98213cc31a1988a8de3c6b6954abf4",
            frozenset({("--mode", "validate")}),
        ),
        "scripts/apply_production_reviewer_access.py": (
            "c8e027b08636c9d35ac1e80fc2a5014f9105dd8df9329f6b2eaffd805b2f52dd",
            frozenset({("--mode", "validate")}),
        ),
        "scripts/apply_repository_governance.py": (
            "7b07e9aa7dc590198a05f8ea9fd969a7149b43a8a3039eb61561457fed6dfc71",
            frozenset({(), ("--apply",), ("--verify-live",)}),
        ),
    },
    "appolon1908/beyvra-backend": {
        "operations/one_click_readonly_release.py": (
            "66f853c64b440615179cddb3a67ad027017fec0d17d5ed56178ec5b2ce9173ba",
            frozenset({("--self-test",)}),
        ),
    },
    "appolon1908/beyvra-frontend": {
        "operations/verify_backend_certification.sh": (
            "ff6afa3966de2e67d7cceec60cc7e018b2cb7f8da261858e1d80b2a2a411e8f0",
            frozenset({()}),
        ),
    },
}
REQUIRED_NATIVE_WORKFLOWS: dict[str, dict[str, str]] = {
    "appolon1908/Infustruction-repo": {
        "runtime_certification": ".github/workflows/staging-readonly-certification.yml",
    },
    "appolon1908/Keycloak": {
        "plan_apply": ".github/workflows/deploy.yml",
        "drift_review": ".github/workflows/drift-review.yml",
    },
    "appolon1908/Middleware-": {
        "signed_release": ".github/workflows/release.yml",
        "runtime_certification": ".github/workflows/production-runtime-certification.yml",
    },
    "appolon1908/codestra": {
        "build_deploy": ".github/workflows/deploy.yml",
    },
}
ALLOWED_RELEASE_VALIDATOR_COMMAND_PREFIXES = {
    ("cosign", "verify"),
    ("cosign", "verify-attestation"),
    ("docker", "buildx", "imagetools", "inspect"),
    ("docker", "login"),
    ("gh", "attestation", "verify"),
    ("git", "ls-tree", "-r", "-z"),
    ("git", "rev-parse"),
    ("git", "status"),
}
ALLOWED_RELEASE_VALIDATOR_IMPORTS = {
    "__future__",
    "base64",
    "copy",
    "email",
    "hashlib",
    "io",
    "json",
    "os",
    "pathlib",
    "re",
    "subprocess",
    "sys",
    "tempfile",
    "typing",
    "urllib",
    "yaml",
    "zipfile",
}


class ContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def release_validator_security_fingerprint(source: str) -> str:
    """Bind release policy bytes without introducing a digest cycle.

    The normalized assignments contain contract-validator hashes or source-tree
    hashes that themselves include this validator. Their names, uniqueness, and
    presence remain bound; only their assigned values are normalized. Every
    other byte of the release validator is covered by this fingerprint.
    """

    try:
        tree = ast.parse(source, filename=str(RELEASE_VALIDATOR_PATH))
    except SyntaxError as error:
        raise ContractError("release-intent validator is not valid Python") from error
    source_bytes = source.encode("utf-8")
    line_starts = [0]
    for line in source_bytes.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    replacements: list[tuple[int, int, bytes]] = []
    seen: set[str] = set()
    for node in tree.body:
        names: list[str] = []
        binding_value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            binding_value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
            binding_value = node.value
        matched = set(names) & RELEASE_VALIDATOR_NON_SELF_REFERENTIAL_BINDINGS
        if not matched:
            continue
        require(
            len(names) == 1 and len(matched) == 1,
            "release-validator trust binding assignment is ambiguous",
        )
        name = names[0]
        require(name not in seen, "release-validator trust binding is assigned more than once")
        if binding_value is None:
            raise ContractError("release-validator trust binding has no value")
        if name == "EXPECTED_REQUIRED_CHECK_SOURCE_CLOSURE_SHA256":
            if not isinstance(binding_value, ast.Dict):
                raise ContractError("release-validator source closure binding is invalid")
            literal_bindings: dict[str, str] = {}
            for key_node, value_node in zip(binding_value.keys, binding_value.values):
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    bound_repository = key_node.value
                elif isinstance(key_node, ast.Name) and key_node.id == "CONTROLLER_REPOSITORY":
                    bound_repository = "appolon1908/codestra-production-platform"
                else:
                    raise ContractError("release-validator source closure key is not static")
                try:
                    bound_digest = ast.literal_eval(value_node)
                except (TypeError, ValueError) as error:
                    raise ContractError(
                        "release-validator source closure digest is not static"
                    ) from error
                require(
                    isinstance(bound_digest, str)
                    and re.fullmatch(r"[0-9a-f]{64}", bound_digest) is not None,
                    "release-validator source closure binding is invalid",
                )
                require(
                    bound_repository not in literal_bindings,
                    "release-validator source closure binding contains duplicates",
                )
                literal_bindings[bound_repository] = bound_digest
            require(
                set(literal_bindings)
                == set(EXPECTED_RELEASE_VALIDATOR_SECURITY_SHA256)
                | {"appolon1908/codestra-production-platform"},
                "release-validator source closure catalog is incomplete",
            )
        else:
            try:
                literal_value = ast.literal_eval(binding_value)
            except (TypeError, ValueError) as error:
                raise ContractError(
                    "release-validator trust binding is not a static literal"
                ) from error
            require(
                isinstance(literal_value, str)
                and re.fullmatch(r"[0-9a-f]{64}", literal_value) is not None,
                "release-validator contract hash binding is invalid",
            )
        end_lineno = binding_value.end_lineno
        end_col_offset = binding_value.end_col_offset
        if not isinstance(end_lineno, int) or not isinstance(end_col_offset, int):
            raise ContractError("release-validator trust binding location is unavailable")
        require(
            1 <= binding_value.lineno <= len(line_starts)
            and 1 <= end_lineno <= len(line_starts),
            "release-validator trust binding location is invalid",
        )
        replacements.append(
            (
                line_starts[binding_value.lineno - 1] + binding_value.col_offset,
                line_starts[end_lineno - 1] + end_col_offset,
                b'"<normalized-independent-trust-binding>"',
            )
        )
        seen.add(name)
    require(
        seen == RELEASE_VALIDATOR_NON_SELF_REFERENTIAL_BINDINGS,
        "release-validator non-self-referential trust bindings are incomplete",
    )
    for start, end, replacement in sorted(replacements, reverse=True):
        require(
            0 <= start < end <= len(source_bytes),
            "release-validator trust binding byte range is invalid",
        )
        source_bytes = source_bytes[:start] + replacement + source_bytes[end:]
    return hashlib.sha256(source_bytes).hexdigest()


def validate_release_validator_trust_root(source: str, repository: object) -> None:
    if not isinstance(repository, str):
        raise ContractError("release-validator repository identity is invalid")
    expected = EXPECTED_RELEASE_VALIDATOR_SECURITY_SHA256.get(repository)
    require(
        isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected) is not None,
        "release-validator independent trust root is missing",
    )
    require(
        release_validator_security_fingerprint(source) == expected,
        "release-validator independent trust root mismatch",
    )


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def require_mapping(value: object, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(message)
    return value


def require_string_list(value: object, message: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(message)
    if nonempty and not value:
        raise ContractError(message)
    if not all(isinstance(item, str) and item for item in value):
        raise ContractError(message)
    return [item for item in value if isinstance(item, str)]


def load_contract() -> dict[str, Any]:
    require(CONTRACT_PATH.is_file() and not CONTRACT_PATH.is_symlink(), "contract is missing or unsafe")
    value = json.loads(
        CONTRACT_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    require(isinstance(value, dict), "contract must be a JSON object")
    return value


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mappings."""


def construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        require(key not in result, "workflow contains a duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


@dataclass(frozen=True)
class WorkflowJob:
    data: dict[str, Any]
    raw: str
    working_directory: str | None
    shell: str | None
    environment: dict[str, Any]


def default_working_directory(value: dict[str, Any], path: str) -> str | None:
    defaults = value.get("defaults")
    if defaults is None:
        return None
    require(isinstance(defaults, dict), f"workflow defaults are invalid: {path}")
    run = defaults.get("run")
    if run is None:
        return None
    require(isinstance(run, dict), f"workflow run defaults are invalid: {path}")
    working_directory = run.get("working-directory")
    require(
        working_directory is None
        or isinstance(working_directory, str)
        and bool(working_directory),
        f"workflow working-directory is invalid: {path}",
    )
    return working_directory


def default_shell(value: dict[str, Any], path: str) -> str | None:
    defaults = value.get("defaults")
    if defaults is None:
        return None
    require(isinstance(defaults, dict), f"workflow defaults are invalid: {path}")
    run = defaults.get("run")
    if run is None:
        return None
    require(isinstance(run, dict), f"workflow run defaults are invalid: {path}")
    shell = run.get("shell")
    require(
        shell is None or isinstance(shell, str) and bool(shell),
        f"workflow shell is invalid: {path}",
    )
    return shell


def workflow_jobs(workflow: str, path: str) -> dict[str, WorkflowJob]:
    """Parse GitHub Actions jobs with YAML semantics and source spans."""

    try:
        document = yaml.load(workflow, Loader=UniqueKeyLoader)
        root = yaml.compose(workflow, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ContractError(f"workflow is not valid YAML: {path}") from exc
    require(isinstance(document, dict), f"workflow is not a mapping: {path}")
    workflow_working_directory = default_working_directory(document, path)
    workflow_shell = default_shell(document, path)
    workflow_environment = document.get("env", {})
    require(
        isinstance(workflow_environment, dict)
        and all(isinstance(name, str) and bool(name) for name in workflow_environment),
        f"workflow environment is invalid: {path}",
    )
    jobs_value = document.get("jobs")
    require(isinstance(jobs_value, dict) and bool(jobs_value), f"workflow has no jobs: {path}")
    require(isinstance(root, yaml.nodes.MappingNode), f"workflow root is invalid: {path}")
    jobs_node: yaml.nodes.MappingNode | None = None
    for key_node, value_node in root.value:
        if isinstance(key_node, yaml.nodes.ScalarNode) and key_node.value == "jobs":
            require(isinstance(value_node, yaml.nodes.MappingNode), f"workflow jobs are invalid: {path}")
            jobs_node = value_node
            break
    if jobs_node is None:
        raise ContractError(f"workflow jobs source is missing: {path}")
    lines = workflow.splitlines()
    result: dict[str, WorkflowJob] = {}
    for key_node, value_node in jobs_node.value:
        require(isinstance(key_node, yaml.nodes.ScalarNode), f"workflow job name is invalid: {path}")
        name = key_node.value
        data = jobs_value.get(name)
        require(isinstance(data, dict), f"workflow job is not a mapping: {path}:{name}")
        job_environment = data.get("env", {})
        require(
            isinstance(job_environment, dict)
            and all(isinstance(key, str) and bool(key) for key in job_environment),
            f"workflow job environment is invalid: {path}:{name}",
        )
        raw = "\n".join(lines[key_node.start_mark.line : value_node.end_mark.line]) + "\n"
        job_working_directory = default_working_directory(data, path)
        job_shell = default_shell(data, path)
        result[name] = WorkflowJob(
            data=data,
            raw=raw,
            working_directory=job_working_directory or workflow_working_directory,
            shell=job_shell or workflow_shell,
            environment={**workflow_environment, **job_environment},
        )
    require(set(result) == set(jobs_value), f"workflow job source mismatch: {path}")
    return result


def workflow_steps(job: WorkflowJob, path: str) -> list[dict[str, Any]]:
    value = job.data.get("steps")
    if value is None:
        return []
    require(isinstance(value, list), f"job steps are invalid: {path}")
    steps: list[dict[str, Any]] = []
    for item in value:
        require(isinstance(item, dict), f"workflow step is not a mapping: {path}")
        for key in ("env", "with"):
            require(
                key not in item or isinstance(item[key], dict),
                f"workflow step {key} is invalid: {path}",
            )
        steps.append(item)
    return steps


def step_working_directory(
    job: WorkflowJob,
    step: dict[str, Any],
    path: str,
) -> Path:
    value = step.get("working-directory", job.working_directory)
    if value is None:
        return ROOT
    require(
        isinstance(value, str)
        and bool(value)
        and "${{" not in value
        and "$" not in value,
        f"workflow working-directory is dynamic or invalid: {path}",
    )
    candidate = ROOT / value
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(ROOT.resolve())
    except (OSError, ValueError) as exc:
        raise ContractError(
            f"workflow working-directory is missing or unsafe: {path}"
        ) from exc
    require(
        resolved.is_dir() and not resolved.is_symlink(),
        f"workflow working-directory is unsafe: {path}",
    )
    return resolved


def shell_tokens(script: str) -> list[str]:
    try:
        lexer = shlex.shlex(
            script.replace("\\\n", " "),
            posix=True,
            punctuation_chars="|&;()\n",
        )
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = "#"
        return list(lexer)
    except ValueError:
        # Bash command substitutions and heredocs are richer than POSIX shlex.
        # A conservative token fallback keeps known runtime tools visible rather
        # than treating an unsupported shell construct as safe, and it must keep
        # every command boundary: a quoting failure in one command may not
        # merge a later publish/sign command into the first one.
        fallback: list[str] = []
        for line in script.replace("\\\n", " ").split("\n"):
            for segment in re.split(r"(\|\||&&|[;|&])", line):
                if segment in {"||", "&&", ";", "|", "&"}:
                    fallback.append(segment)
                else:
                    fallback.extend(re.findall(r"[A-Za-z0-9_./@${}:+-]+", segment))
            fallback.append("\n")
        return fallback


def shell_separator_token(token: str) -> bool:
    # shlex can coalesce adjacent punctuation such as a close parenthesis and
    # semicolon. Every token made only of separators ends the current command.
    return token in SHELL_SEPARATORS or (
        bool(token)
        and all(character in "\n&();|{}" for character in token)
        and any(character in "\n&();|" for character in token)
    )


def executable_name(token: str) -> str:
    return token.strip("$(){}[]").rsplit("/", 1)[-1]


def command_token_has_dynamic_executable(token: str) -> bool:
    """Reject expansion in the executable leaf, while allowing fixed path leaves."""

    return "$" in token.rsplit("/", 1)[-1]


def absolute_executable_is_unproved(token: str) -> bool:
    return Path(token).is_absolute() and not token.startswith(("/bin/", "/usr/bin/"))


def shell_command_bindings(
    tokens: list[str],
    before_index: int | None = None,
) -> dict[str, str]:
    """Return assignments that are effective before a command token.

    Only assignment words in command position establish bindings. Arguments
    such as ``echo tool=echo`` are not assignments, and later assignments must
    not retroactively change an earlier variable executable.
    """

    bindings: dict[str, str] = {}
    expect_command = True
    control = {"coproc", "do", "elif", "else", "if", "then", "until", "while"}
    for index, token in enumerate(tokens):
        if before_index is not None and index >= before_index:
            break
        if shell_separator_token(token):
            expect_command = True
            continue
        if token in control:
            expect_command = True
            continue
        if not expect_command:
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.+)", token, re.DOTALL)
        if match is not None:
            bindings[match.group(1)] = match.group(2)
            continue
        expect_command = False
    return bindings


def resolved_command_token(token: str, bindings: dict[str, str]) -> str:
    match = re.fullmatch(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", token)
    if match is None:
        return token
    return bindings.get(match.group(1) or match.group(2), token)


def shell_command_substitutions(script: str) -> tuple[list[str], str] | None:
    """Return ``$()`` bodies and shell with those bodies safely elided."""

    def substitution_end(start: int) -> int | None:
        depth = 1
        quote: str | None = None
        escaped = False
        comment = False
        at_word_start = True
        index = start
        while index < len(script):
            character = script[index]
            if comment:
                if character == "\n":
                    comment = False
                    at_word_start = True
                index += 1
                continue
            if escaped:
                escaped = False
                at_word_start = False
                index += 1
                continue
            if character == "\\" and quote != "'":
                escaped = True
                index += 1
                continue
            if quote == "'":
                if character == "'":
                    quote = None
                index += 1
                continue
            if character == "'" and quote is None:
                quote = "'"
                at_word_start = False
            elif character == '"':
                quote = None if quote == '"' else '"'
                at_word_start = False
            elif character == "#" and quote is None and at_word_start:
                comment = True
            elif character == "`":
                return None
            elif character == "$" and index + 1 < len(script) and script[index + 1] == "(":
                if index + 2 >= len(script) or script[index + 2] != "(":
                    depth += 1
                    index += 1
                at_word_start = False
            elif quote is None and character == "(":
                depth += 1
                at_word_start = True
            elif quote is None and character == ")":
                depth -= 1
                if depth == 0:
                    return index
                at_word_start = False
            elif quote is None and (character.isspace() or character in ";|&{}"):
                at_word_start = True
            else:
                at_word_start = False
            index += 1
        return None

    payloads: list[str] = []
    sanitized: list[str] = []
    previous_end = 0
    quote: str | None = None
    escaped = False
    comment = False
    at_word_start = True
    index = 0
    while index < len(script):
        character = script[index]
        if comment:
            if character == "\n":
                comment = False
                at_word_start = True
            index += 1
            continue
        if escaped:
            escaped = False
            at_word_start = False
            index += 1
            continue
        if character == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if character == "'" and quote is None:
            quote = "'"
            at_word_start = False
        elif character == '"':
            quote = None if quote == '"' else '"'
            at_word_start = False
        elif character == "#" and quote is None and at_word_start:
            comment = True
        elif character == "`":
            return None
        elif character == "$" and index + 1 < len(script) and script[index + 1] == "(":
            if index + 2 < len(script) and script[index + 2] == "(":
                at_word_start = False
            else:
                end = substitution_end(index + 2)
                if end is None:
                    return None
                payloads.append(script[index + 2 : end])
                sanitized.extend((script[previous_end:index], "SUBSTITUTION"))
                previous_end = end + 1
                index = end
                at_word_start = False
        elif quote is None and (character.isspace() or character in ";|&(){}"):
            at_word_start = True
        else:
            at_word_start = False
        index += 1
    sanitized.append(script[previous_end:])
    return payloads, "".join(sanitized)


def command_indexes(tokens: list[str]) -> list[int]:
    indexes: list[int] = []
    expect_command = True
    control = {"coproc", "do", "elif", "else", "if", "then", "until", "while"}
    skip_through = -1
    for index, token in enumerate(tokens):
        if index <= skip_through:
            continue
        if token == "[[" and expect_command:
            try:
                skip_through = tokens.index("]]", index + 1)
            except ValueError:
                indexes.append(index)
                expect_command = False
            continue
        if shell_separator_token(token):
            expect_command = True
            continue
        if token in control:
            expect_command = True
            continue
        if not expect_command:
            continue
        if token in {"[", "[["}:
            terminator = "]" if token == "[" else "]]"
            try:
                skip_through = tokens.index(terminator, index + 1)
            except ValueError:
                indexes.append(index)
                expect_command = False
                continue
            expect_command = False
            continue
        if executable_name(token) in SHELL_WRAPPERS:
            resolved = wrapped_executable_index(tokens, index)
            if resolved is None:
                expect_command = False
                continue
            indexes.append(resolved)
            skip_through = resolved
            expect_command = False
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue
        indexes.append(index)
        expect_command = False
    return indexes


def wrapped_executable_index(tokens: list[str], start: int) -> int | None:
    """Resolve common shell wrappers without treating their options as commands."""

    no_value_options = {
        "!": set(),
        "command": {"--"},
        "env": {"--", "--ignore-environment", "--null", "-0", "-i"},
        "exec": {"--", "-c", "-l"},
        "nohup": set(),
        "sudo": {
            "--",
            "--preserve-env",
            "-E",
            "-H",
            "-K",
            "-S",
            "-b",
            "-k",
            "-n",
        },
        "systemd-run": {"--", "--wait"},
        "time": {"--", "-a", "-p", "-v"},
    }
    value_options = {
        "env": {"--chdir", "--split-string", "--unset", "-c", "-s", "-u"},
        "exec": {"-a"},
        "sudo": {
            "--chdir",
            "--chroot",
            "--close-from",
            "--command-timeout",
            "--group",
            "--host",
            "--prompt",
            "--user",
            "-c",
            "-g",
            "-p",
            "-r",
            "-t",
            "-u",
        },
        "time": {"--format", "--output", "-f", "-o"},
    }
    index = start
    while index < len(tokens):
        wrapper = executable_name(tokens[index])
        if wrapper not in no_value_options:
            return index
        index += 1
        if wrapper == "command" and index < len(tokens) and tokens[index] in {"-v", "-V"}:
            return None
        while index < len(tokens):
            token = tokens[index]
            lower = token.lower()
            option_key = lower if token.startswith("--") else token
            if shell_separator_token(token):
                return None
            if wrapper == "env" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                index += 1
                continue
            if option_key in no_value_options[wrapper]:
                index += 1
                continue
            if wrapper == "sudo" and lower.startswith("--preserve-env="):
                index += 1
                continue
            if option_key in value_options.get(wrapper, set()):
                index += 2
                continue
            if any(
                lower.startswith(f"{option}=")
                for option in value_options.get(wrapper, set())
                if option.startswith("--")
            ):
                index += 1
                continue
            if token.startswith("-"):
                # Unknown wrapper options are ambiguous, so classify the
                # wrapper itself as unsafe rather than skipping a payload.
                return start
            break
    return None


def raw_command_arguments(tokens: list[str], index: int) -> list[str]:
    arguments: list[str] = []
    for token in tokens[index + 1 :]:
        if shell_separator_token(token):
            break
        arguments.append(token)
    return arguments


def command_arguments(tokens: list[str], index: int) -> list[str]:
    return [executable_name(token) for token in raw_command_arguments(tokens, index)]


def command_consumes_pipeline(tokens: list[str], index: int) -> bool:
    return index > 0 and tokens[index - 1] == "|"


def runtime_cli_operation_is_dynamic(name: str, arguments: list[str]) -> bool:
    """Fail closed only when a runtime CLI's operation token is unresolved."""

    value_options = {
        "helm": {"--kube-apiserver", "--kube-context", "--kube-token", "--namespace", "-n"},
        "kubectl": {"--context", "--kubeconfig", "--namespace", "--server", "--token", "-n", "-s"},
        "terraform": {"-chdir"},
        "tofu": {"-chdir"},
    }.get(name, set())
    skip_value = False
    for token in arguments:
        if skip_value:
            skip_value = False
            continue
        lower = token.lower()
        option = lower.split("=", 1)[0]
        if option in value_options:
            skip_value = "=" not in token
            continue
        if token.startswith("-"):
            continue
        return "$" in token or "${{" in token
    return False


def xargs_payload(arguments: list[str]) -> str | None:
    """Return a statically delimited xargs command, or fail closed with None."""

    value_options = {
        "--arg-file",
        "--delimiter",
        "--max-args",
        "--max-chars",
        "--max-lines",
        "--max-procs",
        "--process-slot-var",
        "--replace",
        "-a",
        "-d",
        "-i",
        "-l",
        "-n",
        "-p",
        "-s",
    }
    no_value_options = {
        "--exit",
        "--no-run-if-empty",
        "--null",
        "--open-tty",
        "--show-limits",
        "--verbose",
        "-0",
        "-r",
        "-t",
        "-x",
    }
    index = 0
    while index < len(arguments):
        token = arguments[index]
        lower = token.lower()
        if token == "SUBSTITUTION" or "$" in token:
            # Expansions can move the option/command boundary or synthesize a
            # replacement option after static parsing.
            return None
        if (
            lower in {"--replace", "-i"}
            or lower.startswith("--replace=")
            or token.startswith("-I")
        ):
            # Replacement input can become the executable itself, so no
            # static payload remains to prove read-only.
            return None
        if lower in no_value_options:
            index += 1
            continue
        if lower in value_options:
            if index + 1 >= len(arguments):
                return None
            index += 2
            continue
        if any(
            lower.startswith(f"{option}=")
            for option in value_options
            if option.startswith("--")
        ):
            index += 1
            continue
        if token.startswith("-"):
            return None
        payload_tokens = arguments[index:]
        payload_name = executable_name(payload_tokens[0])
        if payload_name in SHELL_INTERPRETERS | SCRIPT_INTERPRETERS:
            inline = interpreter_payload(payload_tokens, 0)
            if inline == "" or (
                inline is None
                and interpreter_script_target(payload_tokens, 0) is None
            ):
                # xargs appends stdin items after the fixed arguments. For
                # inline interpreters or an interpreter without a fixed script,
                # that input becomes executable code or a script path.
                return None
        return " ".join(payload_tokens)
    # With no explicit command, xargs invokes echo and cannot launch a hidden
    # repository/runtime executable.
    return ""


def interpreter_payload(tokens: list[str], index: int) -> str | None:
    name = executable_name(tokens[index])
    if name == "eval":
        return tokens[index + 1] if index + 1 < len(tokens) else ""
    if name not in SHELL_INTERPRETERS | SCRIPT_INTERPRETERS:
 