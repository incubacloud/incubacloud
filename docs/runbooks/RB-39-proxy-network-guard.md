# RB-39 — The guard on the proxy network

**Severity:** planned (turning it on, a Full Setup) · warning
(`shared_guard_open`) · **When:** a host's first Full Setup with 1.0.228,
a `shared_guard_open` alert, an instance that cannot reach another one it
used to, or turning the guard off.

Every instance's `odoo` container joins `inverseproxy_shared`, the
network the host's Traefik reaches it on, and Docker lets any two
containers of a network talk. Without the guard, code running in one
instance can open the 8069 and 8072 of every other instance awake on the
same host directly: past the CDN, the proxy's routers and its per-address
limits. Their databases and Sablier are not on that network.

## How it works

- The proxy joins `shared` with a fixed, locally administered MAC,
  `02:69:63:00:00:01` (`mac_address` in its compose file). The upgrade to
  1.0.228 adds it to every host's stored compose file; the host's next
  Full Setup recreates the proxy with it. Without it Docker draws a new
  address at every restart of the proxy.
- `scripts/shared_guard.sh` puts a chain of the host's firewall in front
  of the network's bridge:

  ```
  -A DOCKER-USER -i br-<shared> -o br-<shared> -j IC-SHARED
  -A IC-SHARED -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
  -A IC-SHARED -m mac --mac-source 02:69:63:00:00:01 -j RETURN
  -A IC-SHARED -j DROP
  ```

  Only the proxy opens connections on the network; the replies go back
  through conntrack. Traffic the instances send to the internet, to their
  own database or to anything outside `shared` does not cross that bridge
  and is not touched.
- **It fails open.** The chain is removed, and the reason given, when the
  running proxy has another address on `shared`, is not running, the
  network or its bridge is not there, or the firewall cannot do it: no
  `DOCKER-USER` chain (Docker's nftables backend), no MAC match
  (`xt_mac`), or bridged packets that skip iptables
  (`net.bridge.bridge-nf-call-iptables` not 1). A guard on an address the
  proxy no longer has would drop the proxy's own connections and cut every
  instance of the host.
- **Full Setup** runs *Isolate the instances on the proxy network* right
  after *Start Traefik*. It copies the script to
  `/usr/local/lib/incubacloud-shared-guard/`, installs
  `incubacloud-shared-guard.service` and its timer, which runs it every
  minute, and applies it. The rules are lost at every reboot; the timer
  puts them back within a minute, and opens the guard within a minute when
  the proxy comes back with another address. Without the timer the step
  applies nothing. The step never fails the setup: a guard left open
  raises `shared_guard_open` (warning) with the reason, and a run that
  closes it, or removes it as asked, resolves it.
- **The switch:** host page › *General* › *Proxy network* › *Only the
  proxy may open connections to the instances* (`cloud.host.shared_guard`), on by
  default. It is part of the host's configuration: changed, the host
  shows its configuration as not applied until a Full Setup, which then
  installs the guard or removes it, timer and files included.
- The host's teardown removes it too.
- The system log has one line per change of state, tag
  `incubacloud-shared-guard`; the minute runs log nothing else.

What it costs, measured on a test server (Debian 13, Docker 29.9, two
panels, 13-nov-2026): a Full Setup takes the host down for the seconds the
proxy is recreated, as before. After a reboot the guard is back on at the
first minute tick (51 s after boot there); in between it is open. A proxy
recreated by hand with another address leaves its instances without
service until the next tick opens the guard (43 s there); a Full Setup
that ships such a compose file opens it in its own step, a few seconds
after the proxy starts.

## Check it on a host

```bash
$ ssh <host>
sudo bash /usr/local/lib/incubacloud-shared-guard/shared_guard.sh status
sudo iptables -S DOCKER-USER
sudo iptables -S IC-SHARED
sudo journalctl -t incubacloud-shared-guard -n 20
systemctl list-timers incubacloud-shared-guard.timer
```

`status` ends with `IC_SHARED_GUARD_ON -i br-… -o br-… -j IC-SHARED`
when the guard is on, `IC_SHARED_GUARD_OFF no rules` when it is not. It
also prints the proxy's address on `shared` and the last state the timer
wrote. The last line of the log says why the guard is open, if it is.

From one instance to another (both awake), the guard holds when the
connection times out:

```bash
$ ssh <host>
docker exec <project>-odoo-1 python3 -c 'import socket; s=socket.create_connection(("<other project>-odoo-1", 8069), 3)'
```

`TimeoutError` is the guard; no error means the instances still reach
each other. Through the proxy both still answer:
`curl -sk -o /dev/null -w '%{http_code}\n' https://<domain>/web/health`.

## When it is open (`shared_guard_open`)

The alert names the reason, as the log does.

- **`the proxy's address on inverseproxy_shared is …, not 02:69:63:00:00:01`**:
  the proxy runs without the fixed address. Look at its compose file on
  the host page (*Traefik* tab; the *Behind the shipped templates* pill
  names `services.proxy.networks.shared` when it lacks the address): the
  `shared` entry of the `proxy` service needs
  `mac_address: "02:69:63:00:00:01"`. A file that lists the proxy's
  networks as a sequence (`- shared`) cannot take it: write them as a
  map. Then run Full Setup.
- **`the proxy is not running`**: the proxy is down or restarting; the
  instances do not answer either. See why
  (`docker ps -a --filter name=inverseproxy-proxy-1`,
  `docker logs inverseproxy-proxy-1`); the timer closes the guard once it
  runs with the right address.
- **`the firewall has no MAC match (xt_mac)`**: the kernel has no
  `xt_mac` module (`modinfo xt_mac`). Install the distribution's extra
  kernel modules, or leave the guard off on that host.
- **`there is no DOCKER-USER chain`**, with or without *Docker writes its
  rules with nftables*: Docker runs with `"firewall-backend": "nftables"`.
  The guard does not support it yet; turn it off on that host.
- **`bridged packets do not reach iptables`**: `br_netfilter` is not
  loaded or `net.bridge.bridge-nf-call-iptables` is 0, so no rule sees
  traffic between containers of one network. Docker usually sets both;
  find what changed them.
- **`the incubacloud-shared-guard timer could not be installed`** or
  **`exit …`**: the setup's user could not write to
  `/etc/systemd/system` or `/usr/local/lib`, or `sudo -n` asked for a
  password. Fix that and run Full Setup.

`… has IPv6, which this guard does not cover` in the step's log means the
network was given IPv6: the guard only filters IPv4, and the same rules
would be needed in `ip6tables`.

## An instance needs to reach another one

Instances talk to each other through their public addresses, which go
through the proxy like any visitor's. A direct connection between two
containers on `shared` is what the guard is there to stop. If an
integration really needs one, give the two stacks a network of their own
rather than turning the guard off for every instance of the host.

## Rollback

Turn the switch off on the host page and run Full Setup: the step
removes the timer, the rules and the installed files
(`IC_SHARED_GUARD_OFF removed` in its log).

Faster, by hand, when it is in the way now (the next Full Setup puts it
back while the switch is on):

```bash
$ ssh <host>
sudo bash /usr/local/lib/incubacloud-shared-guard/shared_guard.sh uninstall
```

Removing only the rules is not enough: the timer writes them again
within a minute.
