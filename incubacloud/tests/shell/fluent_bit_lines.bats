#!/usr/bin/env bats
# The security lines of F0 (Debian 13, OpenSSH 10) through the real Fluent
# Bit image, with the configuration host_observability.yml renders, into a
# Loki of the central's version.
#
# Needs Docker and pulls two images, so it runs only when asked:
#   IC_FLUENT_BIT_TEST=1 bats incubacloud/tests/shell/fluent_bit_lines.bats
#
# The journal input is the one thing replaced: a JSON file with the
# journal's fields stands in for it (the real one is tried on a server).
# Everything else is as rendered, run as the compose file runs it: root
# without capabilities but reading others' files, a read-only filesystem
# and 128 MB.

bats_require_minimum_version 1.5.0

FB_IMAGE=fluent/fluent-bit:5.1.5
LOKI_IMAGE=grafana/loki:2.9.8

setup_file() {
    [ "${IC_FLUENT_BIT_TEST:-}" = 1 ] || skip "IC_FLUENT_BIT_TEST=1 runs it (Docker)"
    TMP="$(mktemp -d)"
    export TMP
    export NAME="ic-fbtest-$$"
    local here="${BATS_TEST_DIRNAME}"
    mkdir -p "$TMP/etc" "$TMP/log/audit"
    python3 "$here/../_agents_playbook.py" "$TMP/etc"
    # The journal's stand-in: the same tag, the journal's field names.
    python3 - "$TMP/etc/fluent-bit.yaml" <<'PY'
import sys, yaml
path = sys.argv[1]
config = yaml.safe_load(open(path))
config["parsers"] = [{"name": "journal_json", "format": "json"}]
config["pipeline"]["inputs"][0] = {
    "name": "tail", "tag": "journal", "path": "/var/log/journal.jsonl",
    "parser": "journal_json", "db": "/var/lib/fluent-bit/journal.db",
    "read_from_head": False, "storage.type": "filesystem",
}
for source in config["pipeline"]["inputs"]:
    source["refresh_interval"] = 1
yaml.safe_dump(config, open(path, "w"), sort_keys=False)
PY
    chmod 600 "$TMP/etc/fluent-bit.yaml"
    : > "$TMP/log/journal.jsonl"
    : > "$TMP/log/audit/audit.log"
    : > "$TMP/log/fail2ban.log"
    docker network create "$NAME" >/dev/null
    docker run -d --name "$NAME-loki" --network "$NAME" --network-alias loki \
        -p 127.0.0.1::3100 "$LOKI_IMAGE" -config.file=/etc/loki/local-config.yaml >/dev/null
    docker run -d --name "$NAME-fb" --network "$NAME" \
        --read-only --cap-drop ALL --cap-add DAC_READ_SEARCH \
        --security-opt no-new-privileges:true --memory 128m \
        -v "$TMP/etc:/fluent-bit/etc:ro" -v "$NAME-data:/var/lib/fluent-bit" \
        -v "$TMP/log:/var/log:ro" -p 127.0.0.1::2021 -p 127.0.0.1::2020 \
        "$FB_IMAGE" /fluent-bit/bin/fluent-bit -c /fluent-bit/etc/fluent-bit.yaml >/dev/null
    LOKI="http://$(docker port "$NAME-loki" 3100 | head -1)"
    METRICS="http://$(docker port "$NAME-fb" 2021 | head -1)"
    AGENT="http://$(docker port "$NAME-fb" 2020 | head -1)"
    export LOKI METRICS AGENT
    # Loki and Fluent Bit up, then the lines, written after the start as
    # a server writes them.
    for _ in $(seq 60); do
        curl -fs "$LOKI/ready" >/dev/null && curl -fs "$AGENT/api/v1/health" >/dev/null && break
        sleep 1
    done
    sleep 3
    cat "$here/fixtures/fluent_bit/journal.jsonl" >> "$TMP/log/journal.jsonl"
    cat "$here/fixtures/fluent_bit/audit.log" >> "$TMP/log/audit/audit.log"
    cat "$here/fixtures/fluent_bit/fail2ban.log" >> "$TMP/log/fail2ban.log"
    for _ in $(seq 60); do
        [ "$(loki_count auditd)" = 5 ] && [ "$(loki_count fail2ban)" = 7 ] &&
            [ "$(loki_count sshd)" = 6 ] && [ "$(loki_count sudo)" = 5 ] && break
        sleep 1
    done
    curl -fs "$METRICS/metrics" > "$TMP/metrics.txt"
}

teardown_file() {
    [ -n "${NAME:-}" ] || return 0
    docker logs "$NAME-fb" > "${TMP}/fb.log" 2>&1 || true
    docker rm -f "$NAME-fb" "$NAME-loki" >/dev/null 2>&1 || true
    docker volume rm "$NAME-data" >/dev/null 2>&1 || true
    docker network rm "$NAME" >/dev/null 2>&1 || true
    rm -rf "$TMP"
}

