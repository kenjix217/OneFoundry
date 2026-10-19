#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
if [ "$#" -gt 0 ]; then
  uv_bin=$1
else
  uv_bin=$(command -v uv || printf '.bootstrap-venv/bin/uv')
fi
tmp_env=$(mktemp)
project="onefoundry_check_$$"
qa_owned_image=
cleanup() {
  docker compose --env-file "$tmp_env" -p "$project" --profile test down -v --remove-orphans >/dev/null 2>&1 || true
  if [ -n "$qa_owned_image" ]; then
    docker image rm "$qa_owned_image" >/dev/null 2>&1 || true
  fi
  rm -f "$tmp_env"
}
trap cleanup EXIT INT TERM
chmod 600 "$tmp_env"
password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')
app_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')
printf 'DB_PASSWORD=%s\nAPP_DB_PASSWORD=%s\n' "$password" "$app_password" > "$tmp_env"
docker compose --env-file "$tmp_env" -p "$project" --profile test run --build --rm test
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_docker_egress_proof.py
"$uv_bin" run --locked pytest -q tests/test_cli_image_admission.py tests/test_codex_cli_admission.py tests/test_claude_cli_admission.py
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_codex_local_responses_proof.py
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_codex_broker_proof.py -m integration
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_codex_team_appserver_proof.py -m integration
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_github_snapshot_codex_fixture_proof.py -m integration
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_claude_workorder_fixture.py -m integration
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_external_offline_supervisor.py -m integration
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_isolated_fixture_test.py -m integration

repository_qa_image=${ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE:-}
if [ -z "$repository_qa_image" ]; then
  repository_qa_image="${project}-repository-qa:latest"
  docker build --target repository-qa -t "$repository_qa_image" .
  qa_owned_image=$repository_qa_image
fi
repository_qa_image_id=$(docker image inspect --format '{{.Id}}' "$repository_qa_image")
if ! printf '%s\n' "$repository_qa_image_id" | LC_ALL=C grep -Eq '^sha256:[0-9a-f]{64}$'; then
  printf '%s\n' 'Repository QA image inspection did not return an immutable image ID.' >&2
  exit 1
fi
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE="$repository_qa_image_id" "$uv_bin" run --locked pytest -q tests/test_repository_code.py tests/test_repository_failure_evidence.py -m integration

ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_repository_provider.py tests/test_repository_codex_observation.py -m integration
ONEFOUNDRY_EGRESS_TEST_IMAGE="${project}-test:latest" "$uv_bin" run --locked pytest -q tests/test_repository_transport.py -m integration
"$uv_bin" run --locked pytest -q tests/test_maintenance_checkpoint.py -m integration
