#!/usr/bin/env bash
# Container-level verification: the built image, with the real AX runner as
# PID 1, driven the way AX drives it, but without a cluster.
#
#   verify/container.sh <image>
#
# The runner reads the Task and Workspace from files (its documented way to
# run outside AX). A named volume stands in for the durable /workspace, and
# `docker stop` + a NEW container on the same volume stands in for suspend and
# resume: AX's runner gets SIGTERM, and the resumed task sees the same files
# with a new process tree. `docker exec` stands in for `ax ssh`.
#
# Phases 1-4 use tests/fakes/orbi so the tick's outcome is controllable.
# Phase 5 runs the REAL Orbi and Pi binaries from the image.
#
# Not covered here (needs a cluster): the gVisor sandbox, Agent Substrate's
# snapshot of /workspace, the Gateway's egress rules and credential injection,
# and `ax ssh` itself.
set -euo pipefail

IMAGE="${1:?usage: verify/container.sh <image>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ID="ax-orbi-verify-$$"
VOL="$ID-workspace"
SPEC="$(mktemp -d)"
N=0
PASSED=0

cleanup() {
  docker rm -f "$ID" >/dev/null 2>&1 || true
  docker volume rm -f "$VOL" >/dev/null 2>&1 || true
  rm -rf "$SPEC"
}
trap cleanup EXIT

pass() { PASSED=$((PASSED + 1)); printf '  ok    %s\n' "$1"; }
fail() {
  printf '  FAIL  %s\n' "$1" >&2
  echo "--- container log ---" >&2
  docker logs "$ID" 2>&1 | tail -40 >&2 || true
  exit 1
}
phase() { N=$((N + 1)); printf '\n[%d] %s\n' "$N" "$1"; }
agent() { docker exec "$ID" /usr/local/bin/ax-orbi-agent "$@"; }
# field <python expression over `s`>: evaluate against the agent's status JSON
field() { agent status --json --limit 50 | python3 -c "import json,sys; s=json.load(sys.stdin); print($1)"; }
run_field() { agent status --json --run "$1" | python3 -c "import json,sys; r=json.load(sys.stdin)['runs'][0]; print(r.get('$2'))"; }
wait_for() { # wait_for <description> <command...>
  local what="$1"; shift
  for _ in $(seq 1 120); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 0.5
  done
  fail "timed out waiting for $what"
}
run_is() { [ "$(run_field "$1" status)" = "$2" ]; }

write_specs() { # write_specs <fake|real>
  local extra=""
  if [ "$1" = fake ]; then
    extra='
    - name: AX_ORBI_ORBI_BIN
      value: "/fakes/orbi"
    - name: FAKE_ORBI_MODE_FILE
      value: "/workspace/.fake-mode"
    - name: FAKE_ORBI_LOG
      value: "/workspace/.fake-ticks"'
  fi
  cat > "$SPEC/workspace.yaml" <<'EOF'
apiVersion: ax.io/v1alpha1
kind: Workspace
metadata:
  name: orbi-verify-ws
  atespace: default
spec:
  profile:
    image: "localhost/ax-orbi@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  setup:
    commands:
      # Stands in for spec.setup.git, which needs the network.
      - >-
        git init -q -b main repo &&
        git -C repo -c user.name=verify -c user.email=verify@example.com
        commit -q --allow-empty -m init &&
        git -C repo remote add origin https://github.com/example/demo.git &&
        echo setup-ran >> /workspace/.setup-count
EOF
  cat > "$SPEC/task.yaml" <<EOF
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: orbi-verify
  atespace: default
spec:
  profile:
    image: "localhost/ax-orbi@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  commands: ["/usr/local/bin/ax-orbi-agent", "serve"]
  workspace:
    name: orbi-verify-ws
  env:
    - name: AX_ORBI_CONFIG
      value: '{"source_repo": "example/demo", "base_branch": "main", "pi": {"provider": "verify", "model": "verify-model", "base_url": "https://model.invalid"}}'
    - name: GH_TOKEN
      value: "placeholder"
    - name: PI_API_KEY
      value: "placeholder"$extra
EOF
}

start() { # a fresh container on the same volume
  docker rm -f "$ID" >/dev/null 2>&1 || true
  docker run -d --name "$ID" -v "$VOL:/workspace" -v "$SPEC:/spec:ro" \
    -v "$ROOT/tests/fakes:/fakes:ro" "$IMAGE" \
    --task-file /spec/task.yaml --workspace-file /spec/workspace.yaml --port 8080 >/dev/null
  wait_for "the runner's /healthz" docker exec "$ID" curl -fsS http://127.0.0.1:8080/healthz
  wait_for "the workspace to be ready and the command started" \
    docker exec "$ID" curl -fsS "http://127.0.0.1:8080/readyz?check=workspace"
  wait_for "the supervisor to finish preparing" sh -c "docker exec $ID /usr/local/bin/ax-orbi-agent status --json | grep -q '\"started\": true'"
}
ticks() { docker exec "$ID" sh -c 'cat /workspace/.fake-ticks 2>/dev/null | wc -l' | tr -d ' '; }
mode() { docker exec "$ID" sh -c "echo $1 > /workspace/.fake-mode"; }

