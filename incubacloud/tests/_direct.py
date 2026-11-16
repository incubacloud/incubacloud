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



def central_names_as_new(settings):
    """Clear the central's public and direct names, as a new database has them.

    A copy of production may have the central on its single name, with
    both names set; without the public name the single path is off
    whatever its switch says, and the tests that set neither read the
    paths of before. By SQL, so that no write queues the central's
    deployment or its sync, nothing a test is about.

    :param settings: the system's ``cloud.settings`` record.
    """
    settings.env.cr.execute(
        "UPDATE cloud_settings SET metrics_central_public_host = NULL, "
        "metrics_central_direct_host = NULL WHERE id = %s",
        [settings.id],
    )
    settings.invalidate_recordset([
        "metrics_central_public_host", "metrics_central_direct_host",
    ])
