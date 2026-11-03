# Migration task regression tests

These tests execute the installer migration tasks, Job templates, and status
updates with a simulated Kubernetes API boundary. They require Python 3.10+,
Ansible Core 2.16, and PyYAML. No cluster or Kubernetes credentials are used.

```sh
python3 -m venv /tmp/awx-migration-tests
/tmp/awx-migration-tests/bin/pip install 'ansible-core==2.16.14' PyYAML
PATH=/tmp/awx-migration-tests/bin:$PATH /tmp/awx-migration-tests/bin/python \
  -m unittest discover -s tests/migration -v
```

The harness isolates the migration entrypoint from cluster discovery and replaces
the Kubernetes collection actions. Watches complete deterministically; the
migration watch's retry count is reduced in the temporary task copy so failure
tests exercise the exhausted-watch path without a 15-minute wait.

Coverage includes same-image database rollback without a Pod restart, healthy
rollouts preserving migration history, target-image probe fallback, probe and
migration failures blocking status advancement, active/retrying and terminally
failed Jobs, prior unlabeled Jobs, and unrelated-instance isolation.

These checks do not replace live reconciliation, `pg_restore`, or AWXRestore
validation. Those scenarios still need a disposable cluster.
