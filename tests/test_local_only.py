"""gates/_local_only.py's rules on the variable names and value shapes subscribed repositories use, and
gates/_regression_playwright.py's text handling: the file filter it hands Playwright and an error as one line. The
gate run end to end is tests/test_regression_playwright.py."""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT

sys.path.insert(0, str(ROOT / "gates"))
import _local_only  # noqa: E402  (gates/ is not a package)
import _regression_playwright  # noqa: E402
from _regression_runners import missing  # noqa: E402

HOSTED = "abcdefghijklmnopqrst"  # a made-up project ref, the shape of a hosted Supabase project's


def jwt(payload):
    """An unsigned JWT with this payload: the gate reads a token's claims, never its signature."""
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")  # noqa: E731
    return "%s.%s.c2lnbmF0dXJl" % (part({"alg": "HS256", "typ": "JWT"}), part(payload))


class LocalOnly(unittest.TestCase):
    """What counts as this machine, and which variables hold a credential for anywhere else."""

    def test_what_the_job_s_own_local_stack_holds_is_local(self):
        for name, value in (("NEXT_PUBLIC_SUPABASE_ANON_KEY", jwt({"iss": "supabase-demo", "role": "anon", "exp": 1983812996})),
                            ("SUPABASE_SERVICE_ROLE_KEY", jwt({"iss": "supabase-demo", "role": "service_role", "exp": 1983812996})),
                            ("TEST_SESSION_TOKEN", jwt({"iss": "http://127.0.0.1:54321/auth/v1", "role": "authenticated"})),
                            ("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"),
                            ("REDIS_URL", "redis://localhost:6379"), ("SUPABASE_URL", "http://127.0.0.1:54321"),
                            ("NEXT_PUBLIC_SITE_URL", "http://app.localhost:3000"), ("GITHUB_SERVER_URL", "https://github.com"),
                            ("PLAYWRIGHT_AUTH_BYPASS", "true"), ("PWD", "/work"), ("TEST_BASE_URL", ""),
                            ("PGHOST", "127.0.0.1:54322"), ("PGHOST", "/var/run/postgresql"), ("DB_HOST", "localhost"),
                            ("POSTGRES_HOST", "::1"), ("DATABASE_URL", "host=127.0.0.1 port=54322 user=postgres"),
                            ("DATABASE_URL", "postgresql:///app"), ("DATABASE_URL", "postgres://u:p@[::1]:5432,localhost:5433/app"),
                            ("NOTES", "a host=b thing"), ("SLACK_WEBHOOK_URL", "http://localhost:3000/api/webhooks/slack"),
                            ("SITE_HOST", "app.example.com")):
            self.assertIsNone(_local_only.why(name, value, set()), name)
        self.assertIsNone(_local_only.why("SESSION_SECRET", "a-local-secret", {"SESSION_SECRET"}))

    def test_what_reaches_somewhere_else_is_refused_even_when_declared_local(self):
        for name, value, said in (
                ("SUPABASE_SERVICE_ROLE_KEY", jwt({"iss": "supabase", "ref": HOSTED, "role": "service_role"}),
                 "holds a token issued by supabase for project " + HOSTED),
                ("VERCEL_OIDC_TOKEN", jwt({"iss": "https://oidc.example.com/team", "sub": "x"}),
                 "holds a token issued by https://oidc.example.com/team"),
                ("POSTGRES_URL", "postgres://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres",
                 "holds a connection string for aws-0-us-east-1.pooler.supabase.com"),
                ("DIRECT_URL", "postgresql://db.internal.example.com/app", "holds a connection string for db.internal.example.com"),
                ("WEBHOOK", "https://user:pass@hooks.example.com/x", "holds an address with a password in it for hooks.example.com"),
                ("SUPABASE_URL", "https://%s.supabase.co" % HOSTED, "holds the address of a hosted Supabase project"),
                ("PGHOST", "db.%s.supabase.co" % HOSTED, "holds a database host that is not this machine"),
                ("REDIS_HOSTNAME", "localhost,cache.example.com", "holds a database host that is not this machine"),
                ("DATABASE_URL", "host=db.example.com user=app password=pw dbname=app",
                 "holds a connection string for db.example.com, which is not this machine"),
                ("DATABASE_URL", "host=localhost,db.example.com user=app password=pw", "holds a connection string for db.example.com"),
                ("DATABASE_URL", "host=127.0.0.1 hostaddr=10.0.0.5 user=app password=pw", "holds a connection string for 10.0.0.5"),
                ("DATABASE_URL", "postgresql:///app?host=db.example.com&password=pw", "holds a connection string for db.example.com"),
                ("DATABASE_URL", "postgres://u:p@db.example.com:5432,127.0.0.1:5432/app", "holds a connection string for db.example.com"),
                ("REVALIDATION_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/x", "holds a webhook address for hooks.slack.com"),
                ("ALERTS", "https://discord.com/api/webhooks/1/abc", "holds a webhook address for discord.com"),
                ("REBUILD", "https://api.vercel.com/v1/integrations/deploy/prj_1/abc", "holds a webhook address for api.vercel.com")):
            self.assertIn(said, _local_only.why(name, value, {name}) or "", name)
        for name in ("SUPABASE_ACCESS_TOKEN", "VERCEL_TOKEN", "RESEND_API_KEY", "NETSUITE_CONSUMER_KEY", "CRON_SECRET",
                     "SUPABASE_DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "GH_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "DB_PASS",
                     "npm_config__authtoken", "PGPASSWORD", "MYSQLPASSWORD", "STRIPESECRET", "MYSQL_PWD", "PGPASSFILE", "PGSERVICE"):
            self.assertIn("is named as a credential", _local_only.why(name, "a-value", set()) or "", name)

    def test_only_loopback_is_this_machine(self):
        for host in ("localhost", "app.localhost", "127.0.0.1", "127.8.0.1", "::1", "[::1]", "LOCALHOST."):
            self.assertTrue(_local_only.local_host(host), host)
        for host in ("0.0.0.0", "10.0.0.5", "localhost.example.com", "app.example.com", "", None):
            self.assertFalse(_local_only.local_host(host), host)

    def test_the_env_files_a_framework_loads_are_read_and_examples_are_not(self):
        directory = Path(tempfile.mkdtemp(prefix="vv-dotenv-"))
        self.addCleanup(shutil.rmtree, directory, True)
        for name in (".env", ".env.local", ".env.production", ".env.example", ".env.local.sample", "env.txt"):
            (directory / name).write_text("export A_KEY='x' # a comment\nB=\"y\"\n# C_TOKEN=z\nD=plain # note\n")
        self.assertEqual([Path(path).name for path in _local_only.env_files(str(directory))], [".env", ".env.local", ".env.production"])
        self.assertEqual(_local_only.dotenv(str(directory / ".env")), [("A_KEY", "x"), ("B", "y"), ("D", "plain")])
        # Anywhere in the repository, as a site started from one package of a monorepo loads its package's, but not
        # in node_modules or a hidden directory.
        for path in ("apps/web/.env.local", "node_modules/pkg/.env", ".next/.env", "apps/web/.env.example"):
            (directory / path).parent.mkdir(parents=True, exist_ok=True)
            (directory / path).write_text("A=b\n")
        self.assertEqual([str(Path(path).relative_to(directory)) for path in _local_only.env_files_under(str(directory))],
                         [".env", ".env.local", ".env.production", "apps/web/.env.local"])


