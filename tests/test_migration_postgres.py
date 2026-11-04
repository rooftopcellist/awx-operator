"""Run the production transfer against disposable PostgreSQL 15 containers.

MIGRATION_PODMAN_TESTS=1 python -m unittest discover -s tests
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
import uuid

from test_data_migration import migration_command


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=120)


@unittest.skipUnless(os.environ.get("MIGRATION_PODMAN_TESTS") == "1", "opt-in disposable Podman fixtures")
class PostgreSQLMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = "migration-test-" + uuid.uuid4().hex[:8]
        cls.source = cls.network + "-source"
        cls.target = cls.network + "-target"
        cls.image = "quay.io/sclorg/postgresql-15-c9s:latest"
        run("podman", "network", "create", cls.network)
        cls.addClassCleanup(lambda: run("podman", "network", "rm", cls.network, check=False))
        with tempfile.TemporaryDirectory() as directory:
            cert = Path(directory) / "server.crt"
            key = Path(directory) / "server.key"
            run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1",
                "-subj", "/CN=migration-test", "-addext",
                f"subjectAltName=DNS:{cls.source},DNS:{cls.target}")
            for name, port, user, db in ((cls.source, 5433, "old_user", "old_db"), (cls.target, 5544, "new_user", "new_db")):
                cls.addClassCleanup(lambda name=name: run("podman", "rm", "-f", name, check=False))
                run("podman", "run", "-d", "--name", name, "--network", cls.network,
                    "-e", "POSTGRESQL_USER=" + user, "-e", "POSTGRESQL_DATABASE=" + db,
                    "-e", "POSTGRESQL_PASSWORD=fixture-password", "-e", "TEST_CERT=" + cert.read_text(),
                    "-e", "TEST_KEY=" + key.read_text(), cls.image, "bash", "-c",
                    'umask 077; printf %s "$TEST_CERT" > /tmp/server.crt; '
                    'printf %s "$TEST_KEY" > /tmp/server.key; '
                    f'exec run-postgresql -p {port} -c ssl=on '
                    '-c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key')
                for _ in range(60):
                    ready = run("podman", "exec", name, "pg_isready", "-h", "127.0.0.1", "-p", str(port), check=False)
                    if ready.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("PostgreSQL fixture did not become ready: " + name)
        run("podman", "exec", cls.source, "psql", "-p", "5433", "-U", "old_user", "-d", "old_db", "-v", "ON_ERROR_STOP=1", "-c",
            "CREATE TABLE sentinel (value text); INSERT INTO sentinel VALUES ('copied');")

    def transfer(self, suffix="", **overrides):
        env = {"PGSSLROOTCERT": "/tmp/server.crt"}
        for prefix, host, port, user, db in (("SOURCE", self.source, "5433", "old_user", "old_db"),
                                             ("TARGET", self.target, "5544", "new_user", "new_db")):
            env.update({f"{prefix}_PG{k}": v for k, v in {
                "HOST": host, "PORT": port, "USER": user, "DATABASE": db,
                "PASSWORD": "fixture-password", "SSLMODE": "verify-full", "TARGETSESSIONATTRS": "read-write",
            }.items()})
        env.update(overrides)
        args = ["podman", "exec"]
        for key, value in env.items():
            args.extend(["-e", key + "=" + value])
        return run(*args, self.target, *shlex.split(migration_command(suffix)), check=False)

    def query(self, sql):
        return run("podman", "exec", self.target, "psql", "-p", "5544", "-U", "new_user", "-d", "new_db", "-Atc", sql).stdout.strip()

    def test_different_endpoints_restore_data_owned_by_target_user(self):
        result = self.transfer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.query("SELECT value || ':' || tableowner FROM sentinel, pg_tables WHERE tablename='sentinel'"), "copied:new_user")

    def test_source_authentication_failure_then_corrected_credentials(self):
        self.assertNotEqual(self.transfer(SOURCE_PGPASSWORD="wrong").returncode, 0)
        self.assertEqual(self.transfer().returncode, 0)
        self.assertEqual(self.query("SELECT value FROM sentinel"), "copied")

    def test_target_authentication_failure_then_corrected_credentials(self):
        self.assertNotEqual(self.transfer(TARGET_PGPASSWORD="wrong").returncode, 0)
        self.assertEqual(self.transfer().returncode, 0)

    def test_restore_replaces_partial_target_data(self):
        self.assertEqual(self.transfer().returncode, 0)
        self.query("INSERT INTO sentinel VALUES ('partial')")
        self.assertEqual(self.transfer().returncode, 0)
        self.assertEqual(self.query("SELECT count(*) FROM sentinel"), "1")

    def test_configured_dump_suffix_excludes_data(self):
        result = self.transfer(suffix="--exclude-table-data 'sentinel'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.query("SELECT count(*) FROM sentinel"), "0")

    def test_certificate_verification_rejects_missing_trust(self):
        result = self.transfer(PGSSLROOTCERT="/tmp/missing-root.crt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root certificate file", result.stderr)

    def test_target_session_attributes_are_enforced(self):
        result = self.transfer(TARGET_PGTARGETSESSIONATTRS="read-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not read-only", result.stderr)


if __name__ == "__main__":
    unittest.main()
