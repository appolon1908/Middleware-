#!/usr/bin/env bash
# Install only on the disposable GitHub-hosted runner selected by required-ci.
# These context checks prevent accidental use on an existing persistent host;
# source review and GitHub's runner selection remain the execution authority.
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted ||
      "${RUNNER_OS:-}" != Linux || "${RUNNER_ARCH:-}" != X64 ||
      "${ImageOS:-}" != ubuntu24 || -z "${ImageVersion:-}" ]]; then
  echo "Refusing egress-guard installation outside GitHub-hosted Ubuntu 24.04" >&2
  exit 1
fi

guard="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/codestra-ci-egress-guard"
expected=881f3c1f592dd645659cf46121625f50464644ef0685c4f752a1e46831e56b5a
test -f "$guard" && test ! -L "$guard"
printf '%s  %s\n' "$expected" "$guard" | sha256sum --check --strict
sudo -n install -o root -g root -m 0755 -- "$guard" /usr/local/sbin/codestra-ci-egress-guard
test "$(sudo -n stat -c '%u:%g:%a' -- /usr/local/sbin/codestra-ci-egress-guard)" = '0:0:755'
test "$(sudo -n sha256sum -- /usr/local/sbin/codestra-ci-egress-guard | cut -d' ' -f1)" = "$expected"
printf 'CI_RUNTIME_EGRESS_GUARD_INSTALL=PASS sha256=%s\n' "$expected"
