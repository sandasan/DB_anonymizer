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

-- Confirm the account's privileges
SHOW GRANTS FOR 'anonymizer_readonly'@'localhost';
```

Use this username, source database name, and password in `config.json` and the `MYSQL_SOURCE_PASSWORD` environment variable. The account host (`localhost`) must match where the CLI connects from. If MySQL sees the connection from a different machine or over a different host, create the account with the appropriate client host instead. `mysqldump` also needs `SELECT`; depending on MySQL version and dump options it may require additional privileges such as `LOCK TABLES` or `TRIGGER`.

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

Review `rules.json` carefully. List sensitive data columns for anonymization and preserve login names, email addresses used for login, password hashes, and other authentication fields. The starter rules use column-name guesses and cannot identify every sensitive field. When satisfied that the copied password hashes should remain usable, set `"confirm_copy_password_hashes": true` in `rules.json`, then run:

```bash
# Copy into the separate, empty destination schema
mysql-anonymizer copy --config config.json --rules rules.json
```

The source should be accessed through a read-only account; the destination user should only have access to the destination schema. Destination must be empty. The tool never drops or truncates objects. It currently copies base tables and rows, not views, triggers, procedures, events, or grants. Review app-specific columns, free text, and JSON manually. Protect the resulting database like production credentials.
