"""Tests for request size limits and ticket field validation."""

import io
import unittest
from unittest.mock import patch

import database
import validation
from helpdesk import auth
from security_helpers import LOCAL_URL, TESTING_ENV, SecurityTestCase

ONE_MIB = 1024 * 1024
EMOJI = "\U0001F600"


class TestCleanText(unittest.TestCase):

    def clean(self, value, max_length=10, multiline=True):
        return validation.clean_text(
            value, label="Field", max_length=max_length, multiline=multiline
        )

    def test_text_at_limit_is_kept(self):
        self.assertEqual(self.clean("a" * 10).value, "a" * 10)
        self.assertIsNone(self.clean("a" * 10).error)

    def test_text_over_limit_is_rejected_not_shortened(self):
        result = self.clean("a" * 11)

        self.assertIn("10 characters", result.error)
        self.assertEqual(result.value, "a" * 11)

    def test_length_counts_characters_not_bytes(self):
        for character in ("é", "漢", EMOJI):
            with self.subTest(character=character):
                self.assertIsNone(self.clean(character * 10).error)
                self.assertIsNotNone(self.clean(character * 11).error)

    def test_combining_marks_count_as_characters(self):
        self.assertIsNotNone(self.clean("é" * 6).error)

    def test_windows_newlines_count_once(self):
        # 11 characters as submitted, but 9 once each line break is one
        result = self.clean("abc\r\ndef\r\n1")

        self.assertIsNone(result.error)
        self.assertEqual(result.value, "abc\ndef\n1")

    def test_old_mac_newlines_are_normalised(self):
        self.assertEqual(self.clean("a\rb").value, "a\nb")

    def test_surrounding_whitespace_does_not_count(self):
        self.assertEqual(self.clean("  " + "a" * 10 + "\n ").value, "a" * 10)

    def test_control_characters_are_rejected(self):
        for character in ("\x00", "\x07", "\x1b", "\x7f", "\x85"):
            with self.subTest(character=repr(character)):
                self.assertIsNotNone(self.clean(f"a{character}b").error)

    def test_surrogates_are_rejected(self):
        self.assertIsNotNone(self.clean("a\ud800b").error)

    def test_newline_and_tab_depend_on_multiline(self):
        for character in ("\n", "\t"):
            with self.subTest(character=repr(character)):
                text = f"a{character}b"

                self.assertIsNone(self.clean(text, multiline=True).error)
                self.assertIsNotNone(self.clean(text, multiline=False).error)

    def test_invisible_only_text_counts_as_empty(self):
        for text in ("", "   ", "\n\t", "​", " ‍", None):
            with self.subTest(text=repr(text)):
                result = self.clean(text)

                self.assertEqual(result.value, "")
                self.assertIsNone(result.error)

    def test_error_never_repeats_the_input(self):
        result = self.clean("<script>" * 5)

        self.assertNotIn("<script>", result.error)


class InputLimitTestCase(SecurityTestCase):

    settings = TESTING_ENV
    base_url = LOCAL_URL


class TestTicketFieldLimits(InputLimitTestCase):

    def create(self, **fields):
        data = {"title": "Title", "description": "Body", "priority": "Low"}
        data.update(fields)

        return self.post_form(self.client, "/tickets/new", data)

    def setUp(self):
        super().setUp()
        self.client = self.login("alice")

    def latest_ticket(self):
        conn = database.get_db()
        try:
            return conn.execute(
                "SELECT * FROM tickets ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

    def assert_saved_exactly(self, field, value, **fields):
        response = self.create(**fields)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.latest_ticket()[field], value)

    def assert_rejected(self, **fields):
        before = self.count("tickets")
        response = self.create(**fields)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.count("tickets"), before)

        return response

    def test_title_boundaries(self):
        self.assert_saved_exactly("title", "a" * 199, title="a" * 199)
        self.assert_saved_exactly("title", "a" * 200, title="a" * 200)
        self.assert_rejected(title="a" * 201)

    def test_description_boundaries(self):
        for length in (9_999, 10_000):
            self.assert_saved_exactly(
                "description", "a" * length, description="a" * length
            )

        self.assert_rejected(description="a" * 10_001)

    def test_multibyte_title_boundaries(self):
        for character in ("é", "漢", EMOJI):
            with self.subTest(character=character):
                self.assert_saved_exactly(
                    "title", character * 200, title=character * 200
                )
                self.assert_rejected(title=character * 201)

    def test_multibyte_description_boundaries(self):
        self.assert_saved_exactly(
            "description", EMOJI * 10_000, description=EMOJI * 10_000
        )
        self.assert_rejected(description=EMOJI * 10_001)

    def test_browser_line_endings_fit_the_limit(self):
        text = "a\r\n" * 5_000
        expected = ("a\n" * 5_000).strip()

        self.assert_saved_exactly("description", expected, description=text)

    def test_control_characters_are_rejected(self):
        self.assert_rejected(title="bad\x00title")
        self.assert_rejected(description="bad\x1bdescription")

    def test_title_cannot_contain_a_line_break(self):
        self.assert_rejected(title="first\nsecond")

    def test_rejection_keeps_the_users_text_safely_escaped(self):
        response = self.assert_rejected(
            title="<b>" + "a" * 200,
            description="<script>keep this</script>",
            priority="High"
        )

        self.assertIn("&lt;b&gt;", response.text)
        self.assertIn("&lt;script&gt;keep this", response.text)
        self.assertNotIn("<script>keep this", response.text)
        self.assertRegex(response.text, r'value="High"\s+checked')

    def test_rejection_message_is_useful(self):
        response = self.assert_rejected(title="a" * 201)

        self.assertIn("200 characters or fewer", response.text)

    def test_empty_fields_keep_their_existing_message(self):
        before = self.count("tickets")

        response = self.create(title="   ", description="​")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Title and description are required", response.text)
        self.assertEqual(self.count("tickets"), before)

    def test_form_advertises_the_same_limits(self):
        html = self.get(self.client, "/tickets/new").text

        self.assertIn('maxlength="200"', html)
        self.assertIn('maxlength="10000"', html)

    def test_invalid_utf8_does_not_cause_a_server_error(self):
        token = self.csrf_token(self.client, "/tickets/new")

        response = self.client.post(
            "/tickets/new",
            data=(
                f"csrf_token={token}&title=%FF%FE&description=%ED%A0%80"
                "&priority=Low"
            ),
            content_type="application/x-www-form-urlencoded",
            base_url=self.base_url
        )

        self.assertLess(response.status_code, 500)


