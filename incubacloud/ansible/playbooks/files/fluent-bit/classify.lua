-- Classify one security record for Fluent Bit (host_observability.yml).
--
-- Sets ``source``, ``host`` and ``host_id`` (Loki's labels), the ``c_*``
-- fields the log_to_metrics filters count, ``c_drop`` on what is counted
-- but not sent, and keeps only the line itself in ``message``; for the
-- proxy's access log, a line of its own (``weblogin``).
--
-- What this panel knows about itself comes from panel.lua, a data file
-- the playbook renders (``return {host = ..., host_id = ..., fps = {...},
-- user = ..., uid = ..., instances = {...}, weblogin_per_minute = ...}``),
-- so this file carries no template.
--
-- Every label value below is one of a closed set: a test of the rendered
-- configuration fails on any other. Lua patterns have no alternation,
-- hence the separate finds. The line shapes are those of Debian 13 with
-- OpenSSH 10 (F0 of the observability plan, 9-nov-2026).

local PANEL = dofile("/fluent-bit/etc/panel.lua")

-- One line per failed connection: sshd ends every connection that never
-- authenticated with one of these (the client closing, resetting or
-- saying goodbye first, or sshd ending it after too many attempts, as a
-- brute force with several passwords per connection gets). ``Invalid
-- user`` and ``Failed …`` come before them in the same connection, which
-- made 1.8 lines per attempt (F0, 9-nov-2026; review of 12-nov-2026).
local FAILED_ENDINGS = {
    "^Connection closed by invalid user ",
    "^Connection closed by authenticating user ",
    "^Connection reset by invalid user ",
    "^Connection reset by authenticating user ",
    "^Disconnected from invalid user ",
    "^Disconnected from authenticating user ",
    "^Disconnecting invalid user ",
    "^Disconnecting authenticating user ",
}

local function ssh(msg, out)
    if msg:find("%[preauth%]$") then
        for _, pattern in ipairs(FAILED_ENDINGS) do
            if msg:find(pattern) then
                out.c_failed = "1"
                break
            end
        end
    end
    local method = msg:match("^Accepted (%S+) for %S+ from %S+ port %d+ ssh2")
    if method then
        local fp = msg:match(" (SHA256:%S+)")
        out.c_acc_kind = (fp and PANEL.fps[fp]) and "panel" or "other"
        out.c_acc_method = (method == "publickey") and "publickey" or "password"
    end
end

-- Whether the journal record is sudo-rs's (Ubuntu 26.04's sudo, which
-- runs from /usr/lib/cargo/bin).
local function is_sudo_rs(exe)
    return exe ~= nil and (exe:find("/cargo/bin/sudo", 1, true) ~= nil
        or exe:find("sudo-rs", 1, true) ~= nil)
end

local function sudo(msg, out, exe)
    -- The line of a refused sudo also carries COMMAND=: it is a failure,
    -- not a command run.
    if msg:find("incorrect password attempt", 1, true) then
        out.c_sudo_failed = "1"
        return
    end
    -- sudo-rs writes no line of its own for a wrong password, only PAM's,
    -- one per refused sudo (Ubuntu 26.04, sudo-rs 0.2.13). GNU sudo writes
    -- PAM's too, beside the line above: counted for sudo-rs alone, or each
    -- refusal would count twice there.
    if is_sudo_rs(exe)
        and msg:find("^pam_unix%(sudo:auth%): authentication failure") then
        out.c_sudo_failed = "1"
        return
    end
    local user = msg:match("^%s*(%S+) : .*COMMAND=")
    if user then
        if user ~= PANEL.user then
            -- ``sudo echo BECOME-SUCCESS`` is anyone's to type: only the
            -- panel's user runs Ansible (review of 12-nov-2026).
            out.c_sudo_kind = "other"
        elseif msg:find("BECOME-SUCCESS", 1, true) then
            out.c_sudo_kind = "ansible"
        else
            out.c_sudo_kind = "panel_user"
        end
    end