# loki_count SOURCE: how many lines of SOURCE Loki holds.
loki_count() {
    curl -fsG "$LOKI/loki/api/v1/query_range" \
        --data-urlencode "query={log_type=\"security\",source=\"$1\"}" \
        --data-urlencode "limit=1000" --data-urlencode "since=1h" |
        python3 -c 'import json,sys; print(sum(len(s["values"]) for s in json.load(sys.stdin)["data"]["result"]))'
}

# counter SERIES: the value of one exported series, or "none".
counter() {
    local line
    line=$(grep -v '^#' "$TMP/metrics.txt" | grep -F "$1 " | head -1)
    [ -n "$line" ] && echo "${line##* }" || echo none
}

@test "one failed SSH connection counts once" {
    [ "$(counter 'ic_ssh_auth_failed_total')" = 3 ]
}

@test "a sign-in with the panel's key is the panel's" {
    [ "$(counter 'ic_ssh_accepted_total{kind="panel",method="publickey"}')" = 1 ]
    [ "$(counter 'ic_ssh_accepted_total{kind="other",method="publickey"}')" = 1 ]
}

@test "a refused sudo is not a command run" {
    [ "$(counter 'ic_sudo_total{kind="panel_user"}')" = 1 ]
    [ "$(counter 'ic_sudo_total{kind="ansible"}')" = 1 ]
    [ "$(counter 'ic_sudo_total{kind="other"}')" = 1 ]
    [ "$(counter 'ic_sudo_failed_total')" = 1 ]
}

@test "fail2ban's events by jail, already banned among them" {
    [ "$(counter 'ic_f2b_total{jail="sshd",action="found"}')" = 1 ]
    [ "$(counter 'ic_f2b_total{jail="sshd",action="ban"}')" = 2 ]
    [ "$(counter 'ic_f2b_total{jail="sshd",action="unban"}')" = 1 ]
    [ "$(counter 'ic_f2b_total{jail="sshd",action="already_banned"}')" = 1 ]
    [ "$(counter 'ic_f2b_total{jail="incubacloud-weblogin",action="found"}')" = 1 ]
    [ "$(counter 'ic_f2b_total{jail="incubacloud-weblogin",action="ban"}')" = 1 ]
    ! grep -q recidive "$TMP/metrics.txt"
}

@test "auditd's who by auid: root, the panel's uid, systemd, anyone else" {
    [ "$(counter 'ic_audit_events_total{key="passwd_changes",who="root"}')" = 1 ]
    [ "$(counter 'ic_audit_events_total{key="ssh_config_changes",who="panel"}')" = 1 ]
    [ "$(counter 'ic_audit_events_total{key="ssh_config_changes",who="system"}')" = 1 ]
    [ "$(counter 'ic_audit_events_total{key="sudoers_changes",who="user"}')" = 1 ]
    ! grep -q auth_log "$TMP/metrics.txt"
}

@test "every line read is counted, sent or not" {
    [ "$(counter 'ic_security_lines_total{source="sshd"}')" = 6 ]
    [ "$(counter 'ic_security_lines_total{source="sudo"}')" = 5 ]
    [ "$(counter 'ic_security_lines_total{source="fail2ban"}')" = 9 ]
    [ "$(counter 'ic_security_lines_total{source="auditd"}')" = 8 ]
}

@test "auditd sends its four keys' SYSCALL and their files' PATH only" {
    [ "$(loki_count auditd)" = 5 ]
}

@test "the login page's jail sends its bans only, other jails nothing" {
    [ "$(loki_count fail2ban)" = 7 ]
}

@test "Loki's streams carry log_type, host and source only" {
    run curl -fs "$LOKI/loki/api/v1/labels"
    [ "$status" -eq 0 ]
    labels=$(python3 -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin)["data"])))' <<<"$output")
    [ "$labels" = "host log_type source" ]
}

@test "a line reaches Loki as the line itself" {
    run curl -fsG "$LOKI/loki/api/v1/query_range" \
        --data-urlencode 'query={source="sudo"} |= "COMMAND=/usr/bin/id"' --data-urlencode since=1h
    [ "$status" -eq 0 ]
    [[ "$output" == *'"probador : PWD=/home/probador ; USER=root ; COMMAND=/usr/bin/id"'* ]]
}

@test "the counters' labels are the closed set" {
    run python3 - "$TMP/metrics.txt" <<'PY'
import re, sys
allowed = {"kind", "method", "jail", "action", "key", "who", "source"}
for line in open(sys.argv[1]):
    if line.startswith("#") or not line.strip():
        continue
    for name in re.findall(r'(\w+)="', line):
        if name not in allowed:
            sys.exit(f"label {name}: {line}")
PY
    [ "$status" -eq 0 ]
}

@test "the agent reports what its output sent" {
    run curl -fs "$AGENT/api/v2/metrics/prometheus"
    [ "$status" -eq 0 ]
    [[ "$output" == *fluentbit_output_proc_records_total* ]]
    [[ "$output" == *fluentbit_output_dropped_records_total* ]]
    [[ "$output" == *fluentbit_output_retries_failed_total* ]]
}
