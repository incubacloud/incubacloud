#!/usr/bin/env bash
# Send one test mail through an instance's relay and say how it ended.
#
# Usage: relay_test.sh <instance_dir> <from> <to> <tag> <oauth> [wait_seconds] [connect_seconds]
#
#   <from>        the sender: the relay's mailbox, or its default sender
#   <to>          the one recipient: the person who asked for the test
#   <tag>         letters, digits and dashes; names the mail
#                 (Message-Id and X-IC-Relay-Test)
#   <oauth>       1 when the relay signs in with OAuth2, else 0
#   wait_seconds  how long to wait for the relay's verdict (120)
#   connect_seconds  how long to wait for the relay to take mail (90)
#
# The mail goes in on the relay's port 25 from inside its container
# (swaks, which docker-mailserver ships; 127.0.0.1 is in mynetworks, so
# it is taken as Odoo's mail is). docker-mailserver passes it through
# Amavis, which queues it again under another ID, so the relay's log is
# followed by the mail's own Message-Id rather than by the ID swaks was
# given: every ``cleanup`` line of the mail names it.
#
# Just after a rebuild the relay's container may not be accepting
# connections yet (the QA's route 6o saw the first test fail 7 s after the
# rebuild ended), and its content filter may start later than Postfix: a
# mail taken then is deferred with «connect to 127.0.0.1[127.0.0.1]:10024:
# Connection refused» and would leave only at Postfix's next retry. So
# before the mail goes in, the content filter Postfix hands it to
# (``postconf -h content_filter``; docker-mailserver's is
# ``smtp-amavis:[127.0.0.1]:10024``) must accept connections on the host
# and port it names; a relay without one skips that. While the container,
# the content filter or port 25 (connection refused, no banner) gives no
# answer, it is tried again every poll seconds for up to connect_seconds;
# then the script exits 75 saying the relay is still starting, and sends
# nothing. A relay that answers and refuses the mail is not tried again:
# that is a result of the relay, not of its start.
#
# The last line printed is always one of:
#
#   IC_RELAY_TEST {"status": "sent|deferred|bounced|timeout", "response": "<the server's answer>", "token": <ic-relay-oauth status, or null>}
#
# A test mail that was not sent is removed from the queue (postsuper -d),
# so it does not arrive hours later; so is one whose job was cancelled
# while it waited (SIGHUP or SIGTERM: the traps below). One that was sent
# flushes the queue (postqueue -f): mail deferred by what the person just
# fixed leaves now instead of at Postfix's next retry. Addresses are left
# out of the server's answer. The script fails (ic_die) only when the
# relay could not be asked: no instance directory, or the relay did not
# take the mail (exit 1; exit 75 when it was still starting: its port 25
# or its content filter never answered).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 5 "$#" \
    "relay_test.sh <instance_dir> <from> <to> <tag> <oauth> [wait_seconds] [connect_seconds]"

dir="$(ic_expand_home "$1")"
from="$2"
to="$3"
tag="$4"
oauth="$5"
wait_seconds="${6:-120}"
# How long to keep trying while the relay is not ready to take mail, in
# seconds. 90 s (12-nov-2026): a relay's smtp container, docker-mailserver,
# takes up to about half a minute after a rebuild to accept on port 25, and
# its Amavis starts later than Postfix (the QA's test mail right after one
# found port 25 open and port 10024 still closed); 90 s covers both starts
# with margin, and costs a person no more than a relay that is not coming
# up makes them wait. Kept in step with CONNECT_WAIT_SECONDS of
# models/relay_test_mail_executor.py, which sizes the step's timeout.
connect_wait="${7:-90}"
# How long one probe of the content filter's port may take, in seconds.
probe_seconds=5
# Polled this often while waiting for the verdict, in seconds.
poll="${IC_RELAY_TEST_POLL:-3}"

