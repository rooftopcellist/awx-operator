"""Run installer migration tasks against a deterministic Kubernetes boundary.

Requires ansible-playbook and PyYAML. Run: python3 -m unittest discover -s tests/migration -v
These tests execute the production tasks and templates; they do not deploy a cluster.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "quay.io/ansible/awx:target"
HASH = hashlib.sha256(json.dumps(["awx", IMAGE]).encode()).hexdigest()[:10]
BASE = "awx-migration-" + HASH
LABELS = {"app.kubernetes.io/part-of": "awx", "awx.ansible.com/migration-image": HASH}


def job(name=BASE, status=None, labels=None):
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "uid": "original-" + name,
                     "creationTimestamp": "2026-11-01T00:00:00Z", "labels": labels or LABELS},
        "spec": {"template": {"spec": {"containers": [{"name": "migration-job", "image": IMAGE}]}}},
        "status": status if status is not None else {"succeeded": 1},
    }


def pod(image=IMAGE):
    return {"metadata": {"name": "web-pod"},
            "spec": {"containers": [{"name": "awx-web", "image": image}]},
            "status": {"phase": "Running", "containerStatuses": [
                {"name": "awx-web", "state": {"running": {}}}]}}


class MigrationTests(unittest.TestCase):
    def run_flow(self, jobs=None, pods=None, plan="[X]  main.0001_initial\n", probe_error=False, migration_error=False):
        state = {"jobs": {j["metadata"]["name"]: j for j in (jobs if jobs is not None else [job()])},
                 "pods": [pod()] if pods is None else pods, "plan": plan, "probe_error": probe_error,
                 "created": [], "deleted": [], "execs": [], "waited": [], "status": {},
                 "migration_error": migration_error}
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            # Isolate this task entrypoint from the role's cluster-discovery dependency.
            shutil.copytree(ROOT / "roles/installer", temp / "roles/installer", ignore=shutil.ignore_patterns("meta"))
            (temp / "roles/installer/meta").mkdir()
            (temp / "roles/installer/meta/main.yml").write_text("collections:\n  - kubernetes.core\n")
            # Exercise exhausted-watch behavior without waiting 15 minutes.
            tasks_path = temp / "roles/installer/tasks/migrate_schema.yml"
            tasks = yaml.safe_load(tasks_path.read_text())
            for task in tasks:
                if "until" in task:
                    task.update(retries=0, delay=0)
            tasks_path.write_text(yaml.safe_dump(tasks))
            (temp / "roles/common").symlink_to(ROOT / "roles/common", target_is_directory=True)
            state_path = temp / "state.json"
            state_path.write_text(json.dumps(state))
            actions = temp / "collections/ansible_collections/kubernetes/core/plugins/action"
            actions.mkdir(parents=True)
            for name in ("k8s", "k8s_info", "k8s_exec", "k8s_log"):
                shutil.copy(Path(__file__).parent / "action_plugins/cluster.py", actions / (name + ".py"))
            status_action = temp / "collections/ansible_collections/operator_sdk/util/plugins/action"
            status_action.mkdir(parents=True)
            shutil.copy(Path(__file__).parent / "action_plugins/cluster.py", status_action / "k8s_status.py")
            # Short module names in the parent fix use the same Kubernetes boundary.
            play = [{"hosts": "localhost", "gather_facts": False,
                     "collections": ["kubernetes.core"],
                     "vars": {"ansible_operator_meta": {"name": "awx", "namespace": "test"},
                              "_image": IMAGE, "awx_web_pod_name": "web-pod",
                              "_custom_image": IMAGE,
                              "awx_web_pod": state["pods"][0] if state["pods"] else {},
                              "secret_key_secret_name": "awx-secret", "bundle_ca_crt": "",
                              "_init_container_image": "init", "additional_labels_items": [],
                              "init_container_resource_requirements": {},
                              "__admin_password_secret": {"resources": [{"metadata": {"name": "admin"}}]},
                              "pg_config": {"resources": [{"metadata": {"name": "postgres"}}]},
                              "__broadcast_websocket_secret": {"resources": [{"metadata": {"name": "websocket"}}]},
                              "_image_version": "test"},
                     "tasks": [{"ansible.builtin.include_role": {"name": "installer", "tasks_from": "migrate_schema"}},
                               {"ansible.builtin.assert": {"that": ["migration_confirmed | bool"]}},
                               {"ansible.builtin.include_role": {"name": "installer", "tasks_from": "update_status"}}]}]
            play_path = temp / "play.yml"
            play_path.write_text(yaml.safe_dump(play))
            env = dict(os.environ, ANSIBLE_ROLES_PATH=str(temp / "roles"),
                       ANSIBLE_COLLECTIONS_PATH=str(temp / "collections"),
                       ANSIBLE_LOCAL_TEMP=str(temp / "ansible"),
                       ANSIBLE_NOCOLOR="1", MIGRATION_TEST_STATE=str(state_path))
            result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(play_path)],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
            self.output = result.stdout + result.stderr
            return result.returncode, json.loads(state_path.read_text())

    def assert_success(self, rc):
        self.assertEqual(rc, 0, self.output)

    def test_healthy_rollout_retains_success_without_new_job(self):
        rc, state = self.run_flow()
        self.assert_success(rc)
        self.assertEqual(len(state["execs"]), 1)
        self.assertEqual(state["execs"][0]["command"], "awx-manage showmigrations --plan --no-color")
        self.assertEqual(state["created"], [])
        self.assertEqual(state["jobs"][BASE]["metadata"]["uid"], "original-" + BASE)
        self.assertEqual(state["status"]["image"], IMAGE)
        self.assertEqual(state["status"]["version"], "4.8.12")

    def test_rollback_without_pod_restart_creates_attempt_and_keeps_history(self):
        rc, state = self.run_flow(plan="[X]  main.0001_initial\n[ ]  main.0002_next\n")
        self.assert_success(rc)
        self.assertEqual(len(state["created"]), 1)
        attempt = state["jobs"][state["created"][0]]
        self.assertEqual(attempt["metadata"]["labels"]["awx.ansible.com/migration-image"], HASH)
        self.assertNotEqual(attempt["metadata"]["name"], BASE)
        self.assertLessEqual(len(attempt["metadata"]["name"]), 63)
        self.assertEqual(state["jobs"][BASE]["metadata"]["uid"], "original-" + BASE)
        self.assertEqual(state["deleted"], [])

    def test_old_web_image_uses_target_image_probe_job(self):
        rc, state = self.run_flow(pods=[pod("quay.io/ansible/awx:old")])
        self.assert_success(rc)
        self.assertEqual(state["execs"], [])
        self.assertEqual(len(state["created"]), 1)
        self.assertEqual(state["deleted"], state["created"])
        self.assertIn(BASE, state["jobs"])

    def test_pending_target_image_probe_starts_migration(self):
        rc, state = self.run_flow(pods=[], plan="[ ]  main.0002_next\n")
        self.assert_success(rc)
        self.assertEqual(len(state["created"]), 2)
        self.assertIn(BASE, state["jobs"])

    def test_probe_failure_stops_before_confirmation(self):
        rc, state = self.run_flow(probe_error=True)
        self.assertNotEqual(rc, 0, self.output)
        self.assertEqual(state["created"], [])
        self.assertEqual(state["deleted"], [])
        self.assertEqual(state["status"], {})
        self.assertIn("database unavailable", self.output)

    def test_check_job_failure_stops_before_confirmation(self):
        rc, state = self.run_flow(pods=[], probe_error=True)
        self.assertNotEqual(rc, 0, self.output)
        self.assertEqual(len(state["created"]), 1)
        self.assertIn(BASE, state["jobs"])
        self.assertEqual(state["status"], {})
        self.assertIn("Migration check failed", self.output)

    def test_empty_or_unrecognized_probe_output_is_not_success(self):
        for plan in ("", "unexpected output\n"):
            with self.subTest(plan=plan):
                rc, state = self.run_flow(plan=plan)
                self.assertNotEqual(rc, 0, self.output)
                self.assertEqual(state["created"], [])
                self.assertEqual(state["status"], {})
                self.assertIn("no recognizable plan", self.output)

    def test_migration_watch_failure_does_not_advance_image_or_version(self):
        rc, state = self.run_flow(jobs=[], migration_error=True)
        self.assertNotEqual(rc, 0, self.output)
        self.assertEqual(state["status"], {})
        self.assertIn("Refusing to continue Django initialization", self.output)

    def test_active_attempt_takes_priority_over_historical_success(self):
        attempt = job(BASE + "-retry", {"active": 1})
        rc, state = self.run_flow(jobs=[job(), attempt])
        self.assert_success(rc)
        self.assertEqual(state["waited"], [attempt["metadata"]["name"]])
        self.assertEqual(state["execs"], [])
        self.assertEqual(state["created"], [])

    def test_failed_pod_with_job_retry_pending_is_preserved(self):
        for status in ({"active": 1}, {"failed": 1}, {}):
            with self.subTest(status=status):
                rc, state = self.run_flow(jobs=[job(status=status)])
                self.assert_success(rc)
                self.assertEqual(state["deleted"], [])
                self.assertEqual(state["waited"], [BASE])
                self.assertEqual(state["execs"], [])
                self.assertEqual(state["created"], [])
                self.assertEqual(state["jobs"][BASE]["metadata"]["uid"], "original-" + BASE)

    def test_terminal_failure_starts_new_attempt_and_preserves_failed_logs(self):
        failed = job(status={"failed": 6, "conditions": [{"type": "Failed", "status": "True"}]})
        rc, state = self.run_flow(jobs=[failed])
        self.assert_success(rc)
        self.assertEqual(len(state["created"]), 1)
        self.assertEqual(state["deleted"], [])
        self.assertEqual(state["jobs"][BASE]["metadata"]["uid"], "original-" + BASE)

    def test_new_image_migrates_directly_without_old_pod_probe(self):
        rc, state = self.run_flow(jobs=[], pods=[pod("old")])
        self.assert_success(rc)
        self.assertEqual(state["execs"], [])
        self.assertEqual(state["created"], [BASE])

    def test_unrelated_instance_attempt_is_ignored(self):
        other = job("other-attempt", {"active": 1}, dict(LABELS, **{"app.kubernetes.io/part-of": "other"}))
        rc, state = self.run_flow(jobs=[job(), other])
        self.assert_success(rc)
        self.assertEqual(len(state["execs"]), 1)
        self.assertEqual(state["jobs"]["other-attempt"], other)

    def test_successful_recovery_is_probed_on_later_reconcile(self):
        rc, state = self.run_flow(jobs=[job(), job(BASE + "-retry")])
        self.assert_success(rc)
        self.assertEqual(len(state["execs"]), 1)
        self.assertEqual(state["created"], [])


if __name__ == "__main__":
    unittest.main()
