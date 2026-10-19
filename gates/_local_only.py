"""What a browser run of regression-test-fails-on-base may reach: this machine, and no credential for anywhere
else. The run starts the pull request's own code (a site, its global setup, the specs), so the gate refuses
(exit 2) before Playwright starts when anything that code inherits could reach a production database,
production credentials or a production address.

A target is local when its host is loopback: localhost, a *.localhost name, 127.0.0.0/8 or ::1.

A variable (from the gate's environment, which Playwright, the site and the browser inherit; a webServer's
`env`; or a .env file a framework would load anywhere in the repository but node_modules and hidden directories,
since a site started from one package of a monorepo loads that package's) is refused when:

- its value is a JWT not issued by a local stack. Supabase's local CLI signs its keys `iss: supabase-demo`,
  and a local auth server's tokens name its loopback address; a hosted project's keys say `iss: supabase`
  and name the project's `ref`, and any other issuer (a deploy platform's OIDC token, say) is not this job's;
- its value is a database connection string (postgres, mysql, mongodb, redis, amqp, or libpq's keyword form)
  naming any host that is not loopback or a Unix socket path (every host of a list, and a `host` or `hostaddr`
  parameter), or any URL carrying a user or password for a host that is not loopback;
- its value is the address of a hosted Supabase project (*.supabase.co, *.supabase.com, *.supabase.in);
- it holds a database's bare host that is not loopback or a Unix socket path (libpq's PGHOST and PGHOSTADDR,
  DB_HOST, POSTGRES_HOST);
- or its name says it is a credential (a KEY, SECRET, TOKEN, PASSWORD, PASSWD, PASS, PWD or CREDENTIALS part,
  the shell's own PWD aside, a part ending in PASSWORD, PASSWD, SECRET or TOKEN, or libpq's PGPASSFILE,
  PGSERVICE or PGSERVICEFILE, as in SUPABASE_SERVICE_ROLE_KEY, SUPABASE_ACCESS_TOKEN, RESEND_API_KEY,
  VERCEL_TOKEN, DB_PASSWORD, PGPASSWORD, MYSQL_PWD) and
  the manifest does not declare it the local stack's with --local-env NAME. A declared name still has its
  value checked by the four rules above.

Values are never printed: a refusal names the variable, where it was read, and the host, issuer or project.
"""
import base64
import binascii
import functools
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import urllib.parse

DATABASE_PARTS = {"DB", "DATABASE", "POSTGRES", "POSTGRESQL", "PG", "MYSQL", "MARIADB", "MONGO", "MONGODB", "REDIS", "SUPABASE"}
# libpq's keyword form of a connection string: `host=db.example.com user=app password=secret dbname=app`.
LIBPQ_HOSTS = re.compile(r"(?:^|\s)(?:host|hostaddr)\s*=\s*'?([^\s']+)")
LIBPQ_KEYS = re.compile(r"(?:^|\s)(?:user|password|dbname|port|sslmode)\s*=")
# Variables that point a database client at a password file or a service entry, wherever it is.
CREDENTIAL_NAMES = {"PGPASSFILE", "PGSERVICE", "PGSERVICEFILE"}
CREDENTIAL_PARTS = {"KEY", "KEYS", "SECRET", "SECRETS", "TOKEN", "TOKENS", "PASSWORD", "PASSWORDS", "PASSWD", "PASS",
                    "CREDENTIAL", "CREDENTIALS", "APIKEY", "AUTHTOKEN"}
DATABASE_SCHEMES = {"postgres", "postgresql", "mysql", "mariadb", "mongodb", "mongodb+srv", "redis", "rediss", "amqp", "amqps"}
HOSTED_SUPABASE = (".supabase.co", ".supabase.com", ".supabase.in")
LOCAL_ISSUER = "supabase-demo"
# A .env file a framework loads: `.env`, `.env.local`, `.env.production` and so on, but not a checked-in example.
EXAMPLE_PARTS = {"example", "sample", "template", "dist"}
DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*?)\s*$")


