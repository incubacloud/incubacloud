#!/usr/bin/env bash
# Send one test mail through an instance's relay and say how it ended.
#
# Usage: relay_test.sh <instance_dir> <from> <to> <tag> <oauth> [wait_seconds]
#
#   <from>        the sender: the relay's mailbox, or its default sender
#   <to>          the one recipient: the person who asked for the test
#   <tag>         letters, digits and dashes; names the mail
#                 (Message-Id and X-IC-Relay-Test)
#   <oauth>       1 when the relay signs in with OAuth2, else 0
#   wait_seconds  how long to wait for the relay's verdict (120)
#
# The mail goes in on the relay's port 25 from inside its container
# (swaks, which docker-mailserver ships; 127.0.0.1 is in mynetworks, so
# it is taken as Odoo's mail is). docker-mailserver passes it through
# Amavis, which queues it again under another ID, so the relay's log is
# followed by the mail's own Message-Id rather than by the ID swaks was
# given: every ``cleanup`` line of the mail names it.
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
# take the mail.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 5 "$#" \
    "relay_test.sh <instance_dir> <from> <to> <tag> <oauth> [wait_seconds]"

dir="$(ic_expand_home "$1")"
from="$2"
to="$3"
tag="$4"
oauth="$5"
wait_seconds="${6:-120}"
# Polled this often while waiting for the verdict, in seconds.
poll="${IC_RELAY_TEST_POLL:-3}"

[[ "$tag" =~ ^[A-Za-z0-9-]+$ ]] || ic_die "invalid tag: $tag"
[[ "$wait_seconds" =~ ^[0-9]+$ ]] || ic_die "invalid wait: $wait_seconds"
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

smtp swaks --server 127.0.0.1:25 --from "$from" --to "$to" \
    --header "Subject: IncubaCloud relay test" \
    --header "Message-Id: $msgid" \
    --header "X-IC-Relay-Test: $tag" \
    --body "A test mail sent through this instance's mail relay from the panel." \
    >/dev/null 2>&1 || ic_die "the relay did not take the test mail on its port 25"

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
