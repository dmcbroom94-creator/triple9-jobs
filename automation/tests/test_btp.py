"""Offline fixtures for the BTP reader. Run with python -m unittest discover -s tests."""
import importlib.util
import pathlib
import unittest
from unittest.mock import patch

MODULE = pathlib.Path(__file__).resolve().parents[1] / "collectors" / "btp.py"
spec = importlib.util.spec_from_file_location("btp", MODULE)
btp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(btp)


def job_url(ident, slug="police-officer"):
    return btp.BASE + "jobs/job/" + slug + "/" + str(ident)


def card(ident, title="Police Officer - London", slug="police-officer"):
    return '<li><a href="%s"><span class="job-list-title">%s</span></a></li>' % (job_url(ident, slug), title)


def search(cards, next_url=None, featured=""):
    next_link = ('<a href="%s"><span class="sr-only">Next results page</span>&gt;</a>' % next_url) if next_url else ""
    return ('<div id="results"><div class="featured"><ul class="jobs">%s</ul></div>'
            '<h2>Jobs</h2><ul class="jobs">%s</ul>%s</div>' % (featured, cards, next_link))


def detail(ident, title="Police Officer - London", location="London, United Kingdom", salary="£32,000",
           closing="31 December, 2099", apply=True, category="Police Officer", status=""):
    def row(label, value):
        return '<li><strong class="label">%s</strong>%s</li>' % (label, value)
    rows = row("Job Reference", "BTP/674/" + str(ident))
    rows += row("Location", location) + row("Job Category", category)
    if salary is not None:
        rows += row("Salary", salary)
    if closing is not None:
        rows += row("Closing Date", closing)
    button = ('<a class="btn btn-apply" href="https://careershub.btp.police.uk/members/?j=%s&amp;lang=en">Apply</a>' % ident) if apply else ""
    return ('<html><p>%s</p><div class="box sidebar-section"><h3>%s</h3>'
            '<ul class="job-details-overview">%s</ul>%s</div></html>' % (status, title, rows, button))


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        value = self.pages.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise btp.SourceError("unexpected fixture request " + url)
        return url, value


def one_source(result):
    return result["sources"][0]


