# MySQL Anonymized Copy

A local CLI to copy MySQL table definitions and data into a separate, empty schema while anonymizing explicitly configured columns. Primary and foreign keys are retained. Authentication fields are preserved so copied accounts retain their existing passwords.

## Setup and quick start

Requirements: Python 3.11+, MySQL access, and an empty destination schema. Use a read-only account for the source and a destination user limited to the destination schema.

### Create a read-only source user

Log in to MySQL with an administrator account. Replace `source_database` and the password with your actual database name and a strong password:

```bash
# Open the MySQL client; it prompts for the administrator password
mysql -u root -p
```

Then run these SQL statements in the MySQL prompt:

```sql
-- Create a local account for the anonymizer to read the source
CREATE USER 'anonymizer_readonly'@'localhost' IDENTIFIED BY 'choose-a-strong-password';

-- Permit reading source tables and viewing view definitions
GRANT SELECT, SHOW VIEW ON `source_database`.* TO 'anonymizer_readonly'@'localhost';

-- Optional: needed only if allow_source_table_locks is enabled for non-InnoDB tables
GRANT LOCK TABLES ON `source_database`.* TO 'anonymizer_readonly'@'localhost';

-- Confirm the account's privileges
SHOW GRANTS FOR 'anonymizer_readonly'@'localhost';
```

Use this username, source database name, and password in `config.json` and the `MYSQL_SOURCE_PASSWORD` environment variable. The account host (`localhost`) must match where the CLI connects from. If MySQL sees the connection from a different machine or over a different host, create the account with the appropriate client host instead. `mysqldump` also needs `SELECT`; depending on MySQL version and dump options it may require additional privileges such as `LOCK TABLES` or `TRIGGER`.

### Create a destination user

The destination schema must already exist and be empty. Log in as a MySQL administrator and create a separate account for writing the copy. Replace `development_database` and the password with your destination schema and a strong password:

```bash
# Open the MySQL client; it prompts for the administrator password
mysql -u root -p
```

Run the following SQL in the MySQL prompt:

```sql
-- Create a local account for the anonymizer to create and populate tables
CREATE USER 'anonymizer_writer'@'localhost' IDENTIFIED BY 'choose-another-strong-password';

-- Allow table creation, row insertion, indexes, and foreign key references
GRANT CREATE, INSERT, INDEX, REFERENCES ON `development_database`.* TO 'anonymizer_writer'@'localhost';

-- Confirm the account's privileges
SHOW GRANTS FOR 'anonymizer_writer'@'localhost';
```

Set `destination.user`, `destination.database`, and `MYSQL_DESTINATION_PASSWORD` in the CLI configuration/environment to match this account. The account host must match where the CLI connects from, just as for the source user. If the account already exists but its password is wrong, update it as an administrator with `ALTER USER 'anonymizer_writer'@'localhost' IDENTIFIED BY 'choose-another-strong-password';`. If the schema name, username, host, and password all match but access is denied, check the server's account host matching and grants with `SHOW GRANTS`.

### Use a MariaDB server in Docker

The CLI can connect from the host to a MariaDB container through a published port, or from another container on the same Docker network. MariaDB is generally compatible with MySQL clients, but this project currently uses MySQL Connector/Python and has not been specifically verified against every MariaDB version. Start by running `inspect` to verify the connection and schema queries.

When running the CLI on the host, publish the container's MariaDB port on a host port. For example, a Compose service can include:

```yaml
services:
  mariadb:
    image: mariadb:11.4
    ports:
      - "127.0.0.1:3307:3306"
```

The server listens on port `3306` inside the container; the example publishes it as host port `3307`, bound to localhost. Open the MariaDB client inside the running container:

```bash
# Replace the container name with yours; MariaDB prompts for the root password
docker exec -it YOUR_MARIADB_CONTAINER mariadb -u root -p
```

In the MariaDB prompt, create a separate empty destination schema and the source/destination accounts. Replace database names and passwords with your own:

```sql
CREATE DATABASE `anonymized_copy`;

CREATE USER 'anonymizer_readonly'@'%' IDENTIFIED BY 'choose-a-source-password';
GRANT SELECT, SHOW VIEW ON `source_database`.* TO 'anonymizer_readonly'@'%';

CREATE USER 'anonymizer_writer'@'%' IDENTIFIED BY 'choose-a-destination-password';
GRANT CREATE, INSERT, INDEX, REFERENCES ON `anonymized_copy`.* TO 'anonymizer_writer'@'%';
```

If `allow_source_table_locks` is enabled for non-InnoDB tables, also grant:

```sql
GRANT LOCK TABLES ON `source_database`.* TO 'anonymizer_readonly'@'%';
```

For the host-running-CLI setup, set both connection `host` values in `config.json` to `127.0.0.1` and both `port` values to `3307`. Use the source and destination schema names and usernames above, then provide their passwords through the environment variables named by `password_env`. Keep the published port bound to `127.0.0.1` when only local access is needed.

If the CLI runs in another container, put both containers on the same Docker network and use the MariaDB Compose service name (for example, `mariadb`) as the host and `3306` as the port. In that case `localhost` refers to the CLI container itself, not the database container. The account host must also permit connections from the CLI container; `%` in these examples does that, so restrict network exposure to the intended Docker network.

With the project's virtual environment active, verify connectivity before copying:

```bash
mysql-anonymizer inspect --config config.json
```

If inspection fails because of connector incompatibility, installing MariaDB Connector/Python alone will not switch this application to that driver; the app currently imports MySQL Connector/Python and would need a connector adapter change.

Run these commands from the project directory:

