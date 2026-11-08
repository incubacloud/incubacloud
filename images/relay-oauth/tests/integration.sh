#!/usr/bin/env bash
# Integration test of the relay-oauth image: Postfix + sasl-xoauth2 + the renewer
# against a fake Microsoft 365 (client credentials token endpoint, and SMTP with
# STARTTLS that demands XOAUTH2 and answers like Exchange Online).
#
# Usage: integration.sh IMAGE
# The relay runs with the environment doodba-copier-template v10.0.8 gives the
# smtpreal service. Prints one PASS/FAIL line per check and exits 1 on any FAIL.
# Removes its containers, network and temporary files on exit, not the image.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${1:?usage: integration.sh IMAGE}"
SUFFIX="$$"
NET="relay-oauth-it-$SUFFIX"
FAKE="relay-oauth-it-fake-$SUFFIX"
RELAY="relay-oauth-it-relay-$SUFFIX"
PROBE="relay-oauth-it-probe-$SUFFIX"
TENANT=11111111-2222-3333-4444-555555555555
CLIENT_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
SECRET=it-secret-value
MAILBOX=sender@example.test
TOKEN=/var/lib/ic-relay-oauth/token.json
# The Docker daemon must see the bind-mounted credentials file: a session's
# private /tmp may not be visible to it, so the runner's temp or the user's cache.
WORK="$(mktemp -d "${RUNNER_TEMP:-${XDG_CACHE_HOME:-$HOME/.cache}}/relay-oauth-it.XXXXXX")"
PASS=0
FAIL=0