class TestCommentLimits(InputLimitTestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = self.make_ticket(owner="alice")
        self.client = self.login("alice")
        self.path = f"/tickets/{self.ticket_id}/comment"

    def comment(self, content, client=None):
        return self.post_form(
            client or self.client, self.path, {"content": content}
        )

    def stored(self):
        return [row["content"] for row in database.get_comments(self.ticket_id)]

    def test_comment_boundaries(self):
        for length in (9_999, 10_000):
            self.assertEqual(self.comment("a" * length).status_code, 302)
            self.assertEqual(self.stored()[-1], "a" * length)

        before = self.stored()

        self.assertEqual(self.comment("a" * 10_001).status_code, 400)
        self.assertEqual(self.stored(), before)

    def test_multibyte_comment_boundaries(self):
        self.assertEqual(self.comment(EMOJI * 10_000).status_code, 302)
        self.assertEqual(self.stored()[-1], EMOJI * 10_000)
        self.assertEqual(self.comment(EMOJI * 10_001).status_code, 400)
        self.assertEqual(len(self.stored()), 1)

    def test_rejected_comment_is_returned_for_editing(self):
        response = self.comment("<i>" + "a" * 10_000)

        self.assertEqual(response.status_code, 400)
        self.assertIn("&lt;i&gt;", response.text)
        self.assertIn("10,000 characters", response.text)

    def test_control_characters_are_rejected(self):
        self.assertEqual(self.comment("bad\x00note").status_code, 400)
        self.assertEqual(self.stored(), [])

    def test_empty_comment_keeps_its_existing_behaviour(self):
        self.assertEqual(self.comment("   ").status_code, 302)
        self.assertEqual(self.stored(), [])

    def test_ownership_is_checked_before_length(self):
        bob = self.login("bob")

        response = self.post_form(bob, self.path, {"content": "a" * 10_001})

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Printer offline", response.text)

    def test_oversized_comment_still_needs_csrf_token(self):
        response = self.client.post(
            self.path,
            data={"content": "a" * 10_001},
            base_url=self.base_url
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("10,000", response.text)

    def test_page_advertises_the_limit(self):
        html = self.get(self.client, f"/tickets/{self.ticket_id}").text

        self.assertIn('maxlength="10000"', html)


class TestRequestBodyLimit(InputLimitTestCase):

    def setUp(self):
        super().setUp()
        self.client = self.login("alice")
        self.token = self.csrf_token(self.client, "/tickets/new")

    def raw_post(self, total_bytes):
        prefix = f"csrf_token={self.token}&title=t&priority=Low&description="
        body = prefix + "a" * (total_bytes - len(prefix))

        return self.client.post(
            "/tickets/new",
            data=body,
            content_type="application/x-www-form-urlencoded",
            base_url=self.base_url
        )

    def test_body_at_limit_reaches_field_validation(self):
        self.assertEqual(self.raw_post(ONE_MIB).status_code, 400)

    def test_body_one_byte_over_limit_is_refused(self):
        before = self.count("tickets")
        response = self.raw_post(ONE_MIB + 1)

        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.count("tickets"), before)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertIn("1 MiB", response.text)

    def test_oversized_body_before_login_is_refused(self):
        response = self.app.test_client().post(
            "/login",
            data={"username": "a" * (ONE_MIB + 1)},
            base_url=self.base_url
        )

        self.assertEqual(response.status_code, 413)

    def test_oversized_multipart_is_refused(self):
        response = self.client.post(
            "/tickets/new",
            data={
                "csrf_token": self.token,
                "title": "t",
                "description": "d",
                "file": (io.BytesIO(b"a" * (ONE_MIB + 1)), "large.bin")
            },
            content_type="multipart/form-data",
            base_url=self.base_url
        )

        self.assertEqual(response.status_code, 413)

    def test_too_many_multipart_fields_are_refused(self):
        parts = b"".join(
            b'--B\r\nContent-Disposition: form-data; name="f%d"\r\n\r\nx\r\n'
            % number
            for number in range(validation.MAX_FORM_PARTS + 1)
        )

        response = self.client.post(
            "/tickets/new",
            data=parts + b"--B--\r\n",
            content_type="multipart/form-data; boundary=B",
            base_url=self.base_url
        )

        self.assertEqual(response.status_code, 413)

    def test_chunked_body_without_length_cannot_exceed_limit(self):
        before = self.count("tickets")
        body = (
            f"csrf_token={self.token}&title=t&priority=Low&description="
            + "a" * (2 * ONE_MIB)
        ).encode()

        response = self.client.post(
            "/tickets/new",
            input_stream=io.BytesIO(body),
            content_type="application/x-www-form-urlencoded",
            headers={"Transfer-Encoding": "chunked"},
            environ_overrides={"wsgi.input_terminated": True},
            base_url=self.base_url
        )

        self.assertIn(response.status_code, (400, 413))
        self.assertEqual(self.count("tickets"), before)

    def test_limits_are_configured(self):
        config = self.app.config

        self.assertEqual(config["MAX_CONTENT_LENGTH"], ONE_MIB)
        self.assertEqual(config["MAX_FORM_MEMORY_SIZE"], ONE_MIB)
        self.assertEqual(config["MAX_FORM_PARTS"], validation.MAX_FORM_PARTS)


class TestLoginLimits(InputLimitTestCase):

    def attempt(self, username, password):
        return self.post_form(
            self.app.test_client(),
            "/login",
            {"username": username, "password": password},
            token_page="/login"
        )

    def test_oversized_password_is_not_hashed(self):
        with patch.object(auth, "check_password_hash") as checker:
            response = self.attempt("alice", "a" * 1025)

        checker.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Invalid username or password", response.text)

    def test_password_at_limit_is_still_checked(self):
        with patch.object(
            auth, "check_password_hash", return_value=False
        ) as checker:
            self.attempt("alice", "a" * 1024)

        checker.assert_called_once()

    def test_oversized_username_is_not_looked_up(self):
        with patch.object(self.module.db, "get_user") as lookup:
            response = self.attempt("a" * 151, "password")

        lookup.assert_not_called()
        self.assertIn("Invalid username or password", response.text)


class TestAssigneeAndTicketNumbers(InputLimitTestCase):

    def setUp(self):
        super().setUp()
        self.ticket_id = self.make_ticket(owner="alice")
        self.admin = self.login("admin")

    def update(self, assigned_to, ticket_id=None):
        return self.post_form(
            self.admin,
            f"/tickets/{ticket_id or self.ticket_id}/update",
            {"status": "Open", "assigned_to": assigned_to}
        )

    def test_oversized_assignee_is_rejected(self):
        self.assertEqual(self.update("a" * 151).status_code, 400)
        self.assertIsNone(database.get_ticket(self.ticket_id)["assigned_to"])

    def test_control_characters_in_assignee_are_rejected(self):
        self.assertEqual(self.update("tech\nadmin").status_code, 400)
        self.assertIsNone(database.get_ticket(self.ticket_id)["assigned_to"])

    def test_valid_assignee_still_works(self):
        self.assertEqual(self.update("tech").status_code, 302)
        self.assertEqual(
            database.get_ticket(self.ticket_id)["assigned_to"], "tech"
        )

    def test_existing_unusual_assignee_does_not_block_updates(self):
        unusual = "a" * 151

        conn = database.get_db()
        conn.execute(
            "UPDATE tickets SET assigned_to = ? WHERE id = ?",
            (unusual, self.ticket_id)
        )
        conn.commit()
        conn.close()

        self.assertEqual(self.update(unusual).status_code, 302)

    def test_impossible_ticket_numbers_return_404(self):
        for number in (2 ** 63, 2 ** 70, 10 ** 40):
            with self.subTest(number=number):
                self.assertEqual(
                    self.get(self.admin, f"/tickets/{number}").status_code,
                    404
                )
                self.assertEqual(
                    self.update("tech", ticket_id=number).status_code,
                    404
                )
                self.assertEqual(
                    self.post_form(
                        self.admin,
                        f"/tickets/{number}/comment",
                        {"content": "x"}
                    ).status_code,
                    404
                )


if __name__ == "__main__":
    unittest.main()