docker volume create "$VOL" >/dev/null

phase "startup: the AX runner prepares the workspace and starts the agent; no delivery begins"
write_specs fake
start
[ "$(docker exec "$ID" cat /proc/1/comm)" = ax-runner ] && pass "ax-runner is PID 1" || fail "PID 1 is not ax-runner"
[ "$(field "s['prepare']['ok']")" = True ] && pass "agent prepared the environment" \
  || fail "prepare failed: $(field "[c for c in s['prepare']['checks'] if not c['ok']]")"
[ "$(field "s['health']['state']")" = idle ] && pass "supervisor is idle" || fail "supervisor is not idle"
[ "$(ticks)" = 0 ] && pass "no tick ran during startup" || fail "a tick ran during startup"
docker exec "$ID" curl -fsS http://127.0.0.1:8099/metadata/v1alpha1/ax/task | grep -q "orbi-verify" \
  && pass "runner serves task metadata on loopback" || fail "no task metadata"

phase "command completion: a tick's real outcome is recorded"
mode ok
agent request-tick --id t-ok >/dev/null
wait_for "t-ok to finish" run_is t-ok succeeded
[ "$(run_field t-ok exitCode)" = 0 ] && pass "successful tick recorded as succeeded, exit 0" || fail "t-ok exit code"
mode fail
agent request-tick --id t-fail >/dev/null
wait_for "t-fail to finish" run_is t-fail failed
[ "$(run_field t-fail exitCode)" = 1 ] && pass "failing tick recorded as failed, exit 1" || fail "t-fail exit code"
agent logs --run t-fail | grep -q "runner failed: simulated" && pass "the tick's output is kept" || fail "log missing"
agent request-tick --id t-ok | grep -q '"duplicate": true' && sleep 2 && [ "$(ticks)" = 2 ] \
  && pass "repeating a request id does not run it again" || fail "duplicate request ran again"

phase "interruption: a stop mid-tick is recorded and the work is preserved"
mode hang
agent request-tick --id t-long >/dev/null
wait_for "t-long to start" run_is t-long running
wait_for "the tick process" sh -c "[ \"\$(docker exec $ID sh -c 'wc -l < /workspace/.fake-ticks')\" -ge 3 ]"
docker exec "$ID" sh -c 'echo unpushed > /workspace/repo/wip.txt'
docker stop -t 20 "$ID" >/dev/null   # SIGTERM to the runner, as on suspend
docker logs "$ID" 2>&1 | grep -q "got-sigterm\|interrupted\|signal" || true
docker rm "$ID" >/dev/null

phase "persistence and recovery: a new container on the same volume"
start
run_is t-long interrupted && pass "the interrupted tick is recorded as interrupted" \
  || fail "t-long is $(run_field t-long status)"
agent logs --run t-long | grep -q "got-sigterm" && pass "the tick received SIGTERM through the runner" \
  || fail "the tick was not signalled cleanly"
[ "$(docker exec "$ID" cat /workspace/repo/wip.txt)" = unpushed ] && pass "uncommitted work survived" || fail "work lost"
[ "$(docker exec "$ID" sh -c 'wc -l < /workspace/.setup-count' | tr -d ' ')" = 1 ] \
  && pass "workspace setup did not run a second time" || fail "workspace setup re-ran"
run_is t-ok succeeded && run_is t-fail failed && pass "earlier run records survived" || fail "run records lost"
sleep 3
[ "$(ticks)" = 3 ] && pass "the interrupted tick was not re-executed" || fail "tick count is $(ticks), expected 3"
mode ok
agent request-tick --id t-after >/dev/null
wait_for "t-after to finish" run_is t-after succeeded
pass "a new tick runs after recovery"

phase "the real Orbi and Pi binaries, with the generated configuration"
docker stop -t 20 "$ID" >/dev/null && docker rm "$ID" >/dev/null
write_specs real
start
[ "$(field "s['prepare']['ok']")" = True ] && pass "prepare passes with the real tools" \
  || fail "prepare failed: $(field "[c for c in s['prepare']['checks'] if not c['ok']]")"
field "[c['detail'] for c in s['prepare']['checks'] if c['name'].startswith('tool:')]"
agent request-tick --id t-real >/dev/null
wait_for "the real tick to finish" sh -c "docker exec $ID /usr/local/bin/ax-orbi-agent status --json --run t-real | grep -Eq '\"status\": \"(succeeded|failed)\"'"
echo "  real tick: status=$(run_field t-real status) exit=$(run_field t-real exitCode)"
echo "  --- real Orbi output (last 12 lines) ---"
agent logs --run t-real --tail 12 | sed 's/^/  | /'
# With a placeholder token and no gateway there is nothing to deliver, so the
# only acceptable outcome is that Orbi itself started, read the generated
# config, and stopped with its own diagnosis.
if agent logs --run t-real --tail 200 | grep -Eqi "orbi.toml|unknown key|invalid config|config error|Traceback"; then
  fail "Orbi rejected the generated configuration"
fi
[ "$(run_field t-real status)" = failed ] && pass "Orbi ran and its failure was recorded, not hidden" \
  || fail "expected the unauthenticated tick to fail, got $(run_field t-real status)"

printf '\n%d checks passed\n' "$PASSED"