class BtpFixtureTests(unittest.TestCase):
    def test_featured_ignored_pagination_dedupe_and_exact_detail_fields(self):
        page2 = btp.BASE + "jobs/search/-1/2"
        pages = {
            btp.BASE: search(card(1) + card(2, "First Contact Centre - Contact Handler"), page2,
                             featured=card(999) + card(1)),
            page2: search(card(1, slug="different-slug") + card(3, "Transferee Police Constables - York")),
            job_url(1): detail(1, salary="£32,000 – £45,000", closing="30 November, 2099"),
            job_url(2): detail(2, "First Contact Centre - Contact Handler", salary="£37,005.87", category="Police Staff"),
            job_url(3): detail(3, "Transferee Police Constables - York", salary=None, closing="No expiry date"),
        }
        client = FakeClient(pages)
        result = btp._scan(client)
        source = one_source(result)
        self.assertTrue(source["success"], source["error"])
        self.assertEqual(result["closed_urls"], [])
        self.assertEqual([j["sourceJobId"] for j in source["jobs"]], ["1", "2", "3"])
        self.assertNotIn(job_url(999), client.requests)
        self.assertEqual(client.requests.count(job_url(1)), 1)
        a, c, b = source["jobs"]
        self.assertEqual((a["pay"], a["place"], a["closes"]), ("£32,000 – £45,000", "London, United Kingdom", "Closes 30 November, 2099"))
        self.assertEqual((c["cat"], b["pay"], b["closes"]), ("control", "See advert", "Open – see advert"))
        self.assertEqual((a["org"], a["region"], a["source"]), ("British Transport Police", "uk", "btp"))
        self.assertTrue(a["lastCheckedAt"].endswith("Z"))

    def test_partial_pagination_never_successful(self):
        page2 = btp.BASE + "jobs/search/-1/2"
        client = FakeClient({btp.BASE: search(card(1), page2), page2: btp.SourceError("HTTP 503"), job_url(1): detail(1)})
        result = btp._scan(client)
        self.assertFalse(one_source(result)["success"])
        self.assertEqual(len(one_source(result)["jobs"]), 1)
        self.assertIn("HTTP 503", one_source(result)["error"])
        self.assertEqual(result["closed_urls"], [])

    def test_detail_403_404_timeout_are_unknown_not_closed(self):
        for error in ("HTTP 403", "HTTP 404", "Read timed out"):
            with self.subTest(error=error):
                client = FakeClient({btp.BASE: search(card(1)), job_url(1): btp.SourceError(error)})
                result = btp._scan(client)
                self.assertFalse(one_source(result)["success"])
                self.assertFalse(one_source(result)["jobs"])
                self.assertEqual(result["closed_urls"], [])

    def test_closed_marker_wins_even_with_an_apply_button(self):
        client = FakeClient({btp.BASE: search(card(1)), job_url(1): detail(1, status="This job is now closed")})
        result = btp._scan(client)
        self.assertTrue(one_source(result)["success"])
        self.assertEqual(one_source(result)["jobs"], [])
        self.assertEqual(result["closed_urls"], [job_url(1)])

    def test_no_positive_open_signal_is_unknown(self):
        client = FakeClient({btp.BASE: search(card(1)), job_url(1): detail(1, apply=False)})
        result = btp._scan(client)
        self.assertFalse(one_source(result)["success"])
        self.assertFalse(one_source(result)["jobs"])
        self.assertFalse(result["closed_urls"])

    def test_wrong_apply_job_id_is_not_positive(self):
        html = detail(1).replace("?j=1&amp;", "?j=2&amp;")
        with self.assertRaises(btp.SourceError):
            btp._detail(html, job_url(1), "1")

    def test_past_date_and_bad_date_are_unknown_not_closed(self):
        for close in ("1 January, 2001", "at the end of the year"):
            with self.subTest(close=close):
                client = FakeClient({btp.BASE: search(card(1)), job_url(1): detail(1, closing=close)})
                result = btp._scan(client)
                self.assertFalse(one_source(result)["success"])
                self.assertEqual(result["closed_urls"], [])

    def test_inconsistent_reference_location_and_duplicate_fields_fail(self):
        for html in (detail(2), detail(1, location=""), detail(1).replace("</ul>", '<li><strong class="label">Location</strong>York</li></ul>')):
            with self.subTest(html=html):
                with self.assertRaises(btp.SourceError):
                    btp._detail(html, job_url(1), "1")

    def test_structural_failure_and_unexpected_empty_list(self):
        for html in ("<html>Not a search page</html>", search(""), '<div id="results"><ul class="jobs"><li>bad</li></ul></div>'):
            with self.subTest(html=html):
                result = btp._scan(FakeClient({btp.BASE: html}))
                self.assertFalse(one_source(result)["success"])
                self.assertEqual(result["closed_urls"], [])

    def test_explicit_empty_result_is_valid_but_next_conflict_is_not(self):
        html = '<div id="results">No jobs found</div>'
        self.assertTrue(one_source(btp._scan(FakeClient({btp.BASE: html})))["success"])
        html = '<div id="results">No jobs found <a href="/jobs/search/-1/2">Next results page</a></div>'
        self.assertFalse(one_source(btp._scan(FakeClient({btp.BASE: html})))["success"])

    def test_loop_repeated_page_and_page_cap_fail(self):
        page2 = btp.BASE + "jobs/search/-1/2"
        page3 = btp.BASE + "jobs/search/-1/3"
        variants = [
            ({btp.BASE: search(card(1), page2), page2: search(card(2), page2)}, 50),
            ({btp.BASE: search(card(1), page2), page2: search(card(1))}, 50),
            ({btp.BASE: search(card(1), page2), page2: search(card(2), page3)}, 2),
        ]
        for pages, cap in variants:
            pages.update({job_url(1): detail(1), job_url(2): detail(2)})
            with self.subTest(pages=pages):
                self.assertFalse(one_source(btp._scan(FakeClient(pages), max_pages=cap))["success"])

    def test_untrusted_next_and_untrusted_job_link_fail(self):
        bad_next = search(card(1), "https://evil.example/jobs/search/-1/2")
        bad_job = search(card(1).replace(btp.HOST, "evil.example"))
        for html in (bad_next, bad_job):
            self.assertFalse(one_source(btp._scan(FakeClient({btp.BASE: html})))["success"])

    def test_numbered_later_page_without_next_is_incomplete(self):
        html = search(card(1))
        html = html[:-6] + '<a href="/jobs/search/-1/2">2</a></div>'
        client = FakeClient({btp.BASE: html, job_url(1): detail(1)})
        source = one_source(btp._scan(client))
        self.assertFalse(source["success"])
        self.assertIn("Next link is missing", source["error"])

    def test_ambiguous_pools_staff_and_trainers_are_skipped(self):
        titles = ["Police Officer Trainer", "Police Community Support Officer", "Police Officer Talent Pool",
                  "Control Room Systems Engineer", "Recruitment Contact Handler", "Work Experience"]
        self.assertTrue(all(btp._role(title) is None for title in titles))
        self.assertEqual(btp._role("First Contact Centre - Contact Handler"), "control")
        self.assertEqual(btp._role("Transferee Police Constables - Control Room"), "police")
        client = FakeClient({btp.BASE: search("".join(card(i + 1, title) for i, title in enumerate(titles)))})
        self.assertTrue(one_source(btp._scan(client))["success"])
        self.assertEqual(client.requests, [btp.BASE])

    def test_detail_limit_marks_incomplete(self):
        client = FakeClient({btp.BASE: search(card(1) + card(2)), job_url(1): detail(1)})
        result = btp._scan(client, max_details=1)
        self.assertFalse(one_source(result)["success"])
        self.assertEqual(len(one_source(result)["jobs"]), 1)
        self.assertIn("Detail limit", one_source(result)["error"])


