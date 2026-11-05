"""A test run never reaches a real system unless a person says so. Standard library and pytest only.

WHY. On a developer machine a production tunnel may be open, a database password may sit in ~/.pgpass, and cloud
credentials may be in the environment. A test that reaches them writes to, or bills against, a real system.

FOUR REFUSALS, for the whole run (import, collection and every test), lifted only for a test carrying one of the
opt-in markers below (which itself runs only when its variable is set):

1. MARKERS. `live`, `sandbox` and `prod_readonly` tests are skipped at collection unless ALLOW_LIVE_TESTS=1,
   ALLOW_SANDBOX_TESTS=1 or ALLOW_PROD_READONLY_TESTS=1, with a reason naming the variable.
2. NETWORK. A socket connect to any address that is not this machine (loopback, a unix socket) raises
   FenceRefused (an OSError, so code that already handles "network down" degrades the same way).
3. DATABASES. psycopg2.connect / psycopg.connect is refused unless the target is this machine on a port not in
   BLOCKED_LOCAL_PORTS (list the ports your SSH tunnels to real databases use). libpq opens its own sockets, which
   a socket patch cannot see, so the driver entry point is checked as well. sqlite is never refused.
4. CLOUD CREDENTIALS. Cloud credential variables are removed from the environment for each test, the shared config
   and credentials files are pointed at /dev/null and the metadata service is switched off, so nothing can
   authenticate even if a request got through.

Every refusal is listed at the end of the run. A deliberate run: `ALLOW_LIVE_TESTS=1 python3 -m pytest -m live`.
"""
from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit

import pytest

OPT_INS = {"live": "ALLOW_LIVE_TESTS", "sandbox": "ALLOW_SANDBOX_TESTS", "prod_readonly": "ALLOW_PROD_READONLY_TESTS"}
LOOPBACK = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104
# Local ports that are really tunnels to a real database or service. Add yours, e.g. frozenset({5433, 5457}).
BLOCKED_LOCAL_PORTS: frozenset = frozenset()
CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI", "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CLIENT_SECRET",
    "PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGPASSFILE",
)
CLOUD_OFF = {
    "AWS_CONFIG_FILE": "/dev/null",
    "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_DEFAULT_REGION": "us-east-1",
}
HINT = "Mark the test `live` and set ALLOW_LIVE_TESTS=1 to allow it."

REFUSED: list = []  # (who, what) for every refusal in this process
_state = {"nodeid": None, "lifted": False}
_restore: list = []


class FenceRefused(OSError):
    """Raised instead of reaching a real system from a test run."""


def _who() -> str:
    return _state["nodeid"] or "no test running (import, collection or a background thread)"


def _refuse(what: str, hint: str = HINT) -> None:
    REFUSED.append((_who(), what))
    raise FenceRefused(f"test fence: refused {what}. {hint}")


def _is_ip(host) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, str(host))
            return True
        except OSError:
            continue
    return False


def _is_local(host) -> bool:
    return str(host) in LOOPBACK or str(host).startswith("127.")


def _check_socket(address) -> None:
    if _state["lifted"] or not (isinstance(address, tuple) and address):
        return  # lifted, or a unix socket path
    host = address[0].decode(errors="replace") if isinstance(address[0], bytes) else address[0]
    port = address[1] if len(address) > 1 else None
    if not _is_local(host):
        _refuse(f"a network connection to {host}:{port}")
    if port in BLOCKED_LOCAL_PORTS:
        _refuse(f"a connection to local port {port} (a tunnel to a real system)")


def _patch(obj, name, replacement) -> None:
    _restore.append((obj, name, getattr(obj, name)))
    setattr(obj, name, replacement)


def _install_socket_fence() -> None:
    real_connect, real_connect_ex, real_gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def connect(self, address):
        _check_socket(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check_socket(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):  # looking up a real host's name is already a network call
        if not _state["lifted"] and host and not _is_local(host) and not _is_ip(host):
            _refuse(f"a DNS lookup of {host}")
        return real_gai(host, *args, **kwargs)

    _patch(socket.socket, "connect", connect)
    _patch(socket.socket, "connect_ex", connect_ex)
    _patch(socket, "getaddrinfo", getaddrinfo)


def _target_of(args, kwargs):
    """(host, port) a database driver call would dial, from a DSN string, a URL or keyword arguments."""
    host, port = kwargs.get("host"), kwargs.get("port")
    dsn = str(args[0]) if args and isinstance(args[0], str) else str(kwargs.get("dsn") or kwargs.get("conninfo") or "")
    if "://" in dsn:
        u = urlsplit(dsn)
        host, port = u.hostname or host, u.port or port
    else:
        for part in dsn.split():
            if part.startswith("host="):
                host = part[5:]
            elif part.startswith("port="):
                port = part[5:]
    if host is None:
        host = os.environ.get("PGHOST", "")  # libpq's own default is the local unix socket
    try:
        port = int(port or os.environ.get("PGPORT") or 0)
    except ValueError:
        port = 0
    return str(host), port


def _install_database_fence() -> None:
    for modname in ("psycopg2", "psycopg"):
        try:
            mod = __import__(modname)
        except Exception:  # noqa: BLE001 - not installed here, nothing to fence
            continue

        def connect(*args, _real=mod.connect, _name=modname, **kwargs):
            if not _state["lifted"]:
                host, port = _target_of(args, kwargs)
                local = host.startswith("/") or _is_local(host)
                if not local or port in BLOCKED_LOCAL_PORTS:
                    _refuse(f"a {_name} database connection to {host or '?'}:{port or '?'}",
                            "Use a disposable local test database, or mark the test `live` and set ALLOW_LIVE_TESTS=1.")
            return _real(*args, **kwargs)

        _patch(mod, "connect", connect)


def pytest_configure(config):
    for marker, var in OPT_INS.items():
        config.addinivalue_line("markers", f"{marker}: reaches a real system; runs only with {var}=1")
    if os.environ.get("ALLOW_LIVE_TESTS") == "1":
        _state["lifted"] = True  # the person said so for the whole run
        return
    _install_socket_fence()
    _install_database_fence()


def pytest_unconfigure(config):
    while _restore:
        obj, name, original = _restore.pop()
        setattr(obj, name, original)


def pytest_collection_modifyitems(config, items):
    for item in items:
        for marker, var in OPT_INS.items():
            if item.get_closest_marker(marker) and os.environ.get(var) != "1":
                item.add_marker(pytest.mark.skip(reason=f"{marker} test: set {var}=1 to run it"))


@pytest.fixture(autouse=True)
def _test_fence(request, monkeypatch):
    """Per test: record which test is running, lift the fence only for an opted-in marked test, strip credentials."""
    marked = any(request.node.get_closest_marker(m) for m in OPT_INS)
    _state["nodeid"] = request.node.nodeid
    previous = _state["lifted"]
    _state["lifted"] = previous or marked
    if not _state["lifted"]:
        for var in CREDENTIAL_VARS:
            monkeypatch.delenv(var, raising=False)
        for var, value in CLOUD_OFF.items():
            monkeypatch.setenv(var, value)
    yield
    _state["nodeid"] = None
    _state["lifted"] = previous


def pytest_terminal_summary(terminalreporter):
    if REFUSED:
        terminalreporter.section("test fence: refused real-system access")
        for who, what in REFUSED:
            terminalreporter.line(f"  {who}: {what}")
