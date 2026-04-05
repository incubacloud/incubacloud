"""Mapping of Python packages to their APT build dependencies.

When a pip package listed here appears in ``pip_dependencies``, the
corresponding APT packages are automatically added to
``apt_dependencies`` so the Docker image build can compile them.

Only packages that require C extensions / system libraries are listed.
Pure-Python packages never need APT deps and are omitted.
"""

import re

# {pip_package_name (lowercase): [apt_package, ...]}
PIP_TO_APT = {
    # Database clients
    'mysqlclient':      ['pkg-config', 'libmariadb-dev-compat', 'libmariadb-dev'],
    'pymssql':          ['freetds-dev'],
    'psycopg2':         ['libpq-dev'],
    'cx-oracle':        ['libaio1'],
    # XML / HTML
    'lxml':             ['libxml2-dev', 'libxslt1-dev'],
    # Image processing
    'pillow':           ['libjpeg-dev', 'zlib1g-dev', 'libfreetype6-dev'],
    # Cryptography
    'cryptography':     ['libssl-dev', 'libffi-dev'],
    'cffi':             ['libffi-dev'],
    'pynacl':           ['libsodium-dev'],
    # LDAP
    'python-ldap':      ['libldap2-dev', 'libsasl2-dev'],
    # Concurrency
    'greenlet':         ['gcc'],
    'gevent':           ['gcc', 'libevent-dev'],
    'uvloop':           ['gcc'],
    # Compression
    'python-snappy':    ['libsnappy-dev'],
    'lz4':              ['liblz4-dev'],
    'brotli':           ['gcc'],
    # Scientific / data
    'numpy':            ['gcc', 'gfortran'],
    'scipy':            ['gcc', 'gfortran', 'libopenblas-dev'],
    'pandas':           ['gcc'],
    # PDF / reporting
    'weasyprint':       ['libpango-1.0-0', 'libpangocairo-1.0-0',
                         'libgdk-pixbuf2.0-0', 'libffi-dev'],
    'cairocffi':        ['libcairo2-dev', 'libffi-dev'],
    'cairosvg':         ['libcairo2-dev'],
    # SSH / network
    'bcrypt':           ['gcc'],
    'paramiko':         ['gcc'],
    # YAML (C extension)
    'pyyaml':           ['libyaml-dev'],
    # Misc
    'python-magic':     ['libmagic1'],
    'zbar':             ['libzbar-dev'],
    'pygraphviz':       ['graphviz', 'libgraphviz-dev'],
    'psutil':           ['gcc'],
    'multidict':        ['gcc'],
    'yarl':             ['gcc'],
    'aiohttp':          ['gcc'],
    'markupsafe':       ['gcc'],
}

_PKG_NAME_RE = re.compile(r'^([A-Za-z0-9_.-]+)')


def resolve_apt_dependencies(pip_text):
    """Return a set of APT packages required by the pip packages in *pip_text*.

    Only packages present in the ``PIP_TO_APT`` mapping are considered.
    """
    apt = set()
    for line in (pip_text or '').splitlines():
        line = line.split('#')[0].strip()
        if not line or line.startswith(('<<<', '===', '>>>')):
            continue
        # git+https://... → skip (no apt mapping)
        if line.startswith(('git+', 'http://', 'https://')):
            m = re.search(r'/([A-Za-z0-9_.-]+?)(?:\.git)?(?:@|$)', line)
            if m:
                name = m.group(1).lower().replace('-', '-')
                apt.update(PIP_TO_APT.get(name, ()))
            continue
        m = _PKG_NAME_RE.match(line)
        if m:
            name = m.group(1).lower().replace('_', '-')
            apt.update(PIP_TO_APT.get(name, ()))
    return apt
