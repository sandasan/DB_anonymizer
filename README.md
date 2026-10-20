# MySQL Anonymized Copy

A local CLI to copy MySQL table definitions and data into a separate schema while anonymizing explicitly configured columns. You can copy selected tables in multiple runs; tables with the same names already in the destination are rejected. Primary and foreign keys are retained. Authentication fields are preserved so copied accounts retain their existing passwords.

## Setup and quick start

Requirements: Python 3.11+ and MySQL access. Use a read-only account for the source and a destination user limited to the destination schema.

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

-- Allow table creation, row insertion, indexes, foreign keys, and clearing a partial table on resume
GRANT CREATE, INSERT, DELETE, INDEX, REFERENCES ON `development_database`.* TO 'anonymizer_writer'@'localhost';

-- Confirm the account's privileges
SHOW GRANTS FOR 'anonymizer_writer'@'localhost';
```

Set `destination.user`, `destination.database`, and `MYSQL_DESTINATION_PASSWORD` in the CLI configuration/environment to match this account. The account host must match where the CLI connects from, just as for the source user. If the account already exists but its password is wrong, update it as an administrator with `ALTER USER 'anonymizer_writer'@'localhost' IDENTIFIED BY 'choose-another-strong-password';`. If the schema name, username, host, and password all match but access is denied, check the server's account host matching and grants with `SHOW GRANTS`.

In the GUI, **Recreate selected tables** drops the selected destination tables and recopies them; this requires `DROP` on those tables. **Clear destination data** drops every base table in the configured destination schema and resets the saved copy checkpoint and copied/recreated marks; it requires `DROP` on those tables. The database itself is kept. **Recreate destination DB** drops and recreates the configured destination schema, then copies all source tables; this requires database-level `DROP` and `CREATE` privileges. The normal writer account above intentionally lacks these privileges. Use a dedicated maintenance account for these actions, and never grant them on the source database.

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
GRANT CREATE, INSERT, DELETE, INDEX, REFERENCES ON `anonymized_copy`.* TO 'anonymizer_writer'@'%';
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

### Graphical interface

From the project directory, activate the virtual environment and launch the desktop interface:

```bash
source .venv/bin/activate
mysql-anonymizer-gui
```

If you have not installed the project in that environment yet, run `python -m pip install -e .` first. You can also start the window directly with `python -m mysql_anonymizer.gui`.

On Linux, you can also double-click `mysql-anonymizer.desktop` in the project folder. If the desktop asks, choose **Allow Launching** or **Trust and Launch**. This launcher expects the project at `/home/alexander/projects/DB_anonymizer`; if you move the folder, update the `Exec` and `Path` entries in the `.desktop` file. The `launch-gui.sh` script can also be run directly from the file manager after marking it as executable.

The window lets you load connection settings and rules, inspect the source, select tables, include foreign-key dependencies, edit or generate anonymization rules, start the copy, browse source or destination table data, and search mapping values in either direction. During a copy, **Overall copy progress** tracks completion across all selected tables, including progress through the current table, and **Current table** tracks rows copied in that table. Use **Pause copy** to save a checkpoint at the next batch boundary. Closing the window during a copy also pauses it safely; the next launch offers to resume. A resumed copy keeps completed tables and clears and recopies the interrupted table. Resuming starts a new source snapshot, so changes made to source data while the app is closed may be included. The checkpoint is stored in the app's user configuration directory with owner-only permissions and includes connection settings, including any password entered in the GUI. The status line shows elapsed time and an estimated time remaining, then the total copy duration when finished. The **Anonymization rules** tab includes a searchable column navigator with table and rule-status filters; double-click a row to jump to its rule in the JSON editor. It shows unconfigured columns and rules that refer to missing source fields. In the **Tables** tab, when a selected source table already exists in the destination, the GUI offers to mark it as copied; accepted tables get a green check and are skipped by the normal copy action. Use **Load config…** and **Load rules…** to open existing JSON files; use **Save config as…** and **Save rules as…** to write the current settings or rules to new files. The **Save config** and **Save rules** actions update the selected files. The GUI remembers the last loaded config and rules paths and opens those files at startup when they still exist. The **Browse data** tab can search across all columns or filter within one selected column, and displays up to 200 matching rows. Use **Test connections** to check both database accounts before copying. If MySQL rejects authentication, the GUI identifies the source or destination account and suggests checking its username, host, password, and grants. MySQL error 1045 cannot distinguish a missing account from a wrong password or a host mismatch. Passwords entered in the window are saved in the config file in plain text and are loaded back into the password fields. Leave a password field blank to use the configured `password_env` variable. If a selected table's rules refer to columns missing from the inspected source, the GUI asks before ignoring those stale entries for that copy; columns without a rule are copied unchanged. Existing destination tables are allowed as long as their names do not collide with the tables being copied; referenced tables may already exist in the destination.

Use **Save profile…** and **Load profile** to keep named bundles of connection settings, rules, mapping-store paths, and table-copy options for different databases. Profiles are stored under the app's user configuration directory with owner-only permissions. A profile stores the mapping-store path, not a copy of the mapping database. Loading a profile updates the current window; config and rules files are only changed when you explicitly save them.

The **Auto-confirm copy/table actions** option skips the regular copy, selected-table recreation, missing-mapping-store, and stale-rule confirmation prompts. It is saved in the app's local state. Recreating the entire destination database still requires typing its name, resuming a saved copy still prompts at startup, and discarding an interrupted-copy checkpoint still requires confirmation.

The GUI writes timestamped diagnostics to `gui.log` beside its state file in the app configuration directory; the full log path appears in the output panel. The log rotates after 2 MB and keeps three backups. It records operation stages and errors, but excludes the output panel's lookup results and masks common row-value details.

For GUI lookups, choose or create a mapping store at the top of the window and copy data with that path selected. If no mapping store is selected, the GUI warns that the copy will not save mappings and asks whether to continue unless auto-confirm is enabled. Open the **Lookup** tab and use **Load fields** to populate the table and column dropdowns from the store. Choose a table and column, enter the exact value, then choose original-to-anonymized or anonymized-to-original. The search uses the mapping store and may return multiple matches. The dropdowns refresh after a copy when a mapping store is selected.

Run these commands from the project directory:

```bash
# Create and activate an isolated Python environment
python3 -m venv .venv
source .venv/bin/activate

