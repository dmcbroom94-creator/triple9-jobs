"""Offline tests; no live requests. Run: python -m unittest discover -s tests -p 'test_nhs.py'."""
import datetime as dt
import html
import unittest
import urllib.parse
import xml.etree.ElementTree as ET

from collectors import nhs


CODE = "C9335"
ORG = nhs.EMPLOYERS[CODE]
MITIE = "E0059"


def advert(code, number):
    return "https://beta.jobs.nhs.uk/candidate/jobadvert/{}-26-{:04d}".format(code, number)


def vacancy(number, code=CODE, title="Newly Qualified Paramedic", **changes):
    return {"id": str(10000 + number), "reference": "{}-26-{:04d}".format(code, number),
            "title": title, "employer": nhs.EMPLOYERS[code], "url": advert(code, number),
            "locations": ["Nottingham, NG8 6PY"], **changes}


def xml(rows, total=None, pages=None):
    total = len(rows) if total is None else total
    pages = (total + 99) // 100 if pages is None else pages
    root = ET.Element("nhsJobs")
    for row in rows:
        el = ET.SubElement(root, "vacancyDetails")
        for key in ("id", "reference", "title", "employer", "url"):
            if key in row:
                ET.SubElement(el, key).text = row[key]
        loc = ET.SubElement(el, "locations")
        for s in row.get("locations", []):
            ET.SubElement(loc, "location").text = s
        ET.SubElement(el, "salary").text = "£99"  # deliberately differs from detail
        ET.SubElement(el, "closeDate").text = "2099-01-01"
    ET.SubElement(root, "totalPages").text = str(pages)
    ET.SubElement(root, "totalResults").text = str(total)
    return ET.tostring(root, encoding="utf-8")


def detail(row, status="The closing date is 29 November 2099", title=None,
           description="Work in an operational crew providing emergency response to patients.",
           pay="£32,073 to £39,043 a year", apply=True, employer=None):
    e = html.escape
    title = row["title"] if title is None else title
    employer = row["employer"] if employer is None else employer
    link = ('<a id="apply-ats-direct" href="/candidate/jobadvert/{}/ats-direct-apply">Apply for this job</a>'.format(e(row["reference"])) if apply else '')
    return ('<html><span id="employer_name">{}</span><h1 id="heading">{}</h1>'
            '<p id="closing_date"><strong>{}</strong></p>{}'
            '<p id="range_salary">{}</p><p id="employer_town">Nottingham</p>'
            '<p id="employer_postcode">NG8 6PY</p><p id="job_overview">{}</p></html>').format(
                e(employer), e(title), e(status), link, e(pay), e(description)).encode()


class FakeHTTP:
    def __init__(self, pages=None, details=None):
        self.pages = pages or {}
        self.details = details or {}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url.startswith(nhs.API):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            assert q.get("externalOnly") == ["true"]
            assert len(q.get("employerCode", [])) == 1
            result = self.pages.get((q["employerCode"][0], int(q["page"][0])))
        else:
            result = self.details.get(url)
        if result is None:
            raise nhs.SourceError("network/HTTP failure: fixture 404")
        if isinstance(result, Exception):
            raise result
        return result


def run_fake(fake, codes=(CODE,), **kwargs):
    return nhs.run(employer_codes=codes, fetch=fake, permission_confirmed=True, **kwargs)


