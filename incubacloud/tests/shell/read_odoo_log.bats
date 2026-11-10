#!/usr/bin/env bats
# Tests for scripts/read_odoo_log.sh.
#
# What crosses SSH is a projection, one record per line, and the panel's
# parser (``cloud_instance_slow_routes.parse_slow_routes``) is written
# against it: these pin its shape. The window is by Odoo's own stamp,
# the trace of a killed worker follows it, and a planted link in place of
# the live file is not followed. ``docker`` is stubbed for the fallback
# of an instance that still logs to its container.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/read_odoo_log.sh"
    TMP="$(mktemp -d)"
    DIR="$TMP/inst"
    mkdir -p "$DIR/logs" "$TMP/bin"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$DOCKER_CALLS"
echo "2026-11-10 09:50:00,000 12 INFO prod werkzeug: 203.0.113.9 - - [x] \"GET /from-container HTTP/1.1\" 200 - 1 0.001 0.002"
STUB
    chmod +x "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
    export DOCKER_CALLS="$TMP/calls"
    : > "$DOCKER_CALLS"
}

teardown() {
    rm -rf "$TMP"
}

# Lines as Odoo 19 writes them (``odoo/netsvc.py``'s format, its timing
# appended; ``werkzeug`` for requests, ``ir_cron`` for scheduled actions,
# ``odoo.service.server`` for the workers' limits).
write_log() {
    cat > "$DIR/logs/odoo.log" <<'LOG'
2026-11-10 09:00:00,100 12 INFO prod werkzeug: 203.0.113.9 - - [10/Nov/2026 09:00:00] "GET /before HTTP/1.1" 200 - 3 0.010 0.020
2026-11-10 09:40:01,123 12 INFO prod werkzeug: 203.0.113.9 - - [10/Nov/2026 09:40:01] "POST /web/dataset/call_kw/stock.quant/action_apply#stock.quant.action_apply HTTP/1.1" 200 - 152 0.523 3.870
2026-11-10 09:40:02,000 13 INFO prod odoo.addons.base.models.ir_cron: Job 'Inventory: value' (7) starting - - -
2026-11-10 09:45:12,000 13 INFO prod odoo.addons.base.models.ir_cron: Job 'Inventory: value' (7) fully done (#loop 1; done 3; remaining 0; duration 310.00s) 12 0.040 309.900
2026-11-10 09:46:00,000 55 INFO prod odoo.service.server: Worker (55) virtual memory limit (2147483648) reached. - - -
2026-11-10 09:47:00,000 1 ERROR ? odoo.service.server: WorkerHTTP (56) timeout after 1200s - - -
2026-11-10 09:47:00,050 57 INFO ? odoo.service.server: Worker WorkerHTTP (57) alive - - -
2026-11-10 09:48:00,000 12 INFO prod odoo.modules.registry: something else - - -
LOG
}

# Threaded mode: the thread over its limit is reported every second, then
# its stack is dumped by ``dumpstacks`` on a line of its own.
write_threaded_log() {
    cat > "$DIR/logs/odoo.log" <<'LOG'
2026-11-10 09:50:00,000 1 WARNING ? odoo.service.server: Thread <Thread(odoo.service.http.request.140245, started 140245)> virtual real time limit (130/120s) reached. - - -
2026-11-10 09:50:59,000 1 INFO ? odoo.service.server: Dumping stacktrace of limit exceeding threads before reloading - - -
2026-11-10 09:50:59,001 1 INFO ? odoo.tools.misc: 
# Thread: <Thread(odoo.service.http.request.140245, started 140245)> (db:prod) (uid:2) (url:http://prod.example.com/web/dataset/call_kw/stock.quant/action_apply) (qc:5210 qt:12.004 pt:118.310)
File: "/usr/lib/python3.12/threading.py", line 1032, in _bootstrap
  self._bootstrap_inner()
File: "/usr/lib/python3.12/threading.py", line 1075, in _bootstrap_inner
  self.run()
File: "/opt/odoo/odoo/service/server.py", line 210, in process_request_thread
  self.finish_request(request, client_address)
File: "/opt/odoo/odoo/http.py", line 2101, in __call__
  response = request._serve_db()
File: "/opt/odoo/custom/src/private/stock_x/models/stock_quant.py", line 88, in action_apply
  for quant in self:
File: "/opt/odoo/custom/src/private/stock_x/models/stock_quant.py", line 91, in action_apply
  quant._recompute_value() - - -
2026-11-10 09:51:00,000 1 INFO ? odoo.service.server: Initiating server reload - - -
LOG
}

@test "each kind of line is projected, the window by Odoo's stamp" {
    write_log
    run bash "$SCRIPT" "$DIR" "2026-11-10 09:30:00" 15
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 5 ]
    [ "${lines[0]}" = "$(printf 'R\t2026-11-10 09:40:01\tPOST /web/dataset/call_kw/stock.quant/action_apply#stock.quant.action_apply HTTP/1.1\t200\t152\t0.523\t3.870')" ]
    [ "${lines[1]}" = "$(printf "C\t2026-11-10 09:40:02\t13\tJob 'Inventory: value' (7) starting")" ]
    [ "${lines[2]}" = "$(printf "C\t2026-11-10 09:45:12\t13\tJob 'Inventory: value' (7) fully done (#loop 1; done 3; remaining 0; duration 310.00s)")" ]
    [ "${lines[3]}" = "$(printf 'M\t2026-11-10 09:46:00')" ]
    # With workers, the master kills the worker and logs no trace of it.
    [ "${lines[4]}" = "$(printf 'K\t2026-11-10 09:47:00\tWorkerHTTP (56) timeout after 1200s')" ]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "without workers, the stack dumped after the limit is kept: its header and innermost frames" {
    write_threaded_log
    run bash "$SCRIPT" "$DIR" "2026-11-10 09:30:00" 15
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 11 ]
    [ "${lines[0]}" = "$(printf 'K\t2026-11-10 09:50:59\tDumping stacktrace of limit exceeding threads before reloading')" ]
    [[ "${lines[1]}" == "$(printf '+\t# Thread: ')"*"(url:http://prod.example.com/web/dataset/call_kw/stock.quant/action_apply) (qc:5210 qt:12.004 pt:118.310)" ]]
    [ "${lines[2]}" = "$(printf '+\t  self.run()')" ]
    [ "${lines[10]}" = "$(printf '+\t  quant._recompute_value()')" ]
    [[ "$output" != *"_bootstrap_inner()"* ]]
    [[ "$output" != *"Initiating server reload"* ]]
}

@test "a stack dumped before the window is left out with its line" {
    write_threaded_log
    run bash "$SCRIPT" "$DIR" "2026-11-10 09:51:00" 15
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "yesterday's archive is read too, plain or compressed" {
    : > "$DIR/logs/odoo.log"
    printf '%s\n' '2026-11-09 23:59:30,000 12 INFO prod werkzeug: 203.0.113.9 - - [x] "GET /archived HTTP/1.1" 200 - 1 0.001 0.002' \
        | gzip > "$DIR/logs/odoo.log.2026-11-09.gz"
    run bash "$SCRIPT" "$DIR" "2026-11-09 23:59:00" 15
    [ "$status" -eq 0 ]
    [[ "${lines[0]}" == *"GET /archived"* ]]
}

@test "a link planted for the live file is not followed" {
    echo "2026-11-10 09:40:01,123 12 INFO prod werkzeug: x - - [x] \"GET /secret HTTP/1.1\" 200 - 1 0.1 0.1" > "$TMP/elsewhere"
    ln -s "$TMP/elsewhere" "$DIR/logs/odoo.log"
    run bash "$SCRIPT" "$DIR" "2026-11-10 09:00:00" 15
    [ "$status" -eq 0 ]
    [[ "$output" != *"/secret"* ]]
    # It falls back to the container's output instead.
    grep -q -- "--since 15m odoo" "$DOCKER_CALLS"
    [[ "$output" == *"/from-container"* ]]
}

@test "a window that is not a number of minutes is refused" {
    run bash "$SCRIPT" "$DIR" "2026-11-10 09:00:00" "15; rm -rf /"
    [ "$status" -eq 2 ]
}
