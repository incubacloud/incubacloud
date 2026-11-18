#!/usr/bin/env bats
# The security lines of F0 (Debian 13, OpenSSH 10), and sudo-rs's (Ubuntu
# 26.04), through the real Fluent Bit image, with the configuration host_observability.yml renders, into a
# Loki of the central's version.
#
# Needs Docker and pulls two images, so it runs only when asked:
#   IC_FLUENT_BIT_TEST=1 bats incubacloud/tests/shell/fluent_bit_lines.bats
#
# The journal input is the one thing replaced: a JSON file with the
# journal's fields stands in for it (the real one is tried on a server).
# Everything else is as rendered, run as the compose file runs it: root
# without capabilities but reading others' files, a read-only filesystem
# and 128 MB. The proxy's access log is rendered for four instances, one
# behind a layer's router, with a cap of 16 failed web logins a minute.

bats_require_minimum_version 1.5.0

FB_IMAGE=fluent/fluent-bit:5.1.5
LOKI_IMAGE=grafana/loki:2.9.8

setup_file() {
    [ "${IC_FLUENT_BIT_TEST:-}" = 1 ] || skip "IC_FLUENT_BIT_TEST=1 runs it (Docker)"
    TMP="$(mktemp -d)"
    export TMP
    export NAME="ic-fbtest-$$"
    local here="${BATS_TEST_DIRNAME}"
    mkdir -p "$TMP/etc" "$TMP/log/audit" "$TMP/log/traefik"
    python3 "$here/../_agents_playbook.py" "$TMP/etc" '{
        "ic_weblogin_per_minute": 16,
        "ic_instances": [
            {"instance_id": "42", "traefik_prefix": "acme-19-0-", "traefik_extra": []},
            {"instance_id": "43", "traefik_prefix": "acme-19-0-x-19-0-", "traefik_extra": []},
            {"instance_id": "44", "traefik_prefix": "my.shop-19-0-", "traefik_extra": []},
            {"instance_id": "45", "traefik_prefix": "", "traefik_extra": ["shop-svc@file"]}
        ]}'
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
    : > "$TMP/log/traefik/access.log"
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
    cat "$here/fixtures/fluent_bit/access.log" >> "$TMP/log/traefik/access.log"
    for _ in $(seq 60); do
        [ "$(loki_count auditd)" = 5 ] && [ "$(loki_count fail2ban)" = 7 ] &&
            [ "$(loki_count sshd)" = 11 ] && [ "$(loki_count sudo)" = 8 ] &&
            [ "$(loki_count weblogin)" -ge 16 ] && break
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

@test "one failed SSH connection counts once, however it ends" {
    # Closed, reset or said goodbye by the client, or ended by sshd after
    # too many attempts; a connection that never named a user is not one.
    [ "$(counter 'ic_ssh_auth_failed_total')" = 7 ]
}

@test "a sign-in with the panel's key is the panel's" {
    [ "$(counter 'ic_ssh_accepted_total{kind="panel",method="publickey"}')" = 1 ]
    [ "$(counter 'ic_ssh_accepted_total{kind="other",method="publickey"}')" = 1 ]
}

@test "a refused sudo is not a command run" {
    [ "$(counter 'ic_sudo_total{kind="panel_user"}')" = 1 ]
    [ "$(counter 'ic_sudo_total{kind="ansible"}')" = 1 ]
    # Another user's BECOME-SUCCESS is not Ansible's; sudo-rs's command
    # line is one too.
    [ "$(counter 'ic_sudo_total{kind="other"}')" = 3 ]
    # GNU sudo's refusal once, not its PAM line beside it; sudo-rs's by
    # its PAM line, the only one it writes.
    [ "$(counter 'ic_sudo_failed_total')" = 2 ]
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
    [ "$(counter 'ic_security_lines_total{source="sshd"}')" = 11 ]
    [ "$(counter 'ic_security_lines_total{source="sudo"}')" = 8 ]
    [ "$(counter 'ic_security_lines_total{source="fail2ban"}')" = 9 ]
    [ "$(counter 'ic_security_lines_total{source="auditd"}')" = 8 ]
}

@test "auditd sends its four keys' SYSCALL and their files' PATH only" {
    [ "$(loki_count auditd)" = 5 ]
}

@test "the login page's jail sends its bans only, other jails nothing" {
    [ "$(loki_count fail2ban)" = 7 ]
}

@test "Loki's streams carry log_type, host_id, host and source only" {
    run curl -fs "$LOKI/loki/api/v1/labels"
    [ "$status" -eq 0 ]
    labels=$(python3 -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin)["data"])))' <<<"$output")
    [ "$labels" = "host host_id log_type source" ]
    run curl -fs "$LOKI/loki/api/v1/label/host_id/values"
    [[ "$output" == *'"data":["7"]'* ]]
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
allowed = {"kind", "method", "jail", "action", "key", "who", "source", "instance_id"}
for line in open(sys.argv[1]):
    if line.startswith("#") or not line.strip():
        continue
    for name, value in re.findall(r'(\w+)="([^"]*)"', line):
        if name not in allowed:
            sys.exit(f"label {name}: {line}")
        if name == "instance_id" and not value.isdigit():
            sys.exit(f"instance_id {value}: {line}")
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