end

local F2B = {Found = "found", Ban = "ban", Unban = "unban", ["Restore Ban"] = "ban"}

-- The jail of the instances' login page bans the addresses that fail an
-- Odoo password. Its ``Found`` lines are counted and not sent: they repeat
-- the ``weblogin`` lines below without the instance, which fail2ban's
-- line does not carry (plan #84, C1.9). Its bans leave the server.
local WEB_JAIL = "incubacloud-weblogin"
local WEB_SENT = {ban = true, unban = true}

local function fail2ban(msg, out)
    local jail, rest = msg:match("%[([%w_.-]+)%] (.*)$")
    if not jail then
        -- The daemon's own lines (start, stop, configuration).
        return
    end
    if jail ~= "sshd" and jail ~= WEB_JAIL then
        out.c_drop = "1"
        return
    end
    local action
    if rest:find("already banned", 1, true) then
        action = "already_banned"
    else
        for word, name in pairs(F2B) do
            if rest:sub(1, #word + 1) == word .. " " then
                action = name
                break
            end
        end
    end
    if action then
        out.c_f2b_jail, out.c_f2b_action = jail, action
    end
    if jail == WEB_JAIL and not (action and WEB_SENT[action]) then
        out.c_drop = "1"
    end
end

local KEYS = {
    passwd_changes = true, shadow_changes = true,
    sudoers_changes = true, ssh_config_changes = true,
}

-- The files whose PATH records are sent: the same four watches.
local SENSITIVE = {
    "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow",
    "/etc/sudoers", "/etc/ssh/",
}

local function sensitive_path(msg)
    local name = msg:match(' name="([^"]+)"')
    if not name then
        return false
    end
    for _, prefix in ipairs(SENSITIVE) do
        if name:sub(1, #prefix) == prefix then
            return true
        end
    end
    return false
end

-- Only the SYSCALL records of the four keys and the PATH records of the
-- files they watch leave the server: every other record (a login's
-- USER_LOGIN, a ban's NETFILTER_CFG) repeats what sshd and fail2ban say,
-- and was 68 % of what auditd sent under attack (F0, 9-nov-2026).
local function auditd(msg, out)
    if msg:sub(1, 13) == "type=SYSCALL " then
        local key = msg:match('key="([%a_]+)"')
        if key and KEYS[key] then
            local auid = msg:match(" auid=(%d+) ")
            out.c_audit_key = key
            if auid == "4294967295" then
                out.c_audit_who = "system"
            elseif auid == "0" then
                -- Root, the panel's too when it connects as root: a
                -- person signed in as root with another key cannot be
                -- told apart here (review of 12-nov-2026).
                out.c_audit_who = "root"
            elseif auid == PANEL.uid then
                out.c_audit_who = "panel"
            else
                out.c_audit_who = "user"
            end
            return
        end
    elseif msg:sub(1, 10) == "type=PATH " and sensitive_path(msg) then
        return
    end
    out.c_drop = "1"
end

-- ── The instances' failed web logins (proxy.access) ──────────────────
--
-- The grep before this script passes the POSTs to /web/login only. A
-- failed login is what the web-login jail counts (host_hardening.yml):
-- POST, path /web/login or /web/login/totp with or without a query, and
-- 200 (the form drawn again; a login that works is redirected). The
-- fields are found by their exact quoted names: Traefik escapes the quotes
-- a client sends, so a field forged inside the path never matches.

local WEB_PER_MINUTE = tonumber(PANEL.weblogin_per_minute) or 600
local web_minute, web_sent = -1, 0

-- The value Traefik wrote for the field *name*, or nil.
local function field(line, name)
    local key = '"' .. name .. '":"'
    local at = line:find(key, 1, true)
    if not at then
        return nil
    end
    local first = at + #key
    local last = line:find('"', first, true)
    if not last then
        return nil
    end
    return line:sub(first, last - 1)
end

local function starts_with(text, prefix)
    return #prefix > 0 and text:sub(1, #prefix) == prefix
end

-- Whether *line* is a failed web login, as the jail's filter says.
local function weblogin_failed(line)
    if not line:find('"RequestMethod":"POST"', 1, true) then
        return false
    end
    if not line:find('"DownstreamStatus":200,', 1, true) then
        return false
    end
    local path = field(line, "RequestPath")
    if not path then
        return false
    end
    local query = path:find("?", 1, true)
    local base = query and path:sub(1, query - 1) or path
    return base == "/web/login" or base == "/web/login/totp"
end

-- The id of the instance the proxy answered *line* for, "0" when none: a
-- service named exactly as a layer named it, else the longest instance
-- prefix a router (with ``https-`` or not) or a service starts with.
-- Compared as text, never as a Lua pattern (``-`` and ``.`` are special).
local function instance_of(line)
    local router = field(line, "RouterName") or ""
    local service = field(line, "ServiceName") or ""
    local best, best_len = "0", 0
    for _, inst in ipairs(PANEL.instances or {}) do
        if service ~= "" and inst.extra and inst.extra[service] then
            return inst.id
        end
        local prefix = inst.prefix or ""
        if #prefix > best_len and (
            starts_with(router, prefix)
            or starts_with(router, "https-" .. prefix)
            or starts_with(service, prefix)
        ) then
            best, best_len = inst.id, #prefix
        end
    end
    return best
end

-- An address as Traefik writes ClientHost: digits, hexadecimal letters,
-- dots and colons, at most 45.
local function address(text)
    if not text or #text == 0 or #text > 45 or text:find("[^%x%.:]") then
        return nil
    end
    if not (text:find(".", 1, true) or text:find(":", 1, true)) then
        return nil
    end
    return text
end

-- The proxy's time, as Traefik writes it (RFC 3339), or nil.
local function proxy_time(text)
    if not text or #text == 0 or #text > 40 or text:find("[^%d%-T:Z%.%+]") then
        return nil
    end
    return text
end

-- Returns the line to send, or nil to drop the record uncounted.
local function weblogin(line, timestamp, out)
    if not weblogin_failed(line) then
        return nil
    end
    local ip = address(field(line, "ClientHost"))
    if not ip then
        return nil
    end
    local id = instance_of(line)
    local t = proxy_time(field(line, "time"))
        or os.date("!%Y-%m-%dT%H:%M:%SZ", math.floor(tonumber(timestamp) or os.time()))
    out.c_web_instance = id
    -- Counted always, sent up to WEB_PER_MINUTE a minute.
    local minute = math.floor(os.time() / 60)
    if minute ~= web_minute then
        web_minute, web_sent = minute, 0
    end
    web_sent = web_sent + 1
    if web_sent > WEB_PER_MINUTE then
        out.c_drop = "1"
    end
    return "weblogin failed ip=" .. ip .. " instance=" .. id .. " t=" .. t
end

function classify(tag, timestamp, record)
    local out = {host = PANEL.host, host_id = PANEL.host_id}
    local msg
    if tag == "proxy.access" then
        msg = weblogin(record["log"] or "", timestamp, out)
        if not msg then
            return -1, timestamp, record
        end
        out.source = "weblogin"
        out.message = msg
        return 1, timestamp, out
    elseif tag == "journal" then
        msg = record["MESSAGE"] or ""
        -- The journal input only admits ssh's units and identifiers, and
        -- sudo's.
        if record["SYSLOG_IDENTIFIER"] == "sudo" then
            out.source = "sudo"
            sudo(msg, out, record["_EXE"])
        else
            out.source = "sshd"
            ssh(msg, out)
        end
    elseif tag == "file.auditd" then
        msg = record["log"] or ""
        out.source = "auditd"
        auditd(msg, out)
    else
        msg = record["log"] or ""
        out.source = "fail2ban"
        fail2ban(msg, out)
    end
    out.message = msg
    return 1, timestamp, out
end
