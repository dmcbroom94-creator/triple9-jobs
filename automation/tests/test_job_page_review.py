"""Offline regression tests for manual job reviews and category pages."""

from collections import Counter
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from urllib.parse import urlsplit


SITE = Path(__file__).resolve().parents[2] / "site"
NODE = shutil.which("node")
PAGES = (
    "custody-nurse-paramedic-jobs.html",
    "paramedic-ambulance-jobs.html",
    "rail-train-driver-jobs.html",
)
RAIL_LINKS = {
    "https://www.lnerjobs.co.uk/jobs/vacancy/find/results/",
    "https://www.gwr.com/about-gwr/careers",
    "https://www.northernrailway.co.uk/careers",
    "https://apply.scotrail.co.uk/vacancies/",
    "https://jobs.tfw.wales/jobs/home/",
    "https://careers.southwesternrailway.com/jobs/search",
    "https://www.careers.avantiwestcoast.co.uk/jobs",
    "https://wmtrains.pinpointhq.com/",
    "https://www.gtrailwaycareers.com/jobs/vacancy/find/results/",
}


class PageParser(HTMLParser):
    """Collect scripts and the employer directory under each separate root."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.script_sources = []
        self.script = None
        self.directories = {"t9-app": [], "t9-tpl": []}
        self.root = None
        self.root_depth = 0
        self.directory = None
        self.section_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.script = []
            if "src" in attrs:
                self.script_sources.append(attrs["src"])

        if self.root is None:
            if (tag, attrs.get("id")) in (("div", "t9-app"), ("template", "t9-tpl")):
                self.root = attrs["id"]
                self.root_depth = 1
        elif tag == ("div" if self.root == "t9-app" else "template"):
            self.root_depth += 1

        if self.root and tag == "section":
            if self.directory is not None:
                self.section_depth += 1
            elif attrs.get("aria-labelledby") == "rail-employer-heading":
                self.directory = {"links": [], "text": []}
                self.directories[self.root].append(self.directory)
                self.section_depth = 1
        if self.directory is not None and tag == "a" and "href" in attrs:
            self.directory["links"].append(attrs["href"])

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.directory is not None:
            self.directory["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        if tag == "section" and self.directory is not None:
            self.section_depth -= 1
            if self.section_depth == 0:
                self.directory = None
        if self.root and tag == ("div" if self.root == "t9-app" else "template"):
            self.root_depth -= 1
            if self.root_depth == 0:
                self.root = None


def parse_page(name):
    parser = PageParser()
    parser.feed((SITE / name).read_text(encoding="utf-8"))
    parser.close()
    return parser


def match_expression(name):
    # Read the declaration from the page's script, not a duplicated predicate.
    pattern = re.compile(r"^[ \t]*const[ \t]+MATCH[ \t]*=[ \t]*(.*?);[ \t]*$", re.M | re.S)
    matches = [match.group(1) for script in parse_page(name).scripts
               for match in pattern.finditer(script)]
    if len(matches) != 1:
        raise AssertionError(f"Expected one MATCH declaration in {name}, found {len(matches)}")
    return matches[0]


def run_node(script, payload):
    """Run only built-in Node modules, with data on stdin and no network access."""
    result = subprocess.run(
        [NODE, "-e", script], input=json.dumps(payload), text=True,
        capture_output=True, check=True, timeout=10,
        env={**os.environ, "TZ": "America/Los_Angeles"},
    )
    return json.loads(result.stdout)


CONFIG_RUNNER = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const context = { window: {} };
// Compile the external file exactly as shipped, then run it without a browser.
new vm.Script(input.source, { filename: 'config.js' }).runInNewContext(context, { timeout: 1000 });
if (input.inspect) {
  console.log(JSON.stringify({
    review: typeof context.window.T9_isJobWithinReview,
    refresh: typeof context.window.T9_scheduleReviewRefresh,
    jobs: context.window.T9_JOBS_URL,
    courses: context.window.T9_COURSES_URL
  }));
} else {
  console.log(JSON.stringify(input.cases.map(c =>
    context.window.T9_isJobWithinReview(c.job, new Date(c.now)))));
}
"""

MATCH_RUNNER = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const match = new vm.Script('(' + input.expression + ')').runInNewContext({}, { timeout: 1000 });
console.log(JSON.stringify(input.jobs.map(job => Boolean(match(job)))));
"""

HOME_RUNNER = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const context = { window: {}, DCLogic: class { setState(p) { Object.assign(this.state, p); } } };
const Component = new vm.Script(input.source + '\n;Component').runInNewContext(context, { timeout: 1000 });
const component = new Component({});
component.state.remote = { jobs: input.jobs };
console.log(JSON.stringify(input.states.map(state => {
  Object.assign(component.state, { tab: 'all', skill: 'any', q: '' }, state);
  const result = component.renderVals();
  return { titles: result.jobs.map(j => j.title), tags: result.jobs.map(j => j.tags), nAmb: result.nAmb };
})));
"""