# Install the project and its MySQL connector dependency
python -m pip install --upgrade pip
python -m pip install -e .

# Create a local connection settings file; edit it before connecting
cp examples/config.example.json config.json

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
git add README.md pyproject.toml requirements.txt examples/config.example.json .gitignore src/mysql_anonymizer
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

Review `rules.json` carefully. Generated rules use inspected column types, comments, primary/foreign-key metadata, and conservative column-name patterns. They preserve identifiers and authentication fields, anonymize clearly named email, phone, name, address, and IP-address fields, and shift date-of-birth fields. IP addresses use a deterministic replacement in a private IPv4 range or the IPv6 documentation range; invalid IP values stop the copy with a column-specific error. Email columns in user/account/auth tables are preserved because they may be login identifiers. Unrecognized and ambiguous columns are left unchanged, so review every table for sensitive fields the generator could not identify. You can also choose a transformation explicitly for a field in `rules.json`, for example `"ip_address": {"type": "ip_address"}` under that table's `anonymize` object, or add the column to `preserve` to keep it unchanged. When `copy` finds a primary key, `id`, or `*_id` column under `anonymize`, it automatically moves that column to `preserve` and saves the repaired rules file before copying. MySQL `SET` values are converted from the connector's Python set representation to MySQL's comma-separated form when inserted. For unique text fields such as `accommtype.name`, the generated `name` transformation is supported and collision handling keeps transformed values distinct. You can also set `{"type": "unique"}` to create opaque pseudonyms. Composite unique indexes need application-specific review. When satisfied that the copied password hashes should remain usable, set `"confirm_copy_password_hashes": true` in `rules.json`, then run:

```bash
# Copy into the separate destination schema
mysql-anonymizer copy --config config.json --rules rules.json
```

By default, `copy` includes every source table. To copy only selected tables, repeat `--table` for each one:

```bash
mysql-anonymizer copy --config config.json --rules rules.json --table customers --table orders
```

Every selected table's referenced tables in the source schema must also be selected so its foreign keys can be created in the destination. If a dependency is missing, or a selected table has a foreign key to another schema, the command reports it and stops before creating destination tables. Add `--include-dependencies` to include all same-schema references recursively:

```bash
mysql-anonymizer copy --config config.json --rules rules.json --table orders --include-dependencies
```

Tables that reference a selected table can be omitted. After a successful copy, the command lists copied tables with row counts and all source tables that were not copied.

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

A failed or paused copy may leave some or all tables and committed batches in the destination. If a GUI checkpoint exists, resume it to keep completed tables and recopy the interrupted table. Without a checkpoint, use a new empty destination or follow the recovery steps below before retrying.

The safest recovery is to create a new, empty destination schema and change `destination.database` in `config.json` to that schema. Grant the destination user access to the new schema as described above.

If you want to reuse the previous destination, first verify the exact schema name in `config.json`. The following commands permanently delete that schema and all its contents; run them only if it is the disposable anonymized copy, never the source database:

```sql
-- Replace this with the exact destination.database value from config.json
DROP DATABASE `development_database`;
CREATE DATABASE `development_database`;
```

If the destination user loses access after recreating the schema, reapply its `GRANT CREATE, INSERT, DELETE, INDEX, REFERENCES` statement. `DELETE` is used to clear a partially copied table when resuming a paused GUI copy. Then check that the destination is empty before retrying:

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