class Response:
    def __init__(self, body="", status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.body = body.encode()
    def iter_content(self, chunk_size):
        yield self.body
    def close(self):
        pass


class Session:
    def __init__(self, mapping):
        self.headers = {}
        self.mapping = mapping
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.mapping[url]


class ClientSafetyTests(unittest.TestCase):
    def test_robots_denial_stops_requests(self):
        session = Session({btp.BASE + "robots.txt": Response("User-agent: *\nDisallow: /\n")})
        with self.assertRaises(btp.SourceError):
            btp._Client(session=session)
        self.assertEqual(len(session.calls), 1)

    def test_unsupported_robots_wildcard_and_anchor_fail_closed(self):
        for path in ("/jobs/*", "/jobs/private$"):
            with self.subTest(path=path):
                session = Session({btp.BASE + "robots.txt": Response("User-agent: *\nDisallow: " + path + "\n")})
                with self.assertRaises(btp.SourceError):
                    btp._Client(session=session)
                self.assertEqual(len(session.calls), 1)

    def test_robots_10_second_delay_and_redirect_validation(self):
        session = Session({btp.BASE + "robots.txt": Response("User-agent: *\nCrawl-delay: 10\n"),
                           btp.BASE: Response(status=302, headers={"Location": "https://evil.example/"})})
        clock = [100.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(btp.time, "monotonic", side_effect=lambda: clock[0]), patch.object(btp.time, "sleep", side_effect=sleep):
            client = btp._Client(session=session)
            with self.assertRaises(btp.SourceError):
                client.get(btp.BASE)
        self.assertGreaterEqual(clock[0], 110.0)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(not args["allow_redirects"] for _, args in session.calls))
        self.assertTrue(all(args["timeout"][0] <= 15 and args["timeout"][1] <= 20 for _, args in session.calls))

    def test_disallowed_detail_path_is_not_fetched(self):
        session = Session({btp.BASE + "robots.txt": Response("User-agent: *\nDisallow: /jobs/job/\n")})
        client = btp._Client(session=session)
        with self.assertRaises(btp.SourceError):
            client.get(job_url(1))
        self.assertEqual(len(session.calls), 1)

    def test_utf8_without_header_charset_preserves_salary(self):
        body = Response("£32,000 – £45,000", headers={"Content-Type": "text/html"})
        body.encoding = "ISO-8859-1"  # requests' default when charset is absent
        session = Session({btp.BASE + "robots.txt": Response("User-agent: *\nCrawl-delay: 10\n"), btp.BASE: body})
        clock = [100.0]
        with patch.object(btp.time, "monotonic", side_effect=lambda: clock[0]), patch.object(btp.time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)):
            client = btp._Client(session=session)
            self.assertEqual(client.get(btp.BASE)[1], "£32,000 – £45,000")


if __name__ == "__main__":
    unittest.main()