class ExternalConfigReferenceTests(unittest.TestCase):
    def test_pages_load_the_external_config(self):
        for name in PAGES:
            with self.subTest(page=name):
                sources = parse_page(name).script_sources
                self.assertIn("config.js", [urlsplit(src).path for src in sources])


@unittest.skipUnless(NODE, "Node is required for JavaScript execution")
class ReviewDateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (SITE / "config.js").read_text(encoding="utf-8")

    def check_cases(self, cases):
        actual = run_node(CONFIG_RUNNER, {"source": self.source, "cases": cases})
        self.assertEqual(len(actual), len(cases))
        for case, result in zip(cases, actual):
            with self.subTest(case=case):
                self.assertIs(result, case["expected"])

    def test_external_config_parses_and_exposes_helpers(self):
        result = run_node(CONFIG_RUNNER, {"source": self.source, "inspect": True})
        self.assertEqual(result["review"], "function")
        self.assertEqual(result["refresh"], "function")
        for key in ("jobs", "courses"):
            self.assertEqual(urlsplit(result[key]).scheme, "https")
            self.assertTrue(urlsplit(result[key]).netloc)

    def test_missing_review_date_stays_visible(self):
        self.check_cases([
            {"job": {}, "now": "2026-11-21T12:00:00Z", "expected": True},
            {"job": {"title": "Ordinary job"}, "now": "invalid", "expected": True},
        ])

    def test_review_date_includes_the_whole_uk_day_bst_and_gmt(self):
        cases = []
        for review, before_midnight, after_midnight in (
            ("2026-07-15", "2026-07-15T22:59:59.999Z", "2026-07-15T23:00:00Z"),
            ("2026-12-15", "2026-12-15T23:59:59.999Z", "2026-12-16T00:00:00Z"),
            # Midnight after the spring and autumn clock-change days.
            ("2026-03-29", "2026-03-29T22:59:59.999Z", "2026-03-29T23:00:00Z"),
            ("2026-10-25", "2026-10-25T23:59:59.999Z", "2026-10-26T00:00:00Z"),
        ):
            cases.extend([
                {"job": {"manualReviewUntil": review}, "now": before_midnight, "expected": True},
                {"job": {"manualReviewUntil": review}, "now": after_midnight, "expected": False},
            ])
        cases.extend([
            {"job": {"manualReviewUntil": "2026-12-16"}, "now": "2026-12-15T12:00:00Z", "expected": True},
            {"job": {"manualReviewUntil": "2024-02-29"}, "now": "2024-02-29T12:00:00Z", "expected": True},
        ])
        self.check_cases(cases)

    def test_invalid_review_dates_and_invalid_current_date_fail_closed(self):
        invalid = (None, "", True, 20261215, [], {}, "2026-2-01", "2026-12-15T00:00:00Z",
                   " 2026-12-15", "2026-02-29", "2024-02-30", "2026-04-31", "0000-01-01",
                   "2026-00-01", "2026-13-01", "2026-01-00", "2026-01-32")
        cases = [{"job": {"manualReviewUntil": value}, "now": "2026-01-01T12:00:00Z", "expected": False}
                 for value in invalid]
        cases.append({"job": {"manualReviewUntil": "2026-12-15"}, "now": "invalid", "expected": False})
        self.check_cases(cases)


