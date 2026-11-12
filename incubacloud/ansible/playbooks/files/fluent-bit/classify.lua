-- Classify one security record for Fluent Bit (host_observability.yml).
--
-- Sets ``source`` and ``host`` (Loki's labels), the ``c_*`` fields the
-- log_to_metrics filters count, ``c_drop`` on what is counted but not
-- sent, and keeps only the line itself in ``message``.
--
-- What this panel knows about itself comes from panel.lua, a data file
-- the playbook renders (``return {host = ..., fps = {...}, user = ...,
-- uid = ...}``), so this file carries no template.
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

local function sudo(msg, out)
    -- The line of a refused sudo also carries COMMAND=: it is a failure,
    -- not a command run.
    if msg:find("incorrect password attempt", 1, true) then
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
-- Odoo password: its ``Found`` lines name the instances' users, who are
-- not the server's, so only its bans leave the server (review of
-- 12-nov-2026). They are counted all the same.
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

function classify(tag, timestamp, record)
    local out = {host = PANEL.host}
    local msg
    if tag == "journal" then
        msg = record["MESSAGE"] or ""
        -- The journal input only admits ssh's units and identifiers, and
        -- sudo's.
        if record["SYSLOG_IDENTIFIER"] == "sudo" then
            out.source = "sudo"
            sudo(msg, out)
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
