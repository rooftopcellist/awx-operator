# Migrating data from an old AWX instance

To migrate data from an older AWX installation, you must provide some information via Secrets.

## Creating Secrets for Migration

### Secret Key

You can find your old secret key in the inventory file you used to deploy AWX in releases prior to version 18.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: <resourcename>-secret-key
  namespace: <target-namespace>
stringData:
  secret_key: <old-secret-key>
type: Opaque
```

!!! note
    `<resourcename>` must match the `name` of the AWX object you are creating. In our example below, it is `awx`.

### Old Database Credentials

The secret should be formatted as follows:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: <resourcename>-old-postgres-configuration
  namespace: <target namespace>
stringData:
  host: <external ip or url resolvable by the cluster>
  port: "<external port, this usually defaults to 5432>"    # quotes are required
  database: <desired database name>
  username: <username to connect as>
  password: <password to connect with>
  sslmode: prefer  # optional; use verify-full when verifying server certificates
  target_session_attrs: any  # optional
type: Opaque
```

!!! note
    For `host`, a URL resolvable by the cluster could look something like `postgresql.<namespace>.svc.<cluster domain>`, where `<namespace>` is filled in with the namespace of the AWX deployment you are migrating data from, and `<cluster domain>` is filled in with the internal kubernetes cluster domain (In most cases it's `cluster.local`).

If your AWX deployment is already using an external database server or its database is otherwise not managed by the AWX deployment, you can instead create the same secret as above but omit the `-old-` from the `name`.
In the next section pass it in through `postgres_configuration_secret` instead, omitting the `_old_` from the key and ensuring the value matches the name of the secret. This will make AWX pick up on the existing database and apply any pending migrations.
It is strongly recommended to backup your database beforehand.

For a managed target, the operator streams the old database into the new managed PostgreSQL pod. If the target pod has custom labels, use `postgres_label_selector` to locate it.

### External Target Database

To copy data into a separate external database, provide both the old database Secret above and a target Secret:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: awx-target-postgres-configuration
  namespace: <target-namespace>
stringData:
  type: unmanaged
  host: <target-database-host>
  port: "5432"
  database: <target-database-name>
  username: <target-application-login>
  password: <target-password>
  sslmode: verify-full
  target_session_attrs: read-write
type: Opaque
```

Create the target database before migration. Its application login must be able to connect, create schema objects, and own the restored objects. Use a dedicated target database: migration replaces matching objects with `pg_restore --clean --if-exists --no-owner --no-acl --exit-on-error`. Source ownership and grants are not restored, and the operator does not grant or revoke the `postgres` role on an external server. Database extensions that require administrator privileges must already be provisioned. Source and target usernames and database names may differ.

Configure the AWX resource with both Secrets and the original application secret key:

```yaml
apiVersion: awx.ansible.com/v1beta1
kind: AWX
metadata:
  name: awx
spec:
  old_postgres_configuration_secret: awx-old-postgres-configuration
  postgres_configuration_secret: awx-target-postgres-configuration
  secret_key_secret: awx-secret-key
  # For certificate verification with a private CA:
  bundle_cacert_secret: database-ca-bundle
```

For a private CA, create `database-ca-bundle` in the AWX namespace with a `bundle-ca.crt` key containing the CA certificates needed for both databases. The temporary client pod mounts this bundle read-only and sets libpq's `PGSSLROOTCERT`. Without a custom bundle, it uses `ca_trust_bundle` from the client image. Each database connection uses the `sslmode` and optional `target_session_attrs` from its own Secret. Omitted values default to `prefer` and `any`, respectively. See the [libpq environment variable documentation](https://www.postgresql.org/docs/15/libpq-envars.html).

The operator creates `<resourcename>-data-migration` in the AWX namespace using the resolved PostgreSQL image. This pod needs no PVC and is removed after success or failure. It inherits PostgreSQL resource requests, node selection, tolerations, priority class, and image pull settings. Allow up to five minutes for the pod to become ready.

### Migration Prerequisites

- Quiesce the source application before starting. The operator scales down the target deployment, but cannot stop applications writing to an external source.
- Retain the original AWX secret key; changing it prevents AWX from decrypting migrated credentials.
- Ensure DNS resolution, database authentication, and network connectivity from the migration pod to **both** database hosts and ports. Namespace NetworkPolicies, database ingress rules, and external firewalls must already permit both connections. The operator's existing db-management egress policy allows the configured external **target** port; it does not automatically permit a different source port. A managed target's existing policy also restricts database pod egress. Resolve any blocked endpoint as an environment prerequisite.
- Use a PostgreSQL client image compatible with both servers. The operator uses the same image resolution as the managed database (`postgres_image`/`postgres_image_version`, related image, then default). The upstream default supplies PostgreSQL 15 clients. `pg_dump` cannot dump a server newer than its own major version, and loading into an older server is not guaranteed. Prefer clients matching the target major version with a source no newer than the target. Custom images must supply compatible `pg_dump`, `pg_restore`, Bash, and `sleep`. See the [PostgreSQL pg_dump documentation](https://www.postgresql.org/docs/15/app-pgdump.html).
- Source and target literal host/port/database tuples must differ. Aliases for the same database cannot be detected; ensure these are actually separate databases.

### Completion and Retries

After transfer succeeds, the operator immediately records the source Secret name in `status.migratedFromSecret`, before continuing installation. The status write has five attempts separated by five seconds. Once recorded, subsequent reconciliations skip the copy, including when a later application installation step failed. The final status updater also preserves this completion field.

If a dump or restore fails, reconciliation fails, the temporary pod is removed, and completion remains unset. Correct the credentials or other prerequisite and reconcile again. Failed transfers can leave partial target data; retrying replaces matching objects. A process failure between successful restore and durable status persistence can still cause another copy. Keep the target dedicated to migration until completion is recorded.

## Deploy AWX

When you apply your AWX object, you must specify the name to the database secret you created above:

```yaml
apiVersion: awx.ansible.com/v1beta1
kind: AWX
metadata:
  name: awx
spec:
  old_postgres_configuration_secret: <resourcename>-old-postgres-configuration
  secret_key_secret: <resourcename>-secret-key
  ...
```
### Exclude postgreSQL tables during migration (optional)

Use the `pg_dump_suffix` parameter under `AWX.spec` to customize the pg_dump command that will execute during migration. This variable will append your provided pg_dump parameters to the end of the 'standard' command. For example, to exclude the data from 'main_jobevent' and 'main_job' to decrease the size of the backup use:

```
pg_dump_suffix: "--exclude-table-data 'main_jobevent*' --exclude-table-data 'main_job'"
```

## Important Note

If you intend to put all the above in one file, make sure to separate each block with three dashes like so:

```yaml
---
# Secret key

---
# Database creds

---
# AWX Config
```

Failing to do so will lead to an inoperable setup.
