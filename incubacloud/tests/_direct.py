"""What the direct-path tests share: hosts at real-looking addresses.

The direct list keeps a host only by a global IPv4 address, so the
documentation ranges (203.0.113.0/24 and its kin) cannot stand in for
one. The tests use 46.4.0.0/24, global and none of production's.
"""


def make_host(env, name, ip, **vals):
    """Create an active host at *ip*; *vals* override the defaults.

    :param env: the test's environment.
    :param str name: the host's name, also its wildcard's first label.
    :param str ip: the host's address.
    :return: the ``cloud.host`` record.
    """
    values = {
        "name": name,
        "ip_address": ip,
        "user": "root",
        "wildcard_domain": f"{name}.example.com",
    }
    values.update(vals)
    return env["cloud.host"].create(values)