@unittest.skipUnless(shutil.which("node"), "the guard is a Node preload")
class Guard(unittest.TestCase):
    """_local_only.GUARD in a Node process, as every Node process of a browser run preloads it."""

    def test_a_node_process_reaches_only_this_machine_however_it_connects(self):
        script = """const dgram = require("dgram"), dns = require("dns"), http = require("http"), net = require("net");
const out = (label) => (error) => console.log(label, error ? error.code : "connected");
const tcp = (label, ...args) => net.connect(...args, () => { console.log(label, "connected"); process.exit(9); }).on("error", out(label));
http.get("http://rates.example.com/", { agent: new http.Agent() }, () => process.exit(9)).on("error", out("agent"));
tcp("raw", 5432, "10.255.255.1");
tcp("name", 80, "127.foo.example.com");
tcp("lookup", { port: 80, host: "localhost", lookup: (host, options, done) => options.all ? done(null, [{ address: "10.255.255.1", family: 4 }]) : done(null, "10.255.255.1", 4) });
tcp("loopback", 9, "127.0.0.1");
tcp("ipv6", 9, "::1");
const udp = dgram.createSocket("udp4");
udp.send("x", 53, "10.255.255.1", (error) => { out("udp")(error); udp.close(); });
"""
        scratch = tempfile.mkdtemp(prefix="vv-guard-")
        self.addCleanup(shutil.rmtree, scratch, True)
        env, record, _ = _local_only.node_guard(scratch)
        result = subprocess.run(["node", "-e", script], env=dict(os.environ, **env), capture_output=True, text=True, timeout=60)
        said = dict(line.split(" ", 1) for line in result.stdout.splitlines())
        # Nothing connects. An address elsewhere is refused before it leaves, a name by what it resolves to (one that does
        # not resolve fails as it would anyway); each is recorded. Loopback goes through, unrecorded: nothing listens there.
        self.assertEqual(set(said), {"agent", "raw", "name", "lookup", "loopback", "ipv6", "udp"}, result.stdout + result.stderr)
        self.assertNotIn("connected", said.values())
        self.assertEqual((said["raw"], said["lookup"], said["udp"], said["loopback"]), ("ECONNREFUSED",) * 4)
        self.assertEqual(sorted(_local_only.reached_by_node(record)), sorted([
            "rates.example.com:80", "10.255.255.1:5432", "127.foo.example.com:80", "localhost:80", "10.255.255.1:53/udp"]))


class Filters(unittest.TestCase):
    """gates/_regression_playwright.py's text handling: the file filter it hands Playwright, and an error as one line."""

    def test_a_file_filter_selects_exactly_that_file(self):
        self.assertEqual(_regression_playwright.pattern("/w/e2e/price.spec.ts"), r"/^\/w\/e2e\/price\.spec\.ts$/")
        self.assertEqual(_regression_playwright.pattern("/w/a (1)/[id].spec.ts"), r"/^\/w\/a \(1\)\/\[id\]\.spec\.ts$/")

    def test_an_expect_failure_keeps_what_it_expected_and_received(self):
        message = ('Error: \x1b[2mexpect(\x1b[22mlocator\x1b[2m).\x1b[22mtoHaveText\x1b[2m(\x1b[22mexpected\x1b[2m)\x1b[22m failed\n\n'
                   "Locator:  locator('#total')\nExpected: \x1b[32m\"180\"\x1b[39m\nReceived: \x1b[31m\"200\"\x1b[39m\nTimeout:  3000ms\n")
        self.assertEqual(_regression_playwright.described(message),
                         'Error: expect(locator).toHaveText(expected) failed Expected: "180" Received: "200"')

    def test_a_missing_export_through_babel_is_missing(self):
        # Playwright runs a TypeScript spec in a CommonJS package through Babel, which calls an import as `(0 , _module.name)`.
        self.assertEqual(missing("TypeError: (0 , _price.roundUp) is not a function"), "roundUp")
        self.assertIsNone(missing("TypeError: (0 , _price.roundUp)(...).map is not a function"))


if __name__ == "__main__":
    unittest.main()