[[ "$tag" =~ ^[A-Za-z0-9-]+$ ]] || ic_die "invalid tag: $tag"
[[ "$wait_seconds" =~ ^[0-9]+$ ]] || ic_die "invalid wait: $wait_seconds"
[[ "$connect_wait" =~ ^[0-9]+$ ]] || ic_die "invalid connect wait: $connect_wait"
[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

smtp() {
    docker compose exec -T smtp "$@"
}

# json_text TEXT — TEXT as the inside of a JSON string, without the
# addresses it named.
json_text() {
    printf '%s' "$1" \
        | sed -E 's/[^][ <>()";:,'"'"']+@[^][ <>()";:,'"'"']+/<address>/g' \
        | tr -d '\000-\037' \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

token=null
if [ "$oauth" = 1 ]; then
    token="$(smtp ic-relay-oauth status 2>/dev/null | tail -n 1)" || token=""
    [[ "$token" == \{* ]] || token=null
fi

msgid="<ic-relay-test-${tag}@incubacloud.invalid>"

# The IDs the mail had in the relay's queue, joined with "|".
queue_ids() {
    smtp grep -aF "message-id=$msgid" /var/log/mail/mail.log 2>/dev/null \
        | sed -n 's/.* \([0-9A-Za-z]\{6,\}\): message-id=.*/\1/p' \
        | sort -u | paste -sd '|' -
}

# remove_mail [IDS] — remove the test mail from the queue under each of
# its IDs ("|"-joined); without IDS, under the ones the log names now.
remove_mail() {
    local id found="${1-$(queue_ids || true)}"
    for id in ${found//|/ }; do
        smtp postsuper -d "$id" >/dev/null 2>&1 || true
    done
}

# Cancelled while it waits: the mail the relay may still be trying to
# hand over must not leave later.
trap 'remove_mail; exit 129' HUP
trap 'remove_mail; exit 143' TERM

# no_answer RC — whether swaks's (or docker compose exec's) exit status RC
# says port 25 gave no answer: 1 and 125 are docker compose exec failing
# (the container is not up), 2 swaks not connecting, 21 no banner read and
# 35 a timeout. The other statuses (22 to 34) are the relay's answer
# refusing a step of the mail.
no_answer() {
    case "$1" in
        1 | 2 | 21 | 35 | 125) return 0 ;;
        *) return 1 ;;
    esac
}

# content_filter_target — "HOST PORT" of the content filter Postfix hands
# mail to before the relay host, when its content_filter names a
# [host]:port; nothing when it names none. Fails while the container does
# not answer.
content_filter_target() {
    local value
    value="$(smtp postconf -h content_filter 2>/dev/null)" || return 1
    printf '%s\n' "$value" \
        | sed -n 's/^[^:]*:\[\([^]]*\)\]:\([0-9][0-9]*\)[[:space:]]*$/\1 \2/p'
}

# port_answers HOST PORT — whether something accepts connections on
# HOST:PORT inside the relay's container.
port_answers() {
    # shellcheck disable=SC2016  # expanded by the container's bash
    smtp timeout "$probe_seconds" bash -c ': <>"/dev/tcp/$1/$2"' _ "$1" "$2" \
        >/dev/null 2>&1
}

# relay_ready — whether the relay can take the mail now: its container
# answers and its content filter, if it has one, accepts connections.
# Sets waiting_for to what it is still waiting for otherwise.
filter_read=0
filter=""
waiting_for=port
relay_ready() {
    local target
    if [ "$filter_read" = 0 ]; then
        target="$(content_filter_target)" || { waiting_for=port; return 1; }
        filter="$target"
        filter_read=1
    fi
    if [ -n "$filter" ]; then
        # shellcheck disable=SC2086  # "HOST PORT": two words on purpose
        port_answers $filter || { waiting_for=filter; return 1; }
    fi
    return 0
}

connect_deadline=$((SECONDS + connect_wait))
while :; do
    if relay_ready; then
        rc=0
        smtp swaks --server 127.0.0.1:25 --from "$from" --to "$to" \
            --header "Subject: IncubaCloud relay test" \
            --header "Message-Id: $msgid" \
            --header "X-IC-Relay-Test: $tag" \
            --body "A test mail sent through this instance's mail relay from the panel." \
            >/dev/null 2>&1 || rc=$?
        [ "$rc" -eq 0 ] && break
        no_answer "$rc" \
            || ic_die "the relay did not take the test mail on its port 25"
        waiting_for=port
    fi
    if [ "$SECONDS" -ge "$connect_deadline" ]; then
        if [ "$waiting_for" = filter ]; then
            reason="the relay is still starting: its content filter does not answer yet; try again in a minute"
        else
            reason="the relay is not accepting mail on its port 25 yet; try again in a minute"
        fi
        printf '[incubacloud] ERROR: %s\n' "$reason" >&2
        exit 75
    fi
    sleep "$poll"
done

line=""
ids=""
deadline=$((SECONDS + wait_seconds))
while :; do
    ids="$(queue_ids || true)"
    if [ -n "$ids" ]; then
        # The relay's own line: not Amavis's hand-off on port 10024.
        line="$(smtp grep -aE " ($ids): to=<.*, status=(sent|deferred|bounced) " \
                /var/log/mail/mail.log 2>/dev/null \
            | grep -avF "relay=127.0.0.1[127.0.0.1]:10024," | tail -n 1 || true)"
        [ -n "$line" ] && break
    fi
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep "$poll"
done

status=timeout
response=""
if [ -n "$line" ]; then
    status="$(printf '%s' "$line" | sed -n 's/.*, status=\([a-z]*\) .*/\1/p')"
    response="$(printf '%s' "$line" | sed -n 's/.*, status=[a-z]* (\(.*\))$/\1/p')"
fi

trap - HUP TERM
if [ "$status" = sent ]; then
    smtp postqueue -f >/dev/null 2>&1 || true
else
    remove_mail "$ids"
fi

printf 'IC_RELAY_TEST {"status": "%s", "response": "%s", "token": %s}\n' \
    "$status" "$(json_text "$response")" "$token"