def local_host(host):
    """Whether `host` is this machine: localhost, a *.localhost name or a loopback address."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_url(url):
    """Whether `url` is an address on this machine."""
    try:
        return local_host(urllib.parse.urlsplit(url).hostname)
    except ValueError:
        return False


def token(value):
    """(issuer, project ref) of a JWT, or None when `value` is not one."""
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        header, payload = (json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))) for part in parts[:2])
    except (ValueError, binascii.Error):
        return None
    if not (isinstance(header, dict) and "alg" in header and isinstance(payload, dict)):
        return None
    return str(payload.get("iss") or ""), payload.get("ref")


def address(value):
    """(scheme, host, whether it carries a user or password) of a URL-shaped value, or None."""
    if "://" not in value:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return parsed.scheme.lower(), host, bool(parsed.username or parsed.password)


def credential_name(name):
    """A KEY, SECRET, TOKEN, PASSWORD, PASSWD, PASS, PWD or CREDENTIALS part (the shell's own PWD and OLDPWD aside), a
    part ending in PASSWORD, PASSWD, SECRET or TOKEN (libpq's PGPASSWORD), or libpq's password file or service."""
    parts = re.split(r"[^A-Z0-9]+", name.upper())
    return name.upper() in CREDENTIAL_NAMES or name not in ("PWD", "OLDPWD") and any(
        part in CREDENTIAL_PARTS or part == "PWD" or part.endswith(("PASSWORD", "PASSWD", "SECRET", "TOKEN")) for part in parts)


def database_host(name):
    """Whether `name` holds a database's bare host: libpq's PGHOST and PGHOSTADDR, or a HOST or HOSTNAME part beside
    a database's (DB_HOST, POSTGRES_HOST, REDIS_HOSTNAME)."""
    parts = set(re.split(r"[^A-Z0-9]+", name.upper()))
    return bool(parts & {"PGHOST", "PGHOSTADDR"} or parts & {"HOST", "HOSTNAME"} and parts & DATABASE_PARTS)


def host_of(item):
    """The host of `host`, `host:port`, `[v6]` or `[v6]:port`."""
    item = item.strip()
    if item.startswith("["):
        return item[1:item.find("]")] if "]" in item else item
    return item.rsplit(":", 1)[0] if item.count(":") == 1 else item


def database_hosts(value):
    """Every host a database connection string names, or None when `value` is not one: a URL's hosts (libpq takes a
    list, `h1:5432,h2:5432`) and its `host` and `hostaddr` parameters, or every `host` and `hostaddr` of libpq's
    keyword form. A URL with no host (`postgresql:///app`) is a Unix socket on this machine."""
    if "://" in value:
        try:
            parsed = urllib.parse.urlsplit(value)
        except ValueError:
            return None
        if parsed.scheme.lower() not in DATABASE_SCHEMES:
            return None
        query = urllib.parse.parse_qs(parsed.query)
        hosts = parsed.netloc.rsplit("@", 1)[-1].split(",") + [part for key in ("host", "hostaddr")
                                                               for item in query.get(key, []) for part in item.split(",")]
    elif LIBPQ_KEYS.search(value):
        hosts = [part for item in LIBPQ_HOSTS.findall(value) for part in item.split(",")]
    else:
        return None
    return [host_of(item) for item in hosts if item.strip()]


def remote_database(value):
    """The first host of a database connection string that is not this machine (a path is a Unix socket here), "" when
    every host is this machine, None when `value` is not a connection string."""
    hosts = database_hosts(value)
    if hosts is None:
        return None
    return next((host for host in hosts if not (local_host(host) or host.startswith("/"))), "")


def value_reason(value):
    """What `value` says by its shape alone: why it reaches something that is not this machine, "" when it is this
    machine's, None when its shape says neither (a plain string, an address of no database)."""
    issued = token(value)
    if issued:
        issuer, ref = issued
        if issuer == LOCAL_ISSUER or local_url(issuer):
            return ""
        return "holds a token issued by %s%s" % (issuer or "an unnamed issuer", " for project %s" % ref if ref else "")
    remote = remote_database(value)
    if remote is not None:
        return remote and "holds a connection string for %s, which is not this machine" % remote
    found = address(value)
    if not found:
        return None
    scheme, host, userinfo = found
    if userinfo:
        return "" if local_host(host) else "holds an address with a password in it for %s, which is not this machine" % (
            host or "no host")
    if host.lower().endswith(HOSTED_SUPABASE):
        return "holds the address of a hosted Supabase project (%s)" % host
    return None


def why(name, value, local_names):
    """Why the variable `name` holding `value` could reach something that is not this machine, or None."""
    value = value.strip()
    reason = value_reason(value) if value else ""
    if reason is not None:
        return reason or None
    hosts = [host.strip() for host in value.split(",") if host.strip()]
    if database_host(name) and not all(local_host(host.rsplit(":", 1)[0] if host.count(":") == 1 else host) or host.startswith("/")
                                       for host in hosts):  # a path is a Unix socket on this machine
        return "holds a database host that is not this machine (%s)" % value
    if credential_name(name) and name not in local_names:
        return "is named as a credential, and the manifest does not declare it the local stack's (--local-env %s)" % name
    return None


class Refuser:
    """A proxy on this machine that every request of a run goes to, for any host but loopback: it answers each one
    403 and records the host it asked for (`CONNECT host:443`, or the URL of a plain `GET`), so the gate knows the run
    reached for somewhere else even when no test's own error says so (a page's script, an in-page fetch, the site's
    server-side fetch)."""

    def __init__(self):
        self.hosts = []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.done.is_set():
            try:
                connection, _ = self.server.accept()
            except OSError:  # the accept timed out, or the server closed
                continue
            with connection:
                try:
                    connection.settimeout(2)
                    line = connection.recv(4096).split(b"\r\n", 1)[0].decode("latin-1").split()
                    target = line[1] if len(line) > 1 else "?"
                    self.hosts.append(urllib.parse.urlsplit(target).netloc or target)
                    connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    def close(self):
        self.done.set()
        self.thread.join()
        self.server.close()
        return sorted(set(self.hosts))


# What the Node side of a run is told, so its fetch and http go to the gate's proxy for every host but this machine.
NO_PROXY = "localhost,127.0.0.1,::1,.localhost"
PROBE = ('const http = require("http"); fetch("http://fetch.vv-probe.invalid/").catch(() => {}).finally(() => '
         'http.get("http://http.vv-probe.invalid/", (response) => response.resume()).on("error", () => {}));')


def proxy_env(port):
    """The environment that sends Node's own fetch and http, and any library that reads HTTP_PROXY, to the proxy at
    `port` for every host but loopback. A raw TCP client, such as a database driver, does not read it."""
    proxy = "http://127.0.0.1:%d" % port
    return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
            "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY, "NODE_USE_ENV_PROXY": "1"}


@functools.lru_cache(maxsize=None)
def node_follows_proxy():
    """Whether the Node on PATH sends both fetch and http.get to the proxy under proxy_env(), probed with one of each
    to a name that cannot resolve: a Node that does not would let the site's server reach other hosts unseen."""
    refuser = Refuser()
    try:
        subprocess.run(["node", "-e", PROBE], env=dict(os.environ, **proxy_env(refuser.port)), capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {host.split(":")[0] for host in refuser.close()} == {"fetch.vv-probe.invalid", "http.vv-probe.invalid"}


def dotenv(path):
    """[(name, value)] of a .env file's assignments; quotes around a value are dropped."""
    pairs = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            found = DOTENV_LINE.match(line)
            if not found or line.lstrip().startswith("#"):
                continue
            quoted = re.match(r"""(['"`])(.*?)\1""", found.group(2))
            pairs.append((found.group(1), quoted.group(2) if quoted else re.sub(r"\s+#.*$", "", found.group(2))))
    return pairs


def env_files(directory):
    """The .env files in `directory` a framework would load, sorted."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [os.path.join(directory, name) for name in sorted(names)
            if (name == ".env" or name.startswith(".env.")) and not EXAMPLE_PARTS & set(name.split(".")[2:])
            and os.path.isfile(os.path.join(directory, name))]


def env_files_under(root):
    """Every .env file a framework would load anywhere under `root`, but in node_modules or a hidden directory: a
    site started from a package of a monorepo (`pnpm --filter web dev`) loads its package's."""
    found = []
    for directory, names, _ in os.walk(root):
        names[:] = sorted(name for name in names if name != "node_modules" and not name.startswith("."))
        found += env_files(directory)
    return found


def refusals(environment, paths, local_names, servers=()):
    """Why each variable the run would inherit is refused: from `environment`, the .env files in `paths`, and each
    webServer's `env` ({"env": {...}} in `servers`). [] when nothing is."""
    found = [("the environment", name, value) for name, value in sorted(environment.items())]
    for path in dict.fromkeys(paths):
        found += [(path, name, value) for name, value in dotenv(path)]
    for index, server in enumerate(servers):
        found += [("webServer %d's env" % (index + 1), name, str(value)) for name, value in sorted((server.get("env") or {}).items())]
    return ["%s: %s %s" % (where, name, reason) for where, name, value in found
            for reason in [why(name, value, local_names)] if reason]