# weblogin_lines: the failed web logins Loki holds, one per line.
weblogin_lines() {
    curl -fsG "$LOKI/loki/api/v1/query_range" \
        --data-urlencode 'query={log_type="security",source="weblogin"}' \
        --data-urlencode "limit=1000" --data-urlencode "since=1h" |
        python3 -c 'import json,sys; [print(v[1]) for s in json.load(sys.stdin)["data"]["result"] for v in s["values"]]'
}

@test "a failed web login leaves as a line of its own with its instance" {
    run weblogin_lines
    [ "$status" -eq 0 ]
    [[ "$output" == *"weblogin failed ip=203.0.113.5 instance=42 t=2026-11-14T10:00:01Z"* ]]
    # /web/login/totp, and a query after /web/login, count as well.
    [ "$(grep -c '^weblogin failed ip=203.0.113.5 instance=42 ' <<<"$output")" = 3 ]
    [[ "$output" == *"weblogin failed ip=2001:db8::7 instance=42 "* ]]
}

@test "a page view, a sign-in that worked, the proxy's limit and a reset neither leave nor count" {
    run weblogin_lines
    ! grep -q '203\.0\.113\.6' <<<"$output"
    [ "$(counter 'ic_security_lines_total{source="weblogin"}')" = 24 ]
}

@test "the instance: none of this server's, the longest prefix, https-, a dot taken as itself" {
    run weblogin_lines
    [[ "$output" == *"ip=203.0.113.8 instance=0 "* ]]
    [[ "$output" == *"ip=203.0.113.9 instance=43 "* ]]
    [[ "$output" == *"ip=203.0.113.10 instance=44 "* ]]
    [[ "$output" == *"ip=203.0.113.11 instance=0 "* ]]
}

@test "a layer's router: its service while awake, none while asleep" {
    run weblogin_lines
    [[ "$output" == *"ip=203.0.113.12 instance=45 "* ]]
    [[ "$output" == *"ip=203.0.113.13 instance=0 "* ]]
}

@test "text from the client posing as fields names neither the address nor the router" {
    run weblogin_lines
    [[ "$output" == *"ip=198.51.100.7 instance=42 "* ]]
    [[ "$output" == *"ip=198.51.100.8 instance=0 "* ]]
    ! grep -q '9\.9\.9\.9' <<<"$output"
    # A ClientHost that is no address is dropped.
    ! grep -q 'ip=evil' <<<"$output"
}

@test "the line sent carries no request, host asked for or proxy JSON" {
    run weblogin_lines
    [ "$status" -eq 0 ]
    ! grep -q 'RequestHost\|RequestPath\|erp\.example\.com\|/web/login\|{' <<<"$output"
    ! grep -vqE '^weblogin failed ip=[0-9a-f.:]+ instance=[0-9]+ t=[0-9TZ:.+-]+$' <<<"$output"
}

@test "each instance's failed web logins are counted, those over the minute's cap too" {
    [ "$(counter 'ic_weblogin_failed_total{instance_id="42"}')" = 17 ]
    [ "$(counter 'ic_weblogin_failed_total{instance_id="0"}')" = 4 ]
    [ "$(counter 'ic_weblogin_failed_total{instance_id="43"}')" = 1 ]
    [ "$(counter 'ic_weblogin_failed_total{instance_id="44"}')" = 1 ]
    [ "$(counter 'ic_weblogin_failed_total{instance_id="45"}')" = 1 ]
    # 24 failed, at most 16 a minute sent (both halves of a minute that
    # turned in the middle would let more through: rare, and said here).
    # The lines of one address in the log carry different seconds on
    # purpose: Loki keeps one of two identical lines in a stream with the
    # same timestamp, and Fluent Bit stamps a burst read together within
    # the same few hundred nanoseconds, so identical ones lost a line now
    # and then.
    sent=$(weblogin_lines | grep -c .)
    [ "$sent" -ge 16 ]
    [ "$sent" -lt 24 ]
}

@test "the login lines agree with the web-login jail" {
    # The jail's own filter, from the hardening playbook, over the same
    # lines: the same failures and the same addresses as the Lua's.
    run python3 - "${BATS_TEST_DIRNAME}/../../ansible/playbooks/host_hardening.yml" \
        "${BATS_TEST_DIRNAME}/fixtures/fluent_bit/access.log" <<'PY'
import re, sys, yaml
tasks = yaml.safe_load(open(sys.argv[1]))[0]["tasks"]
content = next(t for t in tasks if t.get("name") == "Write the fail2ban web-login filter")
failregex = re.search(r"^failregex = (.+)$", content["ansible.builtin.copy"]["content"], re.M)[1]
regex = re.compile(failregex.replace("<HOST>", r"(?P<host>[0-9A-Fa-f.:]+)"))
hosts = [m["host"] for m in map(regex.search, open(sys.argv[2])) if m]
print(len(hosts), " ".join(sorted(set(hosts))))
PY
    [ "$status" -eq 0 ]
    jail_count=${output%% *}
    jail_ips=${output#* }
    lua_ips=$(weblogin_lines | sed -E 's/^weblogin failed ip=([^ ]+) .*/\1/' | LC_ALL=C sort -u | paste -sd' ' -)
    total=0
    for id in 0 42 43 44 45; do
        total=$((total + $(counter "ic_weblogin_failed_total{instance_id=\"$id\"}")))
    done
    [ "$jail_count" = "$total" ]
    [ "$jail_ips" = "$lua_ips" ]
}