class NHSAdapterTest(unittest.TestCase):
    def test_denied_by_default_makes_no_request(self):
        f = FakeHTTP()
        result = nhs.run(employer_codes=[CODE], existing_urls=[advert(CODE, 1)],
                         fetch=f, permission_confirmed=False)
        self.assertEqual(f.calls, [])
        self.assertEqual(result["closed_urls"], [])
        self.assertTrue(all(not s["success"] for s in result["sources"]))
        self.assertIn("permission", result["sources"][0]["error"])

    def test_real_public_query_and_verified_detail_output(self):
        row = vacancy(1)
        f = FakeHTTP({(CODE, 1): xml([row])}, {row["url"]: detail(row)})
        result = run_fake(f)
        source = result["sources"][0]
        self.assertTrue(source["success"], source["error"])
        self.assertEqual(source["error"], "")
        self.assertEqual(result["closed_urls"], [])
        job = source["jobs"][0]
        self.assertEqual(job["cat"], "ambulance")
        self.assertEqual(job["sourceJobId"], row["id"])
        self.assertEqual(job["url"], row["url"])
        self.assertEqual(job["source"], source["id"])
        self.assertEqual(job["pay"], "£32,073 to £39,043 a year")
        self.assertEqual(job["closes"], "Closes 29 Nov 2099")
        self.assertEqual(job["place"], "Nottingham, NG8 6PY")
        self.assertEqual(job["region"], "uk")
        timestamp = dt.datetime.fromisoformat(job["lastCheckedAt"].replace("Z", "+00:00"))
        self.assertEqual(timestamp.utcoffset(), dt.timedelta())
        self.assertGreaterEqual(timestamp, dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5))
        self.assertEqual(len(f.calls), 2)
        self.assertTrue(all(isinstance(x, str) and x for x in job.values()))

    def test_unknown_pay_is_see_advert_not_search_salary(self):
        row = vacancy(1)
        f = FakeHTTP({(CODE, 1): xml([row])}, {row["url"]: detail(row, pay="")})
        self.assertEqual(run_fake(f)["sources"][0]["jobs"][0]["pay"], "See advert")

    def test_custody_clinical_role_not_ambulance(self):
        row = vacancy(2, code=MITIE, title="Custody Nurse or Paramedic")
        desc = "A nurse or paramedic provides healthcare to detainees in police custody suites."
        f = FakeHTTP({(MITIE, 1): xml([row])}, {row["url"]: detail(row, description=desc)})
        src = run_fake(f, [MITIE])["sources"][0]
        self.assertTrue(src["success"], src["error"])
        self.assertEqual([j["cat"] for j in src["jobs"]], ["justice"])

    def test_generic_and_ambiguous_roles_never_categorised(self):
        self.assertIsNone(nhs._category("Nurse/Paramedic Clinical Advisor", "ambulance service and emergency care", CODE))
        self.assertIsNone(nhs._category("Paramedic", "A GP practice job", CODE))
        self.assertIsNone(nhs._category("Custody Nurse Manager", "nurse provides healthcare in custody suite", MITIE))
        self.assertIsNone(nhs._category("Paramedic", "NHS 111 call centre", CODE))
        self.assertFalse(nhs._title_candidate("Domestic Cleaner - Ambulance Station"))
        self.assertFalse(nhs._title_candidate("Qualified Paramedic - holding pool"))
        self.assertIsNone(nhs._category("Paramedic", "Internal applicants only. Emergency response.", CODE))
        self.assertIsNone(nhs._category("Paramedic - internal only", "Operational crew", CODE))
        self.assertIsNone(nhs._category("Paramedic - GP practice", "Requires an emergency driving qualification", CODE))

    def test_empty_result_is_valid(self):
        f = FakeHTTP({(CODE, 1): xml([], pages=0)})
        result = run_fake(f)
        self.assertEqual(result["sources"][0]["jobs"], [])
        self.assertTrue(result["sources"][0]["success"])

    def test_second_page_is_fetched_and_checked(self):
        fillers = [vacancy(i, title="Domestic Cleaner") for i in range(1, 101)]
        candidate = vacancy(101)
        f = FakeHTTP({(CODE, 1): xml(fillers, total=101),
                      (CODE, 2): xml([candidate], total=101)},
                     {candidate["url"]: detail(candidate)})
        result = run_fake(f)
        src = result["sources"][0]
        self.assertTrue(src["success"], src["error"])
        self.assertEqual([j["sourceJobId"] for j in src["jobs"]], [candidate["id"]])
        self.assertEqual(len(f.calls), 3)

    def test_partial_pagination_or_changed_counts_fail(self):
        fillers = [vacancy(i, title="Domestic Cleaner") for i in range(1, 101)]
        for second in (None, xml([vacancy(101)], total=102), xml([], total=101)):
            with self.subTest(second=second is None):
                f = FakeHTTP({(CODE, 1): xml(fillers, total=101), (CODE, 2): second})
                src = run_fake(f)["sources"][0]
                self.assertFalse(src["success"])
                self.assertTrue(src["error"])
                self.assertEqual(src["jobs"], [])

    def test_duplicate_records_are_incomplete(self):
        rows = [vacancy(i, title="Domestic Cleaner") for i in range(1, 100)]
        rows.append(dict(rows[0]))
        f = FakeHTTP({(CODE, 1): xml(rows, total=100)})
        src = run_fake(f)["sources"][0]
        self.assertFalse(src["success"])
        self.assertIn("duplicate", src["error"])

    def test_structural_feed_errors_fail(self):
        valid = vacancy(1)
        for broken in (b"<html>Blocked</html>", b"<nhsJobs></nhsJobs>", b"not xml",
                       b'<!DOCTYPE nhsJobs [<!ENTITY x "x">]><nhsJobs/>',
                       xml([{k: v for k, v in valid.items() if k != "id"}]),
                       xml([vacancy(1, employer="Different NHS Trust")]),
                       xml([vacancy(1, url="https://evil.example/candidate/jobadvert/abc")])):
            with self.subTest(broken=broken[:50]):
                src = run_fake(FakeHTTP({(CODE, 1): broken}))["sources"][0]
                self.assertFalse(src["success"])
                self.assertTrue(src["error"])

    def test_failed_detail_does_not_mean_closed(self):
        row = vacancy(1)
        for failure in (nhs.SourceError("HTTP 403"), nhs.SourceError("HTTP 404"),
                        nhs.SourceError("timeout"), b"<html><h1>captcha</h1></html>",
                        detail(row, apply=False), detail(row, status="The closing date is 01 January 2020")):
            with self.subTest(failure=failure):
                f = FakeHTTP({(CODE, 1): xml([row])}, {row["url"]: failure})
                res = run_fake(f)
                self.assertFalse(res["sources"][0]["success"])
                self.assertEqual(res["closed_urls"], [])
                self.assertEqual(res["sources"][0]["jobs"], [])

    def test_explicit_closed_marker_on_existing_url(self):
        row = vacancy(1)
        f = FakeHTTP(details={row["url"]: detail(row, status="This job is now closed", apply=False)})
        result = run_fake(f, [], existing_urls=[row["url"], row["url"]])
        self.assertEqual(result["closed_urls"], [row["url"]])
        self.assertTrue(result["sources"][0]["success"])
        self.assertEqual(len(f.calls), 1)

    def test_existing_official_share_url_is_preserved(self):
        row = vacancy(1)
        shared_url = row["url"] + "?language=en"
        f = FakeHTTP(details={shared_url: detail(row, status="This job is now closed", apply=False)})
        result = run_fake(f, [], existing_urls=[shared_url])
        self.assertEqual(result["closed_urls"], [shared_url])
        self.assertTrue(result["sources"][0]["success"])

    def test_existing_http_failures_are_unknown_not_closed(self):
        row = vacancy(1)
        res = run_fake(FakeHTTP(), [], existing_urls=[row["url"]])
        self.assertFalse(res["sources"][0]["success"])
        self.assertEqual(res["closed_urls"], [])

    def test_closed_words_in_description_do_not_change_open_status(self):
        row = vacancy(1)
        d = detail(row, description="Operational crew emergency response. This job is now closed was a past advert.")
        res = run_fake(FakeHTTP({(CODE, 1): xml([row])}, {row["url"]: d}))
        self.assertEqual(res["closed_urls"], [])
        self.assertEqual(len(res["sources"][0]["jobs"]), 1)

    def test_unknown_employer_and_unofficial_revalidation_are_rejected(self):
        f = FakeHTTP()
        result = run_fake(f, ["BAD"], existing_urls=["https://evil.example/candidate/jobadvert/C9335-26-0001"])
        self.assertTrue(all(not s["success"] for s in result["sources"]))
        self.assertEqual(f.calls, [])
        self.assertEqual(result["closed_urls"], [])

    def test_detail_employer_disagreement_is_source_failure(self):
        row = vacancy(1)
        f = FakeHTTP({(CODE, 1): xml([row])}, {row["url"]: detail(row, employer="Other NHS Trust")})
        src = run_fake(f)["sources"][0]
        self.assertFalse(src["success"])
        self.assertIn("employer", src["error"])
        self.assertEqual(src["jobs"], [])

    def test_partial_detail_failure_preserves_only_individually_verified_jobs(self):
        first, second = vacancy(1), vacancy(2)
        f = FakeHTTP({(CODE, 1): xml([first, second])}, {first["url"]: detail(first)})
        source = run_fake(f)["sources"][0]
        self.assertFalse(source["success"])
        self.assertEqual([j["sourceJobId"] for j in source["jobs"]], [first["id"]])
        self.assertIn(second["reference"], source["error"])

    def test_live_dry_run_limits_fail_instead_of_truncating(self):
        fillers = [vacancy(i, title="Domestic Cleaner") for i in range(1, 101)]
        f = FakeHTTP({(CODE, 1): xml(fillers, total=101)})
        src, _ = nhs._scan(CODE, f, max_pages=1, max_candidates=5)
        self.assertFalse(src["success"])
        self.assertIn("pagination limit", src["error"])
        self.assertEqual(len(f.calls), 1)
        rows = [vacancy(i) for i in range(1, 7)]
        f = FakeHTTP({(CODE, 1): xml(rows)})
        src, _ = nhs._scan(CODE, f, max_pages=1, max_candidates=5)
        self.assertFalse(src["success"])
        self.assertIn("detail limit", src["error"])
        self.assertEqual(len(f.calls), 1)


if __name__ == "__main__":
    unittest.main()
