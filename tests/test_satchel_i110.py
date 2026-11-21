"""#110 (Rowan's #107 round-2 low): the userinfo detector must stay linear on long unbroken runs, and every URL
password it masked before is still masked. Synthetic fixtures only."""

import time
import unittest

from synthetics import satchel

N = 100_000
# Generous: the bounded pattern scans 100 KB in a few milliseconds; the unbounded one took seconds per input.
BOUND = 2.0


def userinfo_hits(text):
    return [(s, e) for s, e, k in satchel.detect(text) if k == "userinfo"]


class LinearOnLongRuns(unittest.TestCase):
    def timed(self, text):
        start = time.perf_counter()
        satchel.detect(text)
        return time.perf_counter() - start

    def check(self, text):
        elapsed = self.timed(text)
        self.assertLess(elapsed, BOUND, "detect took %.2f s on %d characters" % (elapsed, len(text)))

    def test_letters(self):
        self.check("a" * N)

    def test_hex(self):
        self.check(("0123456789abcdef" * (N // 16 + 1))[:N])

    def test_letters_and_digits(self):
        self.check("a1" * (N // 2))

    def test_long_user_without_password(self):
        self.check("s://" + "u" * N)

    def test_long_password_without_at(self):
        self.check("s://u:" + "p" * N)


class StillMasked(unittest.TestCase):
    def value(self, text, secret):
        start = text.index(secret)
        self.assertEqual(userinfo_hits(text), [(start, start + len(secret))])

    def test_plain_url(self):
        self.value("see https://admin:Hunter2pw@example.invalid/x", "Hunter2pw")

    def test_scheme_glued_to_a_long_word(self):
        # The match starts at most 64 characters before "://"; the password is still found.
        self.value("x" * 5000 + "https://admin:Hunter2pw@example.invalid/x", "Hunter2pw")

    def test_scheme_after_a_digit(self):
        self.value("1https://admin:Hunter2pw@example.invalid/x", "Hunter2pw")

    def test_longest_allowed_scheme(self):
        # A scheme starts with a letter, so a letter followed by 63 digits is the longest scheme the bound allows.
        self.value("z" + "9" * 63 + "://admin:Hunter2pw@example.invalid/x", "Hunter2pw")

    def test_scheme_limit(self):
        # Stated limit: with no letter in the 64 characters before "://", nothing can start a scheme. No real
        # scheme looks like this.
        self.assertEqual(userinfo_hits("z" + "9" * 64 + "://admin:Hunter2pw@example.invalid/x"), [])

    def test_compound_scheme(self):
        self.value("postgresql+psycopg2://app:Hunter2pw@db.invalid:5432/x", "Hunter2pw")

    def test_no_password_no_hit(self):
        self.assertEqual(userinfo_hits("https://admin@example.invalid/x"), [])


if __name__ == "__main__":
    unittest.main()
