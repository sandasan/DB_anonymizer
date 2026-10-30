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

In the GUI, **Recreate selected tables** drops and recopies the selected tables plus every table connected to them through same-schema foreign keys, recursively. The confirmation lists all tables that will be replaced; this requires `DROP` on those tables. **Clear destination data** drops every base table in the configured destination schema and resets the saved copy checkpoint and copied/recreated marks; it requires `DROP` on those tables. The database itself is kept. **Recreate destination DB** drops and recreates the configured destination schema, then copies all source tables; this requires database-level `DROP` and `CREATE` privileges. The normal writer account above intentionally lacks these privileges. Use a dedicated maintenance account for these actions, and never grant them on the source database.

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

The PySide6 window lets you load connection settings and rules, inspect the source, select tables, include foreign-key dependencies, edit or generate anonymization rules, copy data, browse source or destination tables, and search mapping values in either direction. With **Select directly related tables automatically** enabled, selecting a table also selects tables it references and tables that reference it. On the **Tables** tab, enable **Relationship-preserving date range** and choose inclusive start and end timestamps. Every selected table with `DATE`, `DATETIME`, or `TIMESTAMP` columns and a primary key is filtered by the same interval, matching rows where any temporal column is in range. Selected tables without temporal columns, or without a primary key, are copied in full. The app follows foreign keys from selected tables to required parent rows recursively; parent rows can have dates outside the selected interval. It does not scan unselected child tables, so select any child tables you want copied. Date-filtered rows and related parent rows need primary keys for row selection; cross-schema foreign keys are rejected. During range copies, tables already present in the destination are reused, so you can copy additional selected tables using the same interval without clearing previously copied tables.

Scroll areas keep controls reachable at shorter window heights. **Overall copy progress** tracks completion across selected tables, and **Current table** tracks the active table. **Pause copy** saves a checkpoint at the next batch boundary. Closing the window during a copy requests a pause; the next launch offers to resume. Completed tables are retained and the interrupted table is recopied. If a table fails during copying, **Remove failed table and continue** drops that incomplete destination table, removes its lookup mappings, skips it, and resumes the other selected tables from the saved checkpoint. This action requires `DROP` on the destination table. The checkpoint is stored in the app's user configuration directory with owner-only permissions and includes connection settings, including any password entered in the GUI, as well as the date-range selection.

The **Anonymization rules** tab has a searchable column navigator, rule-status filters, a transformation editor, and a JSON editor. **Tables** shows destination tables that already exist; attempting to copy one offers to mark it as already copied, while **Recreate selected tables** replaces it. **Clear destination data** drops all base tables and can also clear the selected mapping store after a separate confirmation. **Recreate destination DB** requires typing its name. The **Browse data** tab searches across all columns or filters one column and displays up to 200 matching rows. **Test connections** checks both database accounts and reports common authentication or grant errors.

### Anonymization types

Add a transformation to a column under that table's `anonymize` object. These examples show the rule syntax and typical output; generated values are deterministic for the same table, column, and original value, except `password`, which generates a fresh unusable marker. `NULL` values remain `NULL`.

