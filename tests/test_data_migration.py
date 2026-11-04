"""Fast migration command tests; run with python -m unittest discover -s tests.

Requires PyYAML and Jinja2. PostgreSQL commands are boundary doubles here;
Molecule exercises the same command against disposable PostgreSQL servers.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]


def environment():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters.update(quote=shlex.quote, bool=bool, from_yaml=yaml.safe_load,
                       combine=lambda a, b: dict(a, **b), to_nice_yaml=yaml.safe_dump)
    return env


def migration_command(suffix=""):
    env = environment()
    tasks = yaml.safe_load((ROOT / "roles/installer/tasks/migrate_data.yml").read_text())
    dump = next(t["set_fact"]["pgdump"] for t in tasks if "pgdump" in t.get("set_fact", {}))
    pgdump = env.from_string(dump).render(managed_database=False, pg_dump_suffix=suffix)
    external = yaml.safe_load((ROOT / "roles/installer/tasks/migrate_data_external.yml").read_text())
    transfer = next(t for t in external if "kubernetes.core.k8s_exec" in t)
    script = env.from_string(transfer["vars"]["installer_external_migration_command"]).render(pgdump=pgdump)
    return env.from_string(transfer["kubernetes.core.k8s_exec"]["command"]).render(installer_external_migration_command=script)


class MigrationCommandTests(unittest.TestCase):
    def run_transfer(self, dump_rc=0, restore_rc=0, suffix=""):
        self.assertTrue((ROOT / "roles/installer/tasks/migrate_data_external.yml").exists(),
                        "External targets need a migration path without a managed PostgreSQL pod")
        # Capture the commands' actual argv and libpq environment, without a
        # Kubernetes connection or a database server in this fast test layer.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name, rc in (("pg_dump", dump_rc), ("pg_restore", restore_rc)):
                tool = path / name
                tool.write_text(
                    "#!/usr/bin/env python3\nimport json,os,sys\n"
                    "from pathlib import Path\n"
                    f"Path({str(path / (name + '.json'))!r}).write_text(json.dumps("
                    "{'args':sys.argv[1:], 'env':{k:v for k,v in os.environ.items() if k.startswith('PG')}}))\n"
                    + ("sys.stdout.write('archive')\n" if name == "pg_dump" else "sys.stdin.read()\n")
                    + f"sys.exit({rc})\n"
                )
                tool.chmod(0o755)
            env = dict(os.environ, PATH=str(path) + os.pathsep + os.environ["PATH"])
            for prefix, host, port, user, db in (
                ("SOURCE", "source", "5433", "old_user", "old_db"),
                ("TARGET", "target", "5544", "new_user", "new_db"),
            ):
                env.update({f"{prefix}_PG{k}": v for k, v in {
                    "HOST": host, "PORT": port, "USER": user, "DATABASE": db,
                    "PASSWORD": prefix + " spaces ' \" $() ; password",
                    "SSLMODE": "require" if prefix == "SOURCE" else "verify-full",
                    "TARGETSESSIONATTRS": "prefer-standby" if prefix == "SOURCE" else "read-write",
                }.items()})
            result = subprocess.run(shlex.split(migration_command(suffix)), env=env, capture_output=True, text=True, timeout=10)
            calls = {name: json.loads((path / (name + ".json")).read_text()) for name in ("pg_dump", "pg_restore")}
            return result, calls

    def test_routes_each_secret_and_preserves_dump_suffix(self):
        result, calls = self.run_transfer(suffix="--exclude-table-data 'main_job*'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls["pg_dump"]["args"], [
            "-h", "source", "-U", "old_user", "-d", "old_db", "-p", "5433",
            "-F", "custom", "--exclude-table-data", "main_job*",
        ])
        self.assertEqual(calls["pg_restore"]["args"], [
            "--clean", "--if-exists", "--no-owner", "--no-acl", "--exit-on-error",
            "-h", "target", "-p", "5544", "-U", "new_user", "-d", "new_db",
        ])
        self.assertEqual(calls["pg_dump"]["env"]["PGSSLMODE"], "require")
        self.assertEqual(calls["pg_restore"]["env"]["PGSSLMODE"], "verify-full")
        self.assertEqual(calls["pg_dump"]["env"]["PGTARGETSESSIONATTRS"], "prefer-standby")
        self.assertEqual(calls["pg_restore"]["env"]["PGTARGETSESSIONATTRS"], "read-write")
        self.assertEqual(calls["pg_dump"]["env"]["PGPASSWORD"], "SOURCE spaces ' \" $() ; password")
        self.assertEqual(calls["pg_restore"]["env"]["PGPASSWORD"], "TARGET spaces ' \" $() ; password")

    def test_dump_failure_fails_the_pipeline(self):
        result, _ = self.run_transfer(dump_rc=12)
        self.assertEqual(result.returncode, 12, result.stderr)

    def test_restore_failure_fails_the_pipeline(self):
        result, _ = self.run_transfer(restore_rc=13)
        self.assertEqual(result.returncode, 13, result.stderr)


class MigrationPodTests(unittest.TestCase):
    def pod(self, **overrides):
        env = environment()
        values = yaml.safe_load((ROOT / "roles/installer/defaults/main.yml").read_text())
        values.update(ansible_operator_meta={"name": "example", "namespace": "test"},
                      old_postgres_configuration_name="source-secret", __postgres_configuration_secret="target-secret",
                      _postgres_image="postgres:test", deployment_type="awx", bundle_ca_crt="")
        values.update(overrides)

        def lookup(kind, name):
            if kind == "env":
                return "test-version"
            template = ROOT / "roles/common/templates/labels" / Path(name).name
            return env.from_string(template.read_text()).render(values)

        env.globals["lookup"] = lookup
        template = ROOT / "roles/installer/templates/pods/data-migration.yaml.j2"
        return yaml.safe_load(env.from_string(template.read_text()).render(values))

    def test_connections_are_references_to_the_correct_secrets(self):
        pod = self.pod()
        self.assertEqual(pod["metadata"]["namespace"], "test")
        self.assertEqual(pod["metadata"]["labels"]["app.kubernetes.io/component"], "db-management")
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["image"], "postgres:test")
        for prefix, secret in (("SOURCE", "source-secret"), ("TARGET", "target-secret")):
            refs = {item["name"]: item["valueFrom"]["secretKeyRef"] for item in container["env"] if "valueFrom" in item}
            for suffix, key in (("PGHOST", "host"), ("PGPORT", "port"), ("PGUSER", "username"),
                                ("PGDATABASE", "database"), ("PGPASSWORD", "password"),
                                ("PGSSLMODE", "sslmode"), ("PGTARGETSESSIONATTRS", "target_session_attrs")):
                ref = refs[prefix + "_" + suffix]
                self.assertEqual((ref["name"], ref["key"]), (secret, key))
                self.assertEqual(ref.get("optional", False), key in ("sslmode", "target_session_attrs"))
        self.assertNotIn("volumes", pod["spec"])

    def test_pull_credentials_resources_and_scheduling_are_preserved(self):
        pod = self.pod(image_pull_secrets=["pull-one", "pull-two"], postgres_selector="disktype: fast",
                       postgres_tolerations="- key: database\n  operator: Exists",
                       postgres_resource_requirements={"requests": {"memory": "128Mi"}})
        self.assertEqual(pod["spec"]["imagePullSecrets"], [{"name": "pull-one"}, {"name": "pull-two"}])
        self.assertEqual(pod["spec"]["nodeSelector"], {"disktype": "fast"})
        self.assertEqual(pod["spec"]["tolerations"], [{"key": "database", "operator": "Exists"}])
        self.assertEqual(pod["spec"]["containers"][0]["resources"], {"requests": {"memory": "128Mi"}})

    def test_configured_ca_is_available_to_libpq_read_only(self):
        pod = self.pod(bundle_ca_crt="test-ca", bundle_cacert_secret="database-roots")
        container = pod["spec"]["containers"][0]
        self.assertIn({"name": "PGSSLROOTCERT", "value": "/etc/awx-migration-ca/bundle-ca.crt"}, container["env"])
        self.assertEqual(container["volumeMounts"], [{
            "name": "database-ca", "mountPath": "/etc/awx-migration-ca", "readOnly": True,
        }])
        self.assertEqual(pod["spec"]["volumes"][0]["secret"]["secretName"], "database-roots")

    def test_default_trust_bundle_is_available_without_a_custom_ca_secret(self):
        container = self.pod()["spec"]["containers"][0]
        self.assertIn({"name": "PGSSLROOTCERT", "value": "/etc/pki/tls/certs/ca-bundle.crt"}, container["env"])


if __name__ == "__main__":
    unittest.main()
