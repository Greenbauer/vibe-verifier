"""What a browser run of regression-test-fails-on-base may reach: this machine, and no credential for anywhere
else. The run starts the pull request's own code (a site, its global setup, the specs), so the gate refuses
(exit 2) before Playwright starts when anything that code inherits could reach a production database,
production credentials or a production address.

A target is local when its host is loopback: localhost, a *.localhost name, 127.0.0.0/8 or ::1.

A variable (from the gate's environment, which Playwright, the site and the browser inherit; a webServer's
`env`; or a .env file a framework loads) is refused when:

- its value is a JWT not issued by a local stack. Supabase's local CLI signs its keys `iss: supabase-demo`,
  and a local auth server's tokens name its loopback address; a hosted project's keys say `iss: supabase`
  and name the project's `ref`, and any other issuer (a deploy platform's OIDC token, say) is not this job's;
- its value is a database connection string (postgres, mysql, mongodb, redis, amqp), or any URL carrying a
  user or password, for a host that is not loopback;
- its value is the address of a hosted Supabase project (*.supabase.co, *.supabase.com, *.supabase.in);
- or its name says it is a credential (a KEY, SECRET, TOKEN, PASSWORD, PASSWD, PASS or CREDENTIALS part,
  as in SUPABASE_SERVICE_ROLE_KEY, SUPABASE_ACCESS_TOKEN, RESEND_API_KEY, VERCEL_TOKEN, DB_PASSWORD) and
  the manifest does not declare it the local stack's with --local-env NAME. A declared name still has its
  value checked by the three rules above.

Values are never printed: a refusal names the variable, where it was read, and the host, issuer or project.
"""
import base64
import binascii
import ipaddress
import json
import os
import re
import urllib.parse

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
    return any(part in CREDENTIAL_PARTS for part in re.split(r"[^A-Z0-9]+", name.upper()))


def why(name, value, local_names):
    """Why the variable `name` holding `value` could reach something that is not this machine, or None."""
    value = value.strip()
    if not value:
        return None
    issued = token(value)
    if issued:
        issuer, ref = issued
        if issuer == LOCAL_ISSUER or local_url(issuer):
            return None
        return "holds a token issued by %s%s" % (issuer or "an unnamed issuer", " for project %s" % ref if ref else "")
    found = address(value)
    if found:
        scheme, host, userinfo = found
        if scheme in DATABASE_SCHEMES or userinfo:
            if local_host(host):
                return None
            return "holds %s for %s, which is not this machine" % (
                "a connection string" if scheme in DATABASE_SCHEMES else "an address with a password in it", host or "no host")
        if host.lower().endswith(HOSTED_SUPABASE):
            return "holds the address of a hosted Supabase project (%s)" % host
    if credential_name(name) and name not in local_names:
        return "is named as a credential, and the manifest does not declare it the local stack's (--local-env %s)" % name
    return None


def dotenv(path):
    """[(name, value)] of a .env file's assignments; quotes around a value are dropped."""
    pairs = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            found = DOTENV_LINE.match(line)
            if not found or line.lstrip().startswith("#"):
                continue
            value = found.group(2)
            if len(value) > 1 and value[0] in "'\"`" and value.endswith(value[0]):
                value = value[1:-1]
            else:
                value = re.sub(r"\s+#.*$", "", value)
            pairs.append((found.group(1), value))
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


def refusals(environment, directories, local_names, servers=()):
    """Why each variable the run would inherit is refused: from `environment`, the .env files in `directories`,
    and each webServer's `env` ({"env": {...}} in `servers`). [] when nothing is."""
    found = [("the environment", name, value) for name, value in sorted(environment.items())]
    for directory in dict.fromkeys(directories):
        for path in env_files(directory):
            found += [(path, name, value) for name, value in dotenv(path)]
    for index, server in enumerate(servers):
        found += [("webServer %d's env" % (index + 1), name, str(value)) for name, value in sorted((server.get("env") or {}).items())]
    return ["%s: %s %s" % (where, name, reason) for where, name, value in found
            for reason in [why(name, value, local_names)] if reason]
