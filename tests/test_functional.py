"""Functional tests for the database copy application's pure logic."""

import unittest
from datetime import date

from mysql_anonymizer.cli import check_config
from mysql_anonymizer.rules import init_rules, migrate_identifier_rules, prune_rules_to_schema, validate_rules
from mysql_anonymizer.schema import expand_related_tables, qname, resolve_table_selection
from mysql_anonymizer.transformations import text_column_length, transform, unique_text


def column(name, data_type="varchar", column_type="varchar(255)", key="", comment=""):
    return {
        "COLUMN_NAME": name,
        "DATA_TYPE": data_type,
        "COLUMN_TYPE": column_type,
        "COLUMN_KEY": key,
        "COLUMN_COMMENT": comment,
    }


TABLES = [
    {"name": "users", "references": [], "foreign_key_columns": [], "columns": [
        column("id", "int", "int", "PRI"), column("email"), column("password_hash"),
    ]},
    {"name": "orders", "references": ["users"], "foreign_key_columns": ["user_id"], "columns": [
        column("id", "int", "int", "PRI"), column("user_id", "int", "int"), column("status"),
    ]},
    {"name": "order_items", "references": ["orders"], "foreign_key_columns": ["order_id"], "columns": [
        column("id", "int", "int", "PRI"), column("order_id", "int", "int"), column("description"),
    ]},
    {"name": "audit_log", "references": ["users"], "foreign_key_columns": ["user_id"], "columns": [
        column("id", "int", "int", "PRI"), column("user_id", "int", "int"), column("event"),
    ]},
    {"name": "standalone", "references": [], "foreign_key_columns": [], "columns": [column("value")]},
]


class SchemaSelectionTests(unittest.TestCase):
    def test_quote_name_escapes_backticks(self):
        self.assertEqual(qname("db`name", "table`name"), "`db``name`.`table``name`")

    def test_selection_recursively_includes_parent_dependencies(self):
        self.assertEqual(resolve_table_selection(TABLES, ["order_items"], True), [
            "users", "orders", "order_items",
        ])

    def test_selection_requires_missing_parent_without_dependency_option(self):
        with self.assertRaisesRegex(ValueError, "users"):
            resolve_table_selection(TABLES, ["orders"], False)

    def test_existing_parent_satisfies_dependency(self):
        self.assertEqual(resolve_table_selection(TABLES, ["orders"], False, {"users"}), ["orders"])

    def test_related_expansion_walks_both_directions_recursively(self):
        self.assertEqual(expand_related_tables(TABLES, ["orders"]), [
            "users", "orders", "order_items", "audit_log",
        ])

    def test_related_expansion_preserves_schema_order_and_rejects_unknown_names(self):
        self.assertEqual(expand_related_tables(TABLES, ["standalone"]), ["standalone"])
        with self.assertRaisesRegex(ValueError, "missing"):
            expand_related_tables(TABLES, ["missing"])


class RulesTests(unittest.TestCase):
    def test_generated_rules_preserve_identifiers_and_credentials(self):
        rules = init_rules(TABLES)
        self.assertIn("id", rules["tables"]["users"]["preserve"])
        self.assertIn("password_hash", rules["tables"]["users"]["preserve"])
        self.assertNotIn("user_id", rules["tables"]["orders"]["anonymize"])

    def test_migration_moves_foreign_key_columns_to_preserve(self):
        rules = {"tables": {"orders": {"preserve": [], "anonymize": {"user_id": {"type": "constant", "value": 0}}}}}
        moved = migrate_identifier_rules(rules, TABLES)
        self.assertEqual(moved, ["orders.user_id"])
        self.assertEqual(rules["tables"]["orders"], {"preserve": ["user_id"], "anonymize": {}})

    def test_prune_removes_only_stale_references_from_copy(self):
        rules = {"tables": {"users": {"preserve": ["id", "gone"], "anonymize": {"email": {"type": "email"}, "old": {"type": "name"}}}}}
        pruned, removed = prune_rules_to_schema(rules, TABLES, {"users"})
        self.assertEqual(removed, ["users.gone", "users.old"])
        self.assertIn("gone", rules["tables"]["users"]["preserve"])
        self.assertNotIn("gone", pruned["tables"]["users"]["preserve"])

    def test_validation_rejects_anonymized_foreign_key_and_preserves_auth(self):
        bad_fk = {"tables": {"orders": {"preserve": [], "anonymize": {"user_id": {"type": "constant", "value": "x"}}}}}
        with self.assertRaisesRegex(ValueError, "Identifier column"):
            validate_rules(TABLES, bad_fk)
        bad_password = {"tables": {"users": {"preserve": [], "anonymize": {"password_hash": {"type": "constant", "value": "x"}}}}}
        with self.assertRaisesRegex(ValueError, "Authentication column"):
            validate_rules(TABLES, bad_password)


class TransformationTests(unittest.TestCase):
    def test_text_transformations_are_deterministic_and_keep_nulls(self):
        rule = {"type": "email"}
        first = transform("person@example.com", rule, "users", "email")
        self.assertEqual(first, transform("person@example.com", rule, "users", "email"))
        self.assertTrue(first.endswith("@example.invalid"))
        self.assertIsNone(transform(None, rule, "users", "email"))

    def test_date_shift_preserves_type_and_invalid_value_fails(self):
        value = date(2000, 1, 1)
        shifted = transform(value, {"type": "date_shift"}, "users", "birthday")
        self.assertIsInstance(shifted, date)
        with self.assertRaisesRegex(ValueError, "non-date"):
            transform("2000-01-01", {"type": "date_shift"}, "users", "birthday")

    def test_ip_transform_is_valid_and_invalid_input_is_rejected(self):
        import ipaddress
        result = transform("192.0.2.1", {"type": "ip_address"}, "users", "ip")
        self.assertEqual(ipaddress.ip_address(result).version, 4)
        with self.assertRaisesRegex(ValueError, "Invalid IP address"):
            transform("not-an-ip", {"type": "ip_address"}, "users", "ip")

    def test_unique_text_fits_length_and_resolves_collisions_stably(self):
        mappings, used = {}, set()
        first = unique_text("a", "same-value", "users", "code", 5, mappings, used)
        second = unique_text("b", "same-value", "users", "code", 5, mappings, used)
        self.assertEqual(len(first), 5)
        self.assertEqual(len(second), 5)
        self.assertNotEqual(first, second)
        self.assertEqual(unique_text("a", "changed", "users", "code", 5, mappings, used), first)

    def test_column_length_parsing(self):
        self.assertEqual(text_column_length("VARCHAR(32)"), 32)
        self.assertEqual(text_column_length("text"), 255)


class ConfigurationTests(unittest.TestCase):
    def test_source_and_destination_must_be_different(self):
        config = {"source": {"database": "source"}, "destination": {"database": "copy"}}
        check_config(config)
        config["destination"]["database"] = "SOURCE"
        with self.assertRaisesRegex(ValueError, "different"):
            check_config(config)

    def test_missing_database_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "source.database"):
            check_config({"source": {}, "destination": {"database": "copy"}})


if __name__ == "__main__":
    unittest.main()
