Run the command and pod-template tests with Python, PyYAML, and Jinja2:

```sh
python -m unittest discover -s tests -v
```

To also run transfers against disposable PostgreSQL servers, install Podman
and OpenSSL, then run:

```sh
MIGRATION_PODMAN_TESTS=1 python -m unittest discover -s tests -v
```

These tests create a private Podman network and two PostgreSQL 15 containers,
then remove them. They exercise the production transfer command, real
authentication, source/target database selection and object ownership,
partial-data replacement, dump options, TLS certificate verification, and
target session attributes. The fast command tests additionally verify distinct
source and target passwords and pipeline exit codes.

Kubernetes lifecycle, durable CR status, managed migration, application startup,
and encrypted credential usability are tested by Molecule:

```sh
molecule test -s kind -- --skip-tags=replicas
```

The migration verification tasks carry the `migration` tag. They create
disposable database fixtures in `<test-namespace>-migration` and an AWX resource
named `migration-app` in the operator's watched test namespace. Use a dedicated
test environment. Namespace-wide RBAC for the test identity is required to
create and remove the fixture namespace; the operator itself keeps its existing
namespace scope. On a cluster enforcing NetworkPolicies, both database fixture
endpoints must already be reachable under the existing policies. A blocked
endpoint is an environment prerequisite; these tests do not change policies.