@unittest.skipUnless(NODE, "Node is required for JavaScript execution")
class PageMatchTests(unittest.TestCase):
    def check_jobs(self, page, cases):
        jobs = [{"title": "Ordinary role", "org": "Example employer", **job} for job, _ in cases]
        actual = run_node(MATCH_RUNNER, {"expression": match_expression(page), "jobs": jobs})
        self.assertEqual(len(actual), len(cases))
        for job, (_, expected), result in zip(jobs, cases, actual):
            with self.subTest(page=page, job=job):
                self.assertIs(result, expected)

    def test_custody_requires_ambulance_nurse_or_paramedic_and_custody(self):
        self.check_jobs(PAGES[0], [
            ({"cat": "ambulance", "skills": ["nurse"], "custodyHealthcare": True}, True),
            ({"cat": "ambulance", "skills": ["para"], "custodyHealthcare": True}, True),
            ({"cat": "ambulance", "skills": ["nurse", "para"], "custodyHealthcare": True}, True),
            ({"cat": "ambulance", "skills": ["nurse"], "title": "CUSTODY clinician"}, True),
            ({"cat": "ambulance", "skills": ["para"], "org": "Custody Healthcare"}, True),
            ({"cat": "ambulance", "skills": ["nurse"], "custodyHealthcare": False}, False),
            ({"cat": "ambulance", "skills": ["hca"], "custodyHealthcare": True}, False),
            ({"cat": "ambulance", "custodyHealthcare": True}, False),
            ({"cat": "civilian", "skills": ["para"], "custodyHealthcare": True}, False),
            ({"cat": "police", "title": "Unrelated job"}, False),
        ])

    def test_ambulance_and_civilian_medical_including_first_responder(self):
        self.check_jobs(PAGES[1], [
            ({"cat": "ambulance", "skills": ["para"]}, True),
            ({"cat": "ambulance"}, True),
            ({"cat": "civilian", "areas": ["medical"], "title": "First Responder"}, True),
            ({"cat": "civilian", "areas": ["transport", "medical"]}, True),
            ({"cat": "civilian", "areas": ["transport"], "title": "First Responder"}, False),
            ({"cat": "civilian"}, False),
            ({"cat": "police", "areas": ["medical"]}, False),
        ])

    def test_rail_civilian_transport_and_railway_search_terms(self):
        self.check_jobs(PAGES[2], [
            ({"cat": "civilian", "areas": ["transport"]}, True),
            ({"cat": "civilian", "areas": ["medical", "transport"]}, True),
            ({"cat": "other", "org": "Network Rail"}, True),
            ({"cat": "other", "org": "National Highways"}, True),
            ({"cat": "other", "title": "Trainee TRAIN DRIVER"}, True),
            ({"cat": "other", "org": "Transport for London"}, True),
            ({"cat": "other", "org": "London Underground"}, True),
            ({"cat": "civilian", "areas": ["medical"]}, False),
            ({"cat": "police", "areas": ["transport"]}, False),
            ({"cat": "ambulance", "title": "Paramedic"}, False),
        ])

    def test_homepage_medical_tab_and_custody_search(self):
        scripts = [script for script in parse_page("index.html").scripts
                   if "class Component extends DCLogic" in script]
        self.assertEqual(len(scripts), 1)
        common = {"org": "Example employer", "place": "London", "pay": "See advert",
                  "closes": "See advert"}
        jobs = [
            {**common, "url": "https://example.test/custody", "cat": "ambulance",
             "title": "Registered Nurse/Paramedic", "skills": ["nurse", "para"],
             "custodyHealthcare": True},
            {**common, "url": "https://example.test/medical", "cat": "civilian",
             "title": "Private paramedic", "areas": ["medical"], "skills": ["para"]},
            {**common, "url": "https://example.test/security", "cat": "civilian",
             "title": "Security officer", "areas": ["security"]},
        ]
        result = run_node(HOME_RUNNER, {"source": scripts[0], "jobs": jobs, "states": [
            {"tab": "ambulance"}, {"tab": "ambulance", "skill": "para"},
            {"tab": "civilian"}, {"q": "custody nurse"},
        ]})
        self.assertEqual(result[0]["titles"], ["Registered Nurse/Paramedic", "Private paramedic"])
        self.assertEqual(result[0]["nAmb"], 2)
        self.assertEqual(result[1]["titles"], ["Registered Nurse/Paramedic", "Private paramedic"])
        self.assertEqual(result[2]["titles"], ["Private paramedic", "Security officer"])
        self.assertEqual(result[3]["titles"], ["Registered Nurse/Paramedic"])
        self.assertIn("Custody healthcare", result[3]["tags"][0])


class RailDirectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directories = parse_page(PAGES[2]).directories

    def check_directory(self, root):
        sections = self.directories[root]
        self.assertEqual(len(sections), 1, f"Expected one railway directory under #{root}")
        section = sections[0]
        self.assertEqual(Counter(section["links"]), Counter(RAIL_LINKS))
        text = " ".join(" ".join(section["text"]).lower().split())
        self.assertIn("vacancy search pages", text)
        self.assertIn("separate resources", text)
        self.assertIn("not current job listings", text)
        self.assertRegex(text, r"(?:not automatically refresh(?:ed)?|does not automatically refresh)")
        self.assertIn("check each employer", text)

    def test_static_app_has_railway_search_resources(self):
        self.check_directory("t9-app")

    def test_template_has_railway_search_resources(self):
        self.check_directory("t9-tpl")


if __name__ == "__main__":
    unittest.main()