```bash
# Create and activate an isolated Python environment
python3 -m venv .venv
source .venv/bin/activate

# Install the project and its MySQL connector dependency
python -m pip install --upgrade pip
python -m pip install -e .

# Create a local connection settings file; edit it before connecting
cp config.example.json config.json

# Ignore the virtual environment, caches, and local files containing settings/rules
cat > .gitignore <<'EOF'
.venv/
__pycache__/
*.py[cod]
*.egg-info/
build/
dist/
config.json
rules.json
EOF

# Initialize Git and commit the project files (credentials and local rules are excluded)
git init
git add README.md pyproject.toml requirements.txt config.example.json .gitignore mysql_anonymizer
git commit -m "Set up MySQL anonymized copy CLI"
```

Edit `config.json` with the source and destination host, schema, and user details. Set the password environment variables in the shell where you will run the CLI, then inspect the source and generate starter rules:

```bash
# Supply credentials without storing them in the config file
export MYSQL_SOURCE_PASSWORD='your-source-password'
export MYSQL_DESTINATION_PASSWORD='your-destination-password'

# Inspect the source schema and generate starter rules from its columns
mysql-anonymizer inspect --config config.json
mysql-anonymizer init-config --config config.json --output rules.json
```

Review `rules.json` carefully. List sensitive data columns for anonymization and preserve primary/foreign keys, login names, email addresses used for login, password hashes, and other authentication fields. The starter rules use column-name guesses and cannot identify every sensitive field. When `copy` finds a primary key, `id`, or `*_id` column under `anonymize`, it automatically moves that column to `preserve` and saves the repaired rules file before copying. MySQL `SET` values are converted from the connector's Python set representation to MySQL's comma-separated form when inserted. For unique text fields such as `accommtype.name`, the generated `name` transformation is supported and collision handling keeps transformed values distinct. You can also set `{"type": "unique"}` to create opaque pseudonyms. Composite unique indexes need application-specific review. When satisfied that the copied password hashes should remain usable, set `"confirm_copy_password_hashes": true` in `rules.json`, then run:

```bash
# Copy into the separate, empty destination schema
mysql-anonymizer copy --config config.json --rules rules.json
```

To enable exact lookups between original and anonymized values, pass a local SQLite mapping store during the copy. The file contains original sensitive values, so keep it private and include it only in a suitably protected backup:

```bash
mysql-anonymizer copy --config config.json --rules rules.json --mapping-store private-mappings.sqlite3
```

Search in either direction by table and column:

```bash
mysql-anonymizer lookup --mapping-store private-mappings.sqlite3 --table customers --column phone --value '+1 555 0100' --direction original-to-anonymized
mysql-anonymizer lookup --mapping-store private-mappings.sqlite3 --table customers --column phone --value '+15551234567' --direction anonymized-to-original
```

The lookup is exact and returns every matching value (an anonymized value can correspond to more than one original value for non-unique transformations). It indexes only configured anonymized columns; preserved columns are not recorded.

The source should be accessed through a read-only account; the destination user should only have access to the destination schema. Destination must be empty. The tool never drops or truncates objects. It currently copies base tables and rows, not views, triggers, procedures, events, or grants. Review app-specific columns, free text, and JSON manually. Protect the resulting database like production credentials.

### Recover after a failed copy

A failed copy may leave some or all tables in the destination. MySQL commits table creation separately, so rolling back inserted rows does not necessarily remove the created tables. The next run requires an empty destination and will stop if those tables remain.

The safest recovery is to create a new, empty destination schema and change `destination.database` in `config.json` to that schema. Grant the destination user access to the new schema as described above.

If you want to reuse the previous destination, first verify the exact schema name in `config.json`. The following commands permanently delete that schema and all its contents; run them only if it is the disposable anonymized copy, never the source database:

```sql
-- Replace this with the exact destination.database value from config.json
DROP DATABASE `development_database`;
CREATE DATABASE `development_database`;
```

If the destination user loses access after recreating the schema, reapply its `GRANT CREATE, INSERT, INDEX, REFERENCES` statement. Then check that the destination is empty before retrying:

```sql
SELECT TABLE_NAME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'development_database';
```

Proceed only when the query returns no rows.

### If the source has non-InnoDB tables

By default, the copy command requires all source tables to use InnoDB so it can read them from one consistent snapshot. First, find tables using another engine:

```sql
-- Run in the MySQL client; replace source_database with the source schema
SELECT TABLE_NAME, ENGINE
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'source_database'
  AND TABLE_TYPE = 'BASE TABLE'
  AND ENGINE <> 'InnoDB';
```

To copy without changing table engines, enable `"allow_source_table_locks": true` in `config.json` and grant `LOCK TABLES` on the source schema as shown above. The tool then holds read locks on every source table until the copy finishes. This prevents writes and may block application requests that write to those tables; use it only during an approved quiet window and monitor the copy time. If another session holds an incompatible lock or transaction, acquiring the locks can wait or fail. The CLI prints a warning when this option is enabled.

For a production database where blocking writes is not acceptable, take a backup and restore it to a separate staging schema/server, convert the non-InnoDB tables in staging, and point the anonymizer's `source.database` at that staging schema. Converting an engine can lock or rebuild a table and may affect disk space or table-specific behavior, so review the application and maintenance impact first.

On the staging database, a database administrator can convert each listed table with:

```sql
-- Example only: replace the schema and table with a result from the query above
ALTER TABLE `staging_database`.`legacy_table` ENGINE=InnoDB;
```

Re-run the engine query and proceed only when it returns no rows. Keep the source account read-only; perform conversions using a separate administrator account.