check() { # check "description" command...
  local desc="$1"
  shift
  if "$@"; then
    PASS=$((PASS + 1))
    echo "PASS  $desc"
  else
    FAIL=$((FAIL + 1))
    echo "FAIL  $desc"
  fi
}
cleanup() {
  docker rm -f "$FAKE" "$RELAY" "$PROBE" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

rx() { docker exec "$RELAY" "$@"; }
ctl() { docker exec "$FAKE" curl -fsS "http://127.0.0.1:8080/control?$1" >/dev/null; }
fake_state() { # fake_state PYTHON-EXPRESSION-ON-d
  docker exec "$FAKE" curl -fsS http://127.0.0.1:8080/state \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"
}

# send_mail SUBJECT: prints the queue ID that goes to the relay. docker-mailserver
# passes mail through Amavis (127.0.0.1:10024), which queues it again under another
# ID: the "queued as" chain is followed from swaks's ID to the relay's.
send_mail() {
  local id1 id2 i
  id1=$(rx swaks --server 127.0.0.1:25 --from "$MAILBOX" --to dest@example.org \
    --header "Subject: $1" 2>&1 | sed -n 's/.*queued as \([0-9A-Za-z]*\).*/\1/p' | head -1)
  for ((i = 0; i < 30; i++)); do
    id2=$(rx grep -E "$id1: to=.*:10024,.*status=sent" /var/log/mail/mail.log 2>/dev/null \
      | sed -n 's/.*queued as \([0-9A-Za-z]*\)).*/\1/p' | tail -1)
    if [[ -n "$id2" ]]; then
      echo "$id2"
      return
    fi
    sleep 1
  done
  echo "${id1:-none}"
}
wait_status() { # wait_status ID sent|deferred [seconds]: that line in mail.log
  local i
  for ((i = 0; i < ${3:-60}; i++)); do
    rx grep -qE "$1: to=.*relay=fake-m365.*status=$2|$1: to=.*status=$2.*fake-m365" \
      /var/log/mail/mail.log 2>/dev/null && return 0
    sleep 1
  done
  return 1
}
in_queue() { # a message can be between queues right after "deferred": retry 5 s
  local i
  for ((i = 0; i < 5; i++)); do
    rx postqueue -j | grep -q "\"queue_id\": *\"$1\"" && return 0
    sleep 1
  done
  return 1
}
queue_empty() { [[ -z "$(rx postqueue -j)" ]]; }
wait_flushed() {
  local i
  for ((i = 0; i < ${1:-60}; i++)); do
    queue_empty && return 0
    sleep 1
  done
  return 1
}
renewer_field() {
  rx ic-relay-oauth status | python3 -c "import json,sys; print(json.load(sys.stdin).get('$1'))"
}
wait_renewer() { # wait_renewer STATE [seconds]
  local i
  for ((i = 0; i < ${2:-60}; i++)); do
    [[ "$(renewer_field state 2>/dev/null)" == "$1" ]] && return 0
    sleep 1
  done
  return 1
}
put_token() { # put_token JSON: as the renewer would, root:postfix 0640, by rename
  docker exec -i "$RELAY" sh -c "cat > $TOKEN.it && chown root:postfix $TOKEN.it \
    && chmod 0640 $TOKEN.it && mv $TOKEN.it $TOKEN" <<<"$1"
}
plugin_said() { rx grep -qE "\\[sasl-xoauth2\\] auth failed: .*$1" /var/log/mail/mail.log; }
server_said() {
  rx grep -qE "$1: to=.*status=deferred \\(SASL authentication failed; server fake-m365.* said: $2" \
    /var/log/mail/mail.log
}
current_token() { rx python3 -c "import json; print(json.load(open('$TOKEN'))['access_token'])"; }
token_keys() { rx python3 -c "import json; print(','.join(sorted(json.load(open('$TOKEN')))))"; }
token_requests() { fake_state 'd["token_requests"]'; }

cleanup
mkdir -p "$WORK" && chmod 0700 "$WORK"
docker network create "$NET" >/dev/null

# --- The fake Microsoft 365, run from the image under test ---------------------------
docker run -d --name "$FAKE" --network "$NET" --network-alias fake-m365 \
  -v "$HERE/fake_m365.py:/fake_m365.py:ro" --entrypoint bash "$IMAGE" -c \
  'openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=fake-m365 \
     -keyout /tmp/fake.key -out /tmp/fake.crt 2>/dev/null \
   && exec python3 -I /fake_m365.py' >/dev/null
for _ in $(seq 30); do
  docker logs "$FAKE" 2>&1 | grep -q "fake-m365 ready" && break
  sleep 1
done

# --- The relay ------------------------------------------------------------------------
cat >"$WORK/ic-relay-oauth.json" <<EOF
{"provider": "microsoft", "tenant_id": "$TENANT", "client_id": "$CLIENT_ID",
 "client_secret": "$SECRET", "mailbox": "$MAILBOX"}
EOF
chmod 0600 "$WORK/ic-relay-oauth.json"
T0=$(date +%s)
docker run -d --name "$RELAY" --network "$NET" --hostname smtp.example.test \
  -e DEFAULT_RELAY_HOST="[fake-m365]:587" -e RELAY_HOST=fake-m365 -e RELAY_PORT=587 \
  -e RELAY_USER="$MAILBOX" -e RELAY_PASSWORD="$TOKEN" \
  -e LOG_LEVEL=info -e ENABLE_SRS=1 -e ONE_DIR=1 -e PERMIT_DOCKER=connected-networks \
  -e POSTFIX_INET_PROTOCOLS=ipv4 -e SMTP_ONLY=1 -e SRS_DOMAINNAME=example.test \
  -e SRS_EXCLUDE_DOMAINS=example.test -e SRS_SENDER_CLASSES=envelope_sender,header_sender \
  -e ENABLE_UPDATE_CHECK=0 \
  -e IC_RELAY_OAUTH_TOKEN_URL="http://fake-m365:8080/$TENANT/oauth2/v2.0/token" \
  -v "$WORK/ic-relay-oauth.json:/run/secrets/ic-relay-oauth.json:ro" \
  "$IMAGE" >/dev/null
for _ in $(seq 120); do
  rx supervisorctl status postfix 2>/dev/null | grep -q RUNNING && break
  sleep 1
done
echo "start until Postfix RUNNING: $(($(date +%s) - T0)) s"

# --- 1. What the image sets up ----------------------------------------------------------
check "saslpluginviewer lists XOAUTH2 with max SSF 60" \
  sh -c "docker exec $RELAY saslpluginviewer -c 2>&1 | grep -A1 'mechanism: XOAUTH2' | grep -q 'best SSF: 60'"
check "smtp_sasl_mechanism_filter = xoauth2 survives the start" \
  sh -c "docker exec $RELAY postconf -h smtp_sasl_mechanism_filter | grep -qx xoauth2"
check "SASL soft bounce is on (a failed sign-in defers, never bounces)" \
  sh -c "docker exec $RELAY postconf -h smtp_sasl_auth_soft_bounce | grep -qx yes"
check "sasl_passwd holds the token file's path" \
  rx grep -qx "\[fake-m365\]:587 *$MAILBOX:$TOKEN" /etc/postfix/sasl_passwd
check "the plugin never logs a full trace (it would hold the token)" \
  sh -c "docker exec $RELAY python3 -c \"import json; print(json.load(open('/etc/sasl-xoauth2.conf'))['log_full_trace_on_failure'])\" | grep -qx no"
check "the renewer is ok" wait_renewer ok 60
check "token.json root:postfix 0640 in a root:postfix 0750 directory" \
  sh -c "[ \"\$(docker exec $RELAY stat -c '%U:%G %a' $TOKEN /var/lib/ic-relay-oauth | tr '\n' ' ')\" = 'root:postfix 640 root:postfix 750 ' ]"
check "the token file has exactly the four keys" \
  [ "$(token_keys)" = "access_token,expiry,refresh_token,user" ]

# --- 2. Sending ---------------------------------------------------------------------------
ID=$(send_mail case-sent)
check "mail with a valid token: sent ($ID)" wait_status "$ID" sent 60
check "the fake SMTP saw AUTH XOAUTH2 and nothing else" \
  [ "$(fake_state 'sorted(set(d["auth_mechs"]))')" = "['XOAUTH2']" ]

# --- 3. The token file's contract, with the renewer stopped ---------------------------------
rx supervisorctl stop ic-relay-oauth >/dev/null
TOK=$(current_token)
EXP=$(($(date +%s) + 3000))
put_token "{\"refresh_token\": \"unused\", \"access_token\": \"$TOK\", \"expiry\": $EXP, \"user\": \"$MAILBOX\"}"
ID=$(send_mail case-expiry-number)
check "expiry as a JSON number: sent ($ID)" wait_status "$ID" sent 60
put_token "{\"refresh_token\": \"unused\", \"access_token\": \"$TOK\", \"expiry\": \"$EXP\", \"user\": \"wrong@example.test\"}"
ID=$(send_mail case-user-override)
check "the file's user overrides RELAY_USER (wrong@ -> deferred) ($ID)" wait_status "$ID" deferred 60
check "  ... the server answered 535 5.7.3" server_said "$ID" "535 5.7.3"
put_token "this is not JSON"
ID=$(send_mail case-corrupt)
check "corrupt token file: deferred ($ID)" wait_status "$ID" deferred 60
check "  ... the plugin says so (Syntax error)" plugin_said "TokenStore::Read: exception=.*Syntax error"
put_token "{\"access_token\": \"$TOK\", \"expiry\": \"$EXP\", \"user\": \"$MAILBOX\"}"
ID=$(send_mail case-no-refresh-token)
check "no refresh_token: deferred ($ID)" wait_status "$ID" deferred 60
check "  ... the plugin says missing refresh_token" plugin_said "missing refresh_token"
check "  ... and it stays queued" in_queue "$ID"
rx rm -f "$TOKEN"
ID=$(send_mail case-file-missing)
check "no token file: deferred ($ID)" wait_status "$ID" deferred 60
check "  ... the plugin says failed to open file" plugin_said "failed to open file"
check "  ... and it stays queued" in_queue "$ID"
BEFORE=$(token_requests)
put_token "{\"refresh_token\": \"unused\", \"access_token\": \"$TOK\", \"expiry\": \"$(($(date +%s) - 100))\", \"user\": \"$MAILBOX\"}"
ID=$(send_mail case-expired)
check "expired token, renewer stopped: deferred ($ID)" wait_status "$ID" deferred 60
check "  ... the plugin tried its refresh against the closed local port" plugin_said "TokenStore::Refresh: http error"
check "  ... and it stays queued" in_queue "$ID"
check "  ... the plugin asked the token endpoint nothing" [ "$BEFORE" = "$(token_requests)" ]

# --- 4. Recovery: the renewer comes back and flushes the queue -------------------------------
rx supervisorctl start ic-relay-oauth >/dev/null
check "the renewer is ok again" wait_renewer ok 30
check "its postqueue -f delivers every deferred message (queue empty)" wait_flushed 60
check "  ... the fake SMTP holds the 2 sent and the 5 deferred" [ "$(fake_state 'len(d["messages"])')" -eq 7 ]

# --- 5. Good token, Exchange rejects (535 5.7.3) ------------------------------------------------
ctl smtp_mode=reject
ID=$(send_mail case-535)
check "535 5.7.3 with a good token: deferred, not bounced ($ID)" wait_status "$ID" deferred 60
check "  ... and it stays queued" in_queue "$ID"
check "  ... the renewer still says ok" [ "$(renewer_field state)" = ok ]
check "  ... postqueue -j's delay_reason names the sign-in" \
  sh -c "docker exec $RELAY postqueue -j | grep -q 'SASL authentication failed'"
ctl smtp_mode=normal
rx postqueue -f
check "  ... fixed on the server + postqueue -f: sent" wait_status "$ID" sent 60

# --- 6. The renewer in error keeps the token that still works -------------------------------------
ctl token_mode=invalid_secret
rx supervisorctl signal USR1 ic-relay-oauth >/dev/null
check "AADSTS7000215: state error" wait_renewer error 20
check "  ... error_code AADSTS7000215" [ "$(renewer_field error_code)" = AADSTS7000215 ]
check "  ... expires_at still that of the working token" \
  sh -c "[ \"\$(docker exec $RELAY ic-relay-oauth status | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"expires_at\"])')\" -gt $(date +%s) ]"
ID=$(send_mail case-renewer-error-old-token)
check "  ... the previous token still sends: sent ($ID)" wait_status "$ID" sent 60
ctl token_mode=unavailable
rx supervisorctl signal USR1 ic-relay-oauth >/dev/null
sleep 2
check "503 from the token endpoint: error_code HTTP 503" [ "$(renewer_field error_code)" = "HTTP 503" ]
ctl token_mode=ok
rx supervisorctl signal USR1 ic-relay-oauth >/dev/null
check "  ... back to ok" wait_renewer ok 20

# --- 7. Credentials file problems and the test-only URL ------------------------------------------
rx sh -c 'IC_RELAY_OAUTH_SECRETS=/nonexistent IC_RELAY_OAUTH_DIR=/tmp/icx timeout 3 ic-relay-oauth run >/dev/null 2>&1; IC_RELAY_OAUTH_DIR=/tmp/icx ic-relay-oauth status' >"$WORK/status-missing.json"
check "no credentials file: config_missing, waiting instead of exiting" \
  grep -q '"error_code": "config_missing"' "$WORK/status-missing.json"
# A bind-mounted file missing on the host: Docker creates a directory in its place.
docker run --rm --name "$PROBE" --entrypoint sh \
  -v "$WORK/never-written.json:/run/secrets/ic-relay-oauth.json:ro" "$IMAGE" \
  -c 'timeout 3 ic-relay-oauth run >/dev/null 2>&1; ic-relay-oauth status' >"$WORK/status-dir.json" 2>/dev/null
check "the credentials mount came up as a directory: config_is_directory" \
  grep -q '"error_code": "config_is_directory"' "$WORK/status-dir.json"
URL=$(rx env IC_RELAY_OAUTH_TOKEN_URL=http://93.184.216.34/x python3 -I -c "
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader('r', '/usr/local/bin/ic-relay-oauth')
spec = importlib.util.spec_from_loader('r', loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
print(mod.token_url({'tenant_id': 'contoso.onmicrosoft.com'}))" 2>/dev/null | tail -1)
check "IC_RELAY_OAUTH_TOKEN_URL on a public address is ignored" \
  [ "$URL" = "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token" ]

# --- 8. Neither the secret nor any token in the logs or docker inspect ------------------------------
fake_state '"\n".join(d["tokens"])' >"$WORK/needles"
echo "$SECRET" >>"$WORK/needles"
{
  rx sh -c 'cat /var/log/mail/* /var/log/supervisor/* 2>/dev/null'
  docker logs "$RELAY" 2>&1
  docker inspect "$RELAY"
} >"$WORK/haystack"
check "no token nor the secret in logs or docker inspect ($(wc -l <"$WORK/needles") strings)" \
  sh -c "! grep -F -f '$WORK/needles' '$WORK/haystack' >/dev/null"

echo "token requests: $(token_requests)  sign-ins ok/failed: $(fake_state 'd["auth_ok"]')/$(fake_state 'd["auth_fail"]')"
echo "RESULT: $PASS PASS, $FAIL FAIL"
if [[ $FAIL -ne 0 ]]; then
  echo "--- renewer log"
  rx tail -n 40 /var/log/supervisor/ic-relay-oauth.log 2>&1
  echo "--- mail.log (last 60 lines)"
  rx tail -n 60 /var/log/mail/mail.log 2>&1
  exit 1
fi
