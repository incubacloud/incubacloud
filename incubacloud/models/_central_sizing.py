"""Whether the metrics central fits on a host: a note, never a refusal.

Figures of 12/13-nov-2026, from the central of a running fleet (≈ 1.7 GB
of VictoriaMetrics and Loki for two servers after three months) and the
operations guide's "Costs and sizing": VictoriaMetrics 0.5–1 GB of RAM,
Loki ≈ 0.3 GB, ≈ 0.3–0.8 GB of disk per sending server over 90 days. The
upper figure is used, so the note errs on the side of more. Past 20
sending servers nothing has been measured. Change them here and in
``docs/observability-operations.md`` ("Where the central runs") together.
"""

#: Disk per sending server over 90 days of retention, in GB.
DISK_GB_PER_SERVER_90D = 0.8
#: Disk left over for the stack's images, its compaction and the server.
DISK_MARGIN_GB = 2.0
#: Memory of a host that runs only the central.
RAM_GB_ALONE = 4.0
#: Memory of a host that also runs instances.
RAM_GB_WITH_INSTANCES = 8.0
#: Memory that must stay unassigned to instances for the central.
RAM_GB_FREE_FOR_CENTRAL = 1.5
#: Cores: VictoriaMetrics answers reads with twice the cores at once.
CPU_CORES = 2
#: Beyond this many sending servers the figures are not measured.
SERVERS_MEASURED = 20


def central_sizing(
    *, servers, retention_days, disk_free_gb, ram_total_gb,
    ram_available_gb, cpu_cores, has_instances, full_setup,
):
    """Say whether the central fits on a host, from its last measures.

    :param int servers: servers that would send to the central, the host
        included
    :param int retention_days: the central's retention; 0 keeps everything
    :param float disk_free_gb: free space on the host's root disk, 0 when
        never measured
    :param float ram_total_gb: the host's memory, 0 when never measured
    :param float ram_available_gb: memory not assigned to its instances
    :param int cpu_cores: the host's cores, 0 when never measured
    :param bool has_instances: whether the host runs instances
    :param bool full_setup: whether Full Setup was run on the host
    :return: ``{"measured", "disk_needed_gb", "ram_needed_gb",
        "cpu_needed", "warnings"}``; each warning is ``(code, values)``,
        worded by the caller
    :rtype: dict
    """
    warnings = []
    measured = bool(disk_free_gb or ram_total_gb or cpu_cores)
    disk_needed = None
    if retention_days:
        disk_needed = round(
            DISK_GB_PER_SERVER_90D * max(servers, 1) * retention_days / 90
            + DISK_MARGIN_GB,
            1,
        )
    ram_needed = RAM_GB_WITH_INSTANCES if has_instances else RAM_GB_ALONE
    if not full_setup:
        warnings.append(("no_full_setup", {}))
    if not retention_days:
        warnings.append(("keeps_everything", {}))
    if servers > SERVERS_MEASURED:
        warnings.append(("not_measured", {"servers": servers}))
    if measured:
        if disk_needed is not None and disk_free_gb < disk_needed:
            warnings.append((
                "disk", {"free": disk_free_gb, "needed": disk_needed},
            ))
        if ram_total_gb < ram_needed:
            warnings.append((
                "ram", {"total": ram_total_gb, "needed": ram_needed},
            ))
        if has_instances and ram_available_gb < RAM_GB_FREE_FOR_CENTRAL:
            warnings.append((
                "ram_free", {
                    "free": round(max(ram_available_gb, 0), 1),
                    "needed": RAM_GB_FREE_FOR_CENTRAL,
                },
            ))
        if cpu_cores < CPU_CORES:
            warnings.append(("cpu", {"cores": cpu_cores, "needed": CPU_CORES}))
    return {
        "measured": measured,
        "disk_needed_gb": disk_needed,
        "ram_needed_gb": ram_needed,
        "cpu_needed": CPU_CORES,
        "warnings": warnings,
    }
