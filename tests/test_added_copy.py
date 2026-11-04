"""Whether a pull request adds a piece of copy (gates/_added_copy.py), which copy-audit and ui-audit both ask before a
finding blocks. Driven through the functions on diff text as added_text() normalizes it."""
import unittest

import helpers  # noqa: F401  (puts gates/ on the path)

from _added_copy import added_by_pull_request, normal  # noqa: E402


class AddedCopy(unittest.TestCase):
    def test_four_consecutive_words_on_an_added_line_are_the_pull_requests(self):
        added = normal("<p>They get the portal&apos;s invite email and join {org}</p>")
        self.assertTrue(added_by_pull_request("They get the portal’s invite email and join Acme as User.", added))
        self.assertFalse(added_by_pull_request("You can resend the invite from the list.", added))

    def test_a_short_label_counts_only_whole_as_a_string_an_elements_text_or_a_line_of_its_own(self):
        for line in ("[AddUserMode.INVITE, 'Send invite'],", "<Button>Send invite</Button>", "  Send invite"):
            self.assertTrue(added_by_pull_request("Send invite", normal(line)), line)
        for line in ("const sendInvite = invite;", "<Button>Send invites</Button>", "return row.send_invite;"):
            self.assertFalse(added_by_pull_request("Send invite", normal(line)), line)
