
"""
Unit tests for csv_export.py.

These tests need no database or Flask: they check the CSV text itself,
reading it back with csv.reader exactly as a spreadsheet import would.
"""

import csv
import io
import unittest

from csv_export import EXPORT_COLUMNS, build_tickets_csv, safe_cell


def ticket(**fields):
    base = {
        "id": 1, "title": "Printer offline", "description": "Paper jam",
        "priority": "High", "status": "Open", "assigned_to": None,
        "created_by": "alice", "created_at": "2026-11-10 07:00:00",
        "updated_at": "2026-11-10 07:30:00",
        # Never exported, even if a row happens to carry it
        "password_hash": "pbkdf2:sha256:secret",
    }
    base.update(fields)
    return base


def read_back(csv_text):
    return list(csv.reader(io.StringIO(csv_text)))


class TestCsvLayout(unittest.TestCase):

    def test_header_row_lists_every_column(self):
        rows = read_back(build_tickets_csv([]))

        self.assertEqual(rows, [[
            "Ticket ID", "Title", "Description", "Priority", "Status",
            "Assigned To", "Created By", "Created (UTC)",
            "Last Updated (UTC)",
        ]])

    def test_ticket_values_appear_in_column_order(self):
        rows = read_back(build_tickets_csv([ticket(assigned_to="tech1")]))

        self.assertEqual(rows[1], [
            "1", "Printer offline", "Paper jam", "High", "Open", "tech1",
            "alice", "2026-11-10 07:00:00", "2026-11-10 07:30:00",
        ])

    def test_missing_values_become_empty_cells(self):
        rows = read_back(build_tickets_csv([ticket(assigned_to=None)]))

        self.assertEqual(rows[1][5], "")

    def test_sensitive_fields_are_never_exported(self):
        csv_text = build_tickets_csv([ticket()])

        self.assertNotIn("password", csv_text.lower())
        self.assertNotIn("pbkdf2", csv_text)
        self.assertNotIn("password_hash", [f for _, f in EXPORT_COLUMNS])

    def test_rows_end_with_crlf_as_spreadsheets_expect(self):
        csv_text = build_tickets_csv([ticket()])

        self.assertTrue(csv_text.endswith("\r\n"))
        self.assertEqual(csv_text.count("\r\n"), 2)


class TestCsvEscaping(unittest.TestCase):

    def round_trip(self, value):
        """Write a title and read it back as a spreadsheet would."""
        return read_back(build_tickets_csv([ticket(title=value)]))[1][1]

    def test_commas_stay_inside_one_cell(self):
        self.assertEqual(self.round_trip("Printer, scanner, fax"),
                         "Printer, scanner, fax")

    def test_quotation_marks_are_preserved(self):
        self.assertEqual(self.round_trip('Error "0x80070005" on login'),
                         'Error "0x80070005" on login')

    def test_multiline_text_stays_inside_one_cell(self):
        description = "Line one\nLine two\r\nLine three"
        rows = read_back(build_tickets_csv([ticket(description=description)]))

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][2], description)


class TestFormulaInjection(unittest.TestCase):

    def test_formula_triggers_are_neutralised(self):
        for value in ("=1+1", "+1+1", "-1+1", "@SUM(A1)",
                      '=HYPERLINK("http://evil.example","Click")',
                      "=cmd|' /C calc'!A0"):
            with self.subTest(value=value):
                self.assertEqual(safe_cell(value), "'" + value)

    def test_whitespace_cannot_hide_a_formula(self):
        for value in (" =1+1", "   @SUM(A1)", "\n=1+1", "\r=1+1",
                      "\t=1+1", "\x00=1+1", " =1+1", " \t\n-2+3"):
            with self.subTest(value=repr(value)):
                self.assertTrue(safe_cell(value).startswith("'"))

    def test_leading_tab_or_carriage_return_is_neutralised(self):
        self.assertEqual(safe_cell("\tdata"), "'\tdata")
        self.assertEqual(safe_cell("\rdata"), "'\rdata")

    def test_full_width_lookalikes_are_neutralised(self):
        for value in ("＝1+1", "＋1", "－1", "＠A1"):
            with self.subTest(value=repr(value)):
                self.assertTrue(safe_cell(value).startswith("'"))

    def test_ordinary_text_is_unchanged(self):
        for value in ("Printer offline", "Email = broken?", "a-b", "x@y.com",
                      "100% CPU", ""):
            with self.subTest(value=value):
                self.assertEqual(safe_cell(value), value)

    def test_ticket_ids_stay_numbers(self):
        self.assertEqual(safe_cell(42), 42)

    def test_every_text_column_is_protected(self):
        attack = "=1+1"
        row = ticket(**{field: attack for _, field in EXPORT_COLUMNS
                        if field != "id"})

        exported = read_back(build_tickets_csv([row]))[1]

        self.assertEqual(exported[0], "1")
        for cell in exported[1:]:
            self.assertEqual(cell, "'=1+1")


if __name__ == "__main__":
    unittest.main()