| Type | Example rule | Example output for a representative input |
| --- | --- | --- |
| `email` | `{"type": "email"}` | `user123456789@example.invalid` |
| `url` | `{"type": "url"}` | Replaced with a deterministic synthetic URL under `https://example.invalid/`. |
| `phone` | `{"type": "phone"}` | `+15551234567` |
| `payment_key` | `{"type": "payment_key"}` | A synthetic `test_…` key; suitable for LiqPay or other payment credentials. This explicit type is allowed for columns named `key`, `token`, or `secret`. |
| `password` | `{"type": "password"}` | Replaces a password or password hash with a fresh 13-character unusable marker beginning with `!`; removes the original credential and prevents hash-based logins for that account. |
| `card_mask` | `{"type": "card_mask"}` | `5168********1234` becomes `**** **** **** 0427`; all original card digits are replaced with a deterministic synthetic last four. |
| `card_number` | `{"type": "card_number"}` | Replaced with a deterministic synthetic 16-digit value with a valid Luhn checksum and a leading `0` outside standard payment network prefixes. |
| `card_payexpirydate` | `{"type": "card_payexpirydate"}` | A deterministic synthetic expiry month/year in `MM/YY` format. |
| `card_validitydate` | `{"type": "card_validitydate"}` | A deterministic synthetic date in `DD/MM/YYYY` format. |
| `card_expirydate` | `{"type": "card_expirydate"}` | A deterministic synthetic day/month in `DD/MM` format. |
| `card_securitycode` | `{"type": "card_securitycode"}` | A synthetic three-digit code; the original security code is discarded. |
| `card_type` | `{"type": "card_type"}` | A deterministic replacement from `VISA`, `MASTERCARD`, `AMEX`, or `DISCOVER`. |
| `description` | `{"type": "description"}` | `redacted` (replaces comments, transaction descriptions, and other free text with a fixed placeholder). |
| `name` | `{"type": "name"}` | `Person01234567` |
| `entity_name` | `{"type": "entity_name"}` | A generic deterministic label such as `Entity01234567`, suitable for hotel, company, or other non-person names. |
| `address` | `{"type": "address"}` | `1234 Example Street` |
| `file_name` | `{"type": "file_name"}` | `reports/quarterly.pdf` becomes `file123456789.pdf` (directory components are removed; the extension is retained). |
| `ip_address` | `{"type": "ip_address"}` | IPv4 becomes an address in `10.0.0.0/8`; IPv6 becomes an address in `2001:db8::/32`. Invalid input stops the copy with a column-specific error. |
| `date_shift` | `{"type": "date_shift"}` | Shifts a date or timestamp by a stable number of days, from 3,650 days earlier to 3,650 days later. Requires a date/time value. |
| `constant` | `{"type": "constant", "value": "redacted"}` | `redacted` |
| `unique` | `{"type": "unique"}` | An opaque `anon…` pseudonym derived from a hash of the original value. |
| `passport` | `{"type": "passport"}` | A deterministic fake number. Add `"country": "GB"` for the supported `GB123456C`-style template; without a country it produces 10 digits. |
| `pnr_code` | `{"type": "pnr_code"}` | A deterministic six-character uppercase alphanumeric code, such as `XTRQ9W`. |
| `location_generalization` | `{"type": "location_generalization", "target": "hotel_class", "value": "Luxury_5_Star_Hotel"}` | `Luxury_5_Star_Hotel`. Supply the generalized label in `value`; without it, the output is `Hotel`. |
| `numeric_variance` | `{"type": "numeric_variance", "percent": 10}` | Deterministically adjusts a numeric amount by up to ±10%. |
| `coordinate_round` | `{"type": "coordinate_round", "precision": 2}` | Rounds numeric `latitude`/`longitude` values to two decimal places (about 1.1 km north-south); use lower precision for coarser location privacy. |
| `date_round` | `{"type": "date_round", "granularity": "month"}` | `2026-10-18` becomes `2026-10-01`. `year` rounds to January 1; `decade` rounds to January 1 of the decade. |

For example, to anonymize a customer's email and phone while replacing a note with a fixed value:

```json
{
  "tables": {
    "customers": {
      "preserve": [],
      "anonymize": {
        "email": {"type": "email"},
        "phone": {"type": "phone"},
        "note": {"type": "constant", "value": "redacted"}
      }
    }
  }
}
```

Text transformations are intended for text columns; `password` is only allowed on columns named like passwords or password hashes. It creates a fresh unusable marker, so affected accounts cannot log in with their former passwords; applications with custom password storage or validation should be checked before use. The type requires a text column at least 13 characters wide. `date_shift` and `date_round` are for date/time columns; `numeric_variance` is for numeric columns and accepts `percent` from 0 to 100. `coordinate_round` applies to numeric columns named `lat`, `latitude`, `lon`, `lng`, `long`, or `longitude`; it rounds to 2 decimal places by default and accepts an integer `precision` from 0 to 6. Latitude and longitude are rounded independently, so this generalizes a point to a coordinate grid and does not maintain a fixed displacement across records. Two decimal places are roughly 1.1 km north-south, with east-west distance decreasing toward the poles. `date_round` supports `month`, `year`, and `decade`. `location_generalization` currently supports `hotel_class`; it does not infer a hotel's real class, so set `value` to an appropriate generalized label. The passport examples are format templates, not checks against a country's official passport issuance rules; the source country is not inferred, so set `country` explicitly when using the GB template. `pnr_code` is stable for repeated copies of the same source value, rather than freshly random on each run. The `unique` type creates a deterministic pseudonym, and unique text columns also get collision handling that fits the destination column length. For `constant`, `value` may be any JSON value supported by the destination column. Choose values that satisfy the column's type and constraints. These transformations do not discover every sensitive field automatically: review generated rules and unconfigured columns before copying.

Use **Load config** and **Load rules** to open JSON files, and **Save config as** or **Save rules as** to write new files. Config and rules can be saved to their selected paths. The GUI remembers the last paths and reopens those files at startup. Passwords entered in the window are saved in the config file in plain text; leave a password field blank to use `password_env`. If rules refer to columns missing from the inspected source, copying asks before ignoring those rules. Columns without a rule are copied unchanged.

Use **Save profile** and **Load profile** to keep named config, rules, mapping-store paths, and copy options. Profiles and the GUI's recent paths, table selections, and copy checkpoints are stored in the app configuration directory. Profiles and copy checkpoints use owner-only file permissions. A profile stores the mapping-store path, not a copy of the mapping database. The auto-confirm option skips routine copy and table-action prompts; destructive whole-database actions still require typing the destination name.

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

## Tests

Run the functional tests with Python's standard library test runner:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The suite covers schema selection and related-table expansion, anonymization rules, transformations, and configuration validation without requiring a live MySQL server.
