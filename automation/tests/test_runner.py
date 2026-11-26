import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("triple9_runner", ROOT / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

NOW = datetime(2026, 11, 19, 22, 30, tzinfo=timezone.utc)
CHECKED = "2026-11-19T22:00:00Z"


def job(ident="C123-26-0001", host="beta.jobs.nhs.uk", **kwargs):
    out = {"cat": "ambulance", "title": "Paramedic", "org": "NHS Trust",
           "place": "London", "pay": "£30,000", "closes": "See advert",
           "url": f"https://{host}/candidate/jobadvert/{ident}", "region": "lon",
           "source": "nhs", "sourceJobId": ident, "lastCheckedAt": CHECKED}
    out.update(kwargs)
    return out


def legacy(ident="C123-26-0001", **kwargs):
    j = job(ident, **kwargs)
    for k in ("source", "sourceJobId", "lastCheckedAt"):
        j.pop(k, None)
    return j


def feed(*jobs):
    return {"updated": "01 Jan 2025", "other": {"keep": [1, 2]}, "jobs": list(jobs)}


def result(adapter="nhs", jobs=(), *, success=True, closed=(), error="", source_id=None):
    return {adapter: {"sources": [{"id": source_id or adapter, "success": success,
                                   "jobs": list(jobs), "error": error}],
                      "closed_urls": list(closed)}}


def btp_job(ident="123"):
    return job(cat="police", title="Constable", org="BTP", region="uk", source="btp",
               sourceJobId=ident, url=f"https://vacancies.btp.police.uk/jobs/job/constable/{ident}")


class MergeTests(unittest.TestCase):
    def test_curated_metadata_and_taxonomy_preserved(self):
        old = legacy(host="www.jobs.nhs.uk", cat="control", region="se|lon", pay="same",
                     ptype="staff", nations=["eng"], ranks=["pc"], routes=["transfer"],
                     entry="experienced", hours="shift", force="special", skills=["emergency"],
                     jroles=["prison"], areas=["general"], pmin=100, pmax=200, custom="keep")
        unrelated = legacy("C123-26-0002", title="Other")
        current = job(title="Updated title", place="New place", pay="same", cat="ambulance", region="uk",
                      ptype="officer", nations=["wal"], ranks=["dc"], routes=["new"],
                      entry="new", hours="full", force="other", skills=["new"],
                      jroles=["other"], areas=["other"], pmin=20, pmax=50)
        original = feed(old, unrelated)
        before = copy.deepcopy(original)
        out, stats = runner.merge_feed(original, result(jobs=[current]), NOW)
        self.assertEqual(original, before)
        self.assertEqual(out["other"], before["other"])
        self.assertEqual(out["jobs"][1], unrelated)
        updated = out["jobs"][0]
        for field in runner.FILTERS | {"cat", "region", "url", "custom"}:
            self.assertEqual(updated.get(field), old.get(field), field)
        self.assertEqual(updated["title"], "Updated title")
        self.assertEqual(updated["place"], "New place")
        self.assertEqual(updated["lastCheckedAt"], CHECKED)
        self.assertEqual(stats["changed"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(out["updated"], "19 Nov 2026")
        self.assertEqual(out["publishedAt"], "2026-11-19T22:30:00Z")
        health = out["sourceHealth"]["sources"][1]
        self.assertEqual((health["id"], health["status"], health["verifiedJobs"]), ("nhs", "success", 1))

    def test_changed_pay_removes_stale_bounds_unless_explicit(self):
        old = legacy(pay="old", pmin=100, pmax=200)
        current = job(pay="new", pmin=110)
        out, _ = runner.merge_feed(feed(old), result(jobs=[current]), NOW)
        self.assertEqual(out["jobs"][0]["pmin"], 110)
        self.assertNotIn("pmax", out["jobs"][0])
        current.pop("pmin")
        out, _ = runner.merge_feed(feed(old), result(jobs=[current]), NOW)
        self.assertNotIn("pmin", out["jobs"][0])
        self.assertNotIn("pmax", out["jobs"][0])

    def test_duplicate_ids_new_and_existing_variations(self):
        existing = legacy(host="www.jobs.nhs.uk")
        newer = job(title="Current")
        new = job("C123-26-0002")
        out, stats = runner.merge_feed(feed(existing), result(jobs=[newer, new, copy.deepcopy(new)]), NOW)
        self.assertEqual(len(out["jobs"]), 2)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(out["jobs"][0]["url"], existing["url"])
        self.assertEqual(out["jobs"][0]["title"], "Current")
        conflict = copy.deepcopy(new)
        conflict["title"] = "conflicting"
        with self.assertRaisesRegex(runner.ValidationError, "conflicting"):
            runner.merge_feed(feed(existing), result(jobs=[new, conflict]), NOW)
        with self.assertRaisesRegex(runner.ValidationError, "duplicate"):
            runner.merge_feed(feed(existing, legacy(host="beta.jobs.nhs.uk")), result(jobs=[new]), NOW)

    def test_stable_id_never_uses_title_and_tal_is_tenant_scoped(self):
        a = legacy(title="Same title")
        b = job("C123-26-9999", title="Same title")
        out, stats = runner.merge_feed(feed(a), result(jobs=[b]), NOW)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(len(out["jobs"]), 2)
        tal_a = "https://asp.tal.net/vx/appcentre-External/opp/123-old/en-GB"
        tal_b = "https://bch.tal.net/vx/appcentre-External/opp/123-new/en-GB"
        self.assertNotEqual(runner.canonical_url(tal_a), runner.canonical_url(tal_b))
        self.assertEqual(runner.canonical_url(tal_a), runner.canonical_url("https://asp.tal.net/vx/opp/123-different/en-GB?tracking=1"))
        self.assertEqual(runner.canonical_url("https://jobs.justice.gov.uk/careers/JobDetail/slug/123"),
                         runner.canonical_url("https://jobs.justice.gov.uk/careers/JobDetail/123"))
        self.assertEqual(runner.canonical_url("https://vacancies.btp.police.uk/jobs/job/old-slug/123"),
                         runner.canonical_url("https://vacancies.btp.police.uk/jobs/job/new-slug/123/"))

    def test_adapter_source_ids_and_nhs_api_numeric_identity(self):
        tal_url = "https://asp.tal.net/vx/opp/123-Constable/en-GB"
        tal_job = job(cat="police", source="avon_oleeo", sourceJobId="123", url=tal_url)
        out, stats = runner.merge_feed(feed(), result("tal", [tal_job], source_id="avon_oleeo"), NOW)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(out["jobs"][0]["url"], tal_url)
        old = job("C9202-26-0001", source="nhs_jobs_C9202", sourceJobId="987654",
                  lastCheckedAt="2026-11-19T21:00:00Z", title="Original")
        incoming = job("C9202-26-0002", source="nhs_jobs_C9202", sourceJobId="987654", title="Updated")
        data = result(jobs=[incoming], source_id="nhs_jobs_C9202")
        out, stats = runner.merge_feed(feed(old), data, NOW)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["changed"], 1)
        self.assertEqual(out["jobs"][0]["url"], old["url"])
        self.assertEqual(out["jobs"][0]["title"], "Updated")
        wrong_employer = job("C9335-26-0001", source="nhs_jobs_C9202", sourceJobId="456")
        data = result(jobs=[wrong_employer], source_id="nhs_jobs_C9202")
        data.update(result("btp"))  # An independently healthy empty source.
        out, stats = runner.merge_feed(feed(old), data, NOW)
        self.assertEqual(stats["verified"], 0)
        self.assertEqual(out["jobs"][0], old)
        conflict = job("C9202-26-0003", source="nhs_jobs_C9202", sourceJobId="987654")
        with self.assertRaisesRegex(runner.ValidationError, "conflicting NHS API"):
            runner.merge_feed(feed(), result(jobs=[incoming, conflict], source_id="nhs_jobs_C9202"), NOW)

    def test_invalid_url_host_category_id_and_missing_data_are_rejected(self):
        bad = [
            job(url="https://user:secret@beta.jobs.nhs.uk/candidate/jobadvert/C123-26-0001"),
            job(url="https://beta.jobs.nhs.uk.evil.example/candidate/jobadvert/C123-26-0001"),
            job(url="javascript:alert(1)"),
            job(url="https://beta.jobs.nhs.uk/search/"),
            job(url="https://beta.jobs.nhs.uk/candidate/jobadvert/C123-26-0001?token=secret"),
            job(cat="invented"),
            job(sourceJobId="wrong"),
            job(region="invented"),
            job(title=""),
            job(pmin=3, pmax=2),
        ]
        before = feed(legacy())
        data = result(jobs=bad)
        data.update(result("btp"))
        out, stats = runner.merge_feed(before, data, NOW)
        self.assertEqual(out["jobs"], before["jobs"])
        self.assertEqual(stats["verified"], 0)
        self.assertEqual(out["updated"], before["updated"])
        self.assertIsNone(out["publishedAt"])
        nhs = next(s for s in out["sourceHealth"]["sources"] if s["id"] == "nhs")
        self.assertEqual(nhs["status"], "partial")
        self.assertEqual(nhs["rejectedJobs"], len(bad))

    def test_partial_scan_with_verified_item_advances_feed_timestamp(self):
        old = legacy()
        out, stats = runner.merge_feed(feed(old), result(jobs=[job(title="verified"), job(cat="bad")]), NOW)
        self.assertEqual(out["jobs"][0]["title"], "verified")
        self.assertEqual(out["jobs"][0]["lastCheckedAt"], CHECKED)
        self.assertEqual(out["updated"], "19 Nov 2026")
        self.assertEqual(out["publishedAt"], "2026-11-19T22:30:00Z")
        self.assertEqual(stats["verified"], 1)
        self.assertEqual(stats["verifiedComplete"], 0)
        self.assertEqual(stats["successfulSources"], 0)
        health = next(s for s in out["sourceHealth"]["sources"] if s["id"] == "nhs")
        self.assertEqual((health["status"], health["success"], health["verifiedJobs"], health["rejectedJobs"]),
                         ("partial", False, 1, 1))

    def test_partial_failure_salvages_verified_items_but_not_closures(self):
        old = legacy(title="Old", cat="control", region="se", ptype="staff", custom="keep")
        unchanged = legacy("C123-26-0002", title="Unobserved")
        data = result(jobs=[job(title="Verified"), job("C123-26-0003")], success=False,
                      closed=[unchanged["url"]], error="timeout and secret")
        base = feed(old, unchanged)
        before = copy.deepcopy(base)
        out, stats = runner.merge_feed(base, data, NOW)
        self.assertEqual(base, before)
        self.assertEqual(out["jobs"][0]["title"], "Verified")
        for field in ("cat", "region", "ptype", "custom", "url"):
            self.assertEqual(out["jobs"][0][field], old[field])
        self.assertEqual(out["jobs"][1], unchanged)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(stats["verified"], 2)
        self.assertEqual(stats["verifiedComplete"], 0)
        self.assertEqual(out["updated"], "19 Nov 2026")
        state = next(s for s in out["sourceHealth"]["sources"] if s["id"] == "nhs")
        self.assertEqual((state["status"], state["success"], state["verifiedJobs"]), ("partial", False, 2))
        self.assertNotIn("secret", json.dumps(out))

    def test_success_with_error_is_corrupt_and_does_not_authorize_items(self):
        old = legacy()
        verified = btp_job()
        data = result(jobs=[job(title="must not update")], success=True,
                      closed=[old["url"]], error="HTTP 403 includes secret")
        data.update(result("btp", [verified]))
        out, _ = runner.merge_feed(feed(old), data, NOW)
        self.assertEqual(out["jobs"][0], old)
        self.assertEqual(next(x for x in out["sourceHealth"]["sources"] if x["id"] == "nhs")["status"], "failed")
        self.assertNotIn("secret", json.dumps(out))

    def test_invalid_only_partial_and_corrupt_success_fail_closed(self):
        base = feed(legacy())
        for data in (
            result(jobs=[], success=False, error="secret"),
            result(jobs=[job(cat="bad")], success=False, error="secret"),
            result(jobs=[job(lastCheckedAt="2026-11-19T22:30:01Z")], success=False, error="secret"),
            result(jobs=[job(sourceJobId="not-the-url-id")], success=False),
            result(jobs=[job()], success=False, source_id="unknown-source"),
            result(jobs=[job(cat="bad")]),
            result(jobs=[job()], success=True, error="contradiction"),
        ):
            with self.subTest(data=data), self.assertRaisesRegex(runner.ValidationError, "all source scans failed"):
                runner.merge_feed(base, data, NOW)

    def test_partial_still_checks_host_identity_fields_and_timestamp(self):
        bad = [job("C123-26-0002", sourceJobId="C123-26-9999"),
               job("C123-26-0003", url="https://beta.jobs.nhs.uk.evil.example/candidate/jobadvert/C123-26-0003"),
               job("C123-26-0004", lastCheckedAt="2026-11-19T22:30:01Z"),
               job("C123-26-0005", place=""), job("C123-26-0006", source="btp")]
        out, stats = runner.merge_feed(feed(), result(jobs=[job(), *bad], success=False, error="timeout"), NOW)
        self.assertEqual(len(out["jobs"]), 1)
        state = next(s for s in out["sourceHealth"]["sources"] if s["id"] == "nhs")
        self.assertEqual((state["status"], state["verifiedJobs"], state["rejectedJobs"]), ("partial", 1, 5))
        self.assertEqual(stats["verified"], 1)

    def test_unrecognized_duplicate_and_malformed_partial_sources_cannot_vouch(self):
        bad_source_results = [
            {"nhs": {"sources": [{"id": "unknown", "success": False, "jobs": [job()], "error": ""}], "closed_urls": []}},
            {"nhs": {"sources": [{"id": "nhs", "success": False, "jobs": [job()], "error": ""},
                                 {"id": "NHS", "success": False, "jobs": [job()], "error": ""}], "closed_urls": []}},
            {"nhs": {"sources": [{"id": "nhs", "success": 0, "jobs": [job()], "error": ""}], "closed_urls": []}},
            {"nhs": {"sources": [{"id": "nhs", "success": False, "jobs": [job()], "error": None}], "closed_urls": []}},
        ]
        for data in bad_source_results:
            with self.subTest(data=data), self.assertRaisesRegex(runner.ValidationError, "all source scans failed"):
                runner.merge_feed(feed(), data, NOW)

    def test_healthy_source_does_not_hide_failed_or_partial_source(self):
        old = legacy("C123-26-0002")
        data = result(jobs=[job()], success=False, closed=[old["url"]], error="secret")
        data.update(result("btp", [btp_job()]))
        data["moj"] = RuntimeError("secret")
        out, stats = runner.merge_feed(feed(old), data, NOW)
        states = {s["id"]: s for s in out["sourceHealth"]["sources"]}
        self.assertEqual(states["nhs"]["status"], "partial")
        self.assertEqual(states["btp"]["status"], "success")
        self.assertEqual(states["moj"]["status"], "failed")
        self.assertEqual((stats["verified"], stats["verifiedComplete"], stats["successfulSources"]), (2, 1, 1))
        self.assertEqual(out["jobs"][0], old)
        self.assertNotIn("secret", json.dumps(out))

    def test_all_sources_failed_throws_and_empty_success_is_safe(self):
        base = feed(legacy())
        with self.assertRaisesRegex(runner.ValidationError, "all source scans failed"):
            runner.merge_feed(base, {"nhs": RuntimeError("secret")}, NOW)
        with self.assertRaisesRegex(runner.ValidationError, "all source scans failed"):
            runner.merge_feed(base, result(jobs=[job()], source_id="unknown-source"), NOW)
        empty, stats = runner.merge_feed(base, result(jobs=[]), NOW)
        self.assertEqual(empty["jobs"], base["jobs"])
        self.assertEqual(empty["updated"], base["updated"])
        self.assertEqual(stats["verified"], 0)
        self.assertIsNone(empty["publishedAt"])
        base["publishedAt"] = "2025-01-01T12:00:00Z"
        empty, _ = runner.merge_feed(base, result(jobs=[]), NOW)
        self.assertEqual(empty["publishedAt"], base["publishedAt"])

    def test_timestamp_validation_and_no_regression(self):
        bad = ["2026-11-19", "2026-11-19T22:00:00", "2026-11-19T22:00Z",
               "2026-02-30T22:00:00Z", "2026-11-20T00:00:00Z", "not a date", None]
        data = result(jobs=[job(lastCheckedAt=x) for x in bad])
        data.update(result("btp"))
        out, _ = runner.merge_feed(feed(legacy()), data, NOW)
        self.assertNotIn("lastCheckedAt", out["jobs"][0])
        self.assertEqual(out["updated"], "01 Jan 2025")
        current = job(lastCheckedAt="2026-11-19T23:00:00+01:00")
        out, _ = runner.merge_feed(feed(legacy()), result(jobs=[current]), NOW)
        self.assertEqual(out["jobs"][0]["lastCheckedAt"], current["lastCheckedAt"])
        old = job(lastCheckedAt="2026-11-19T22:20:00Z", title="Fresher")
        out, stats = runner.merge_feed(feed(old), result(jobs=[job(title="Older")]), NOW)
        self.assertEqual(out["jobs"][0], old)
        self.assertEqual(stats["changed"], 0)
        self.assertEqual(out["updated"], "19 Nov 2026")
        # London date, rather than UTC date, controls the human-readable field.
        summer = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
        valid = job(lastCheckedAt="2026-07-01T23:00:00Z")
        out, _ = runner.merge_feed(feed(legacy()), result(jobs=[valid]), summer)
        self.assertEqual(out["updated"], "02 Jul 2026")

    def test_canonical_closure_and_failed_or_partial_source_veto(self):
        old = legacy(host="www.jobs.nhs.uk")
        fresh = job("C123-26-0002")
        close_url = job()["url"] + "?utm_source=test"
        data = result(jobs=[fresh], closed=[close_url])
        # Use 10 base jobs: one deletion is exactly 10% and allowed.
        base = feed(old, *(legacy(f"C123-26-{n:04}") for n in range(3, 12)))
        out, stats = runner.merge_feed(base, data, NOW)
        self.assertEqual(stats["removed"], 1)
        self.assertNotIn(old["url"], [j["url"] for j in out["jobs"]])
        partial = result(jobs=[fresh, job(cat="bad")], closed=[close_url])
        out, stats = runner.merge_feed(base, partial, NOW)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(out["jobs"][0], old)
        overlap = {"nhs": {"sources": [
            {"id": "nhs", "success": True, "jobs": [fresh], "error": ""},
            {"id": "www.jobs.nhs.uk", "success": False, "jobs": [], "error": "403"}],
            "closed_urls": [close_url]}}
        # beta host closure and www failure refer to the same NHS IDs. A failed
        # host-specific scan must veto deletion across the alias pair.
        out, stats = runner.merge_feed(base, overlap, NOW)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(out["jobs"][0], old)

    def test_nhs_revalidation_is_scoped_and_must_be_fully_healthy(self):
        base = feed(*(legacy(f"C123-26-{n:04}") for n in range(10)))
        revalidation = {"id": "nhs_jobs_revalidation", "success": True, "jobs": [], "error": ""}
        data = result(jobs=[job("C123-26-1000")], closed=[base["jobs"][0]["url"]])
        data["nhs"]["sources"].append(revalidation)
        out, stats = runner.merge_feed(base, data, NOW)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(next(s for s in out["sourceHealth"]["sources"]
                              if s["id"] == "nhs_jobs_revalidation")["status"], "success")
        revalidation.update(success=False, error="404 is not proof of closure")
        out, stats = runner.merge_feed(base, data, NOW)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(out["jobs"][0], base["jobs"][0])
        revalidation.update(success=True, error="", jobs=[job()])
        out, stats = runner.merge_feed(base, data, NOW)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(next(s for s in out["sourceHealth"]["sources"]
                              if s["id"] == "nhs_jobs_revalidation")["status"], "failed")

    def test_circuit_breaker_uses_base_removals_not_net_size(self):
        base = feed(*(legacy(f"C123-26-{n:04}") for n in range(100)))
        active = job("C123-26-1000")
        ten = [j["url"] for j in base["jobs"][:10]]
        out, stats = runner.merge_feed(base, result(jobs=[active], closed=ten), NOW)
        self.assertEqual(stats["removed"], 10)
        self.assertEqual(stats["output"], 91)
        eleven = [j["url"] for j in base["jobs"][:11]]
        with self.assertRaisesRegex(runner.ValidationError, "circuit"):
            runner.merge_feed(base, result(jobs=[job(f"C123-26-{n:04}") for n in range(1000, 1020)], closed=eleven), NOW)
        with self.assertRaisesRegex(runner.ValidationError, "corrupt"):
            runner.merge_feed(base, result(jobs=[active], closed=["https://evil.example/vacancy/1"]), NOW)
        with self.assertRaisesRegex(runner.ValidationError, "both open and closed"):
            runner.merge_feed(base, result(jobs=[job("C123-26-0000")], closed=[base["jobs"][0]["url"]]), NOW)


class AtomicTests(unittest.TestCase):
    def test_collector_exception_and_exit_are_isolated(self):
        def fake_import(name):
            if name.endswith("tal"):
                return types.SimpleNamespace(run=lambda: (_ for _ in ()).throw(SystemExit("secret")))
            if name.endswith("nhs"):
                raise RuntimeError("secret")
            return types.SimpleNamespace(run=lambda: {"sources": [], "closed_urls": []})
        with patch.object(Path, "is_file", return_value=True), patch.object(runner.importlib, "import_module", side_effect=fake_import):
            collected = runner.collect_adapters([])
        self.assertIsInstance(collected["tal"], SystemExit)
        self.assertIsInstance(collected["nhs"], RuntimeError)
        self.assertIsInstance(collected["btp"], dict)
        self.assertIsInstance(collected["moj"], dict)

    def test_supported_adapters_receive_only_relevant_old_direct_urls(self):
        received = {}
        def tal(*, existing_jobs=()):
            received["tal"] = existing_jobs
            return {"sources": [], "closed_urls": []}
        def nhs(*, existing_urls=()):
            received["nhs"] = existing_urls
            return {"sources": [], "closed_urls": []}
        def no_arg(name):
            def run():
                received[name] = True
                return {"sources": [], "closed_urls": []}
            return run
        modules = {"tal": tal, "nhs": nhs, "btp": no_arg("btp"), "moj": no_arg("moj")}
        tal_url = "https://asp.tal.net/vx/candidate/so/opp/123-role/en-GB"
        nhs_url = "https://www.jobs.nhs.uk/candidate/jobadvert/C123-26-0001"
        jobs = [{"url": tal_url}, {"url": nhs_url + "/?utm_source=synthetic"},
                {"url": nhs_url}, {"url": "http://beta.jobs.nhs.uk/candidate/jobadvert/C123-26-0002"},
                {"url": "https://bch.tal.net/vx/opp/123-other/en-GB"},
                {"url": "https://beta.jobs.nhs.uk.evil.example/candidate/jobadvert/C123-26-0001"},
                {"url": "https://beta.jobs.nhs.uk/candidate/jobadvert/C123-26-0001?token=secret"},
                {"url": "https://www.jobs.nhs.uk/search/jobs"}, btp_job(), {}, None]
        def fake_import(name):
            return types.SimpleNamespace(run=modules[name.rsplit(".", 1)[-1]])
        with patch.object(Path, "is_file", return_value=True), patch.object(runner.importlib, "import_module", side_effect=fake_import):
            runner.collect_adapters(jobs)
        self.assertEqual(received, {"tal": [tal_url], "nhs": [nhs_url], "btp": True, "moj": True})

    def test_optional_url_arguments_not_assumed_supported(self):
        calls = []
        def fake_import(name):
            return types.SimpleNamespace(run=lambda: calls.append(name) or {"sources": [], "closed_urls": []})
        with patch.object(Path, "is_file", return_value=True), patch.object(runner.importlib, "import_module", side_effect=fake_import):
            result_data = runner.collect_adapters([legacy()])
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(isinstance(x, dict) for x in result_data.values()))

    def test_successful_cli_writes_only_stage_and_prechecks_input(self):
        with tempfile.TemporaryDirectory() as d:
            source, output = Path(d) / "base.json", Path(d) / "stage.json"
            original = json.dumps(feed(legacy()))
            source.write_text(original, encoding="utf-8")
            stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            fake = result(jobs=[job(title="Current", lastCheckedAt=stamp)])
            with patch.object(runner, "collect_adapters", return_value=fake) as collect:
                code = runner.main(["--input", str(source), "--output", str(output)])
                self.assertEqual(code, 0)
                self.assertEqual(collect.call_count, 1)
                collect.assert_called_once_with(feed(legacy())["jobs"])
            self.assertEqual(source.read_text(), original)
            staged = json.loads(output.read_text())
            self.assertEqual(staged["jobs"][0]["title"], "Current")
            self.assertEqual(staged["jobs"][0]["lastCheckedAt"], stamp)
            with patch.object(runner, "collect_adapters") as collect:
                code = runner.main(["--input", str(source), "--output", str(source)])
                self.assertEqual(code, 1)
                collect.assert_not_called()

    def test_staging_only_atomic_and_alias_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "base.json"
            output = Path(d) / "stage.json"
            original = json.dumps(feed(legacy()))
            source.write_text(original, encoding="utf-8")
            out, _ = runner.merge_feed(json.loads(original), result(jobs=[job()]), NOW)
            runner.atomic_write(out, source, output)
            self.assertEqual(source.read_text(), original)
            self.assertEqual(json.loads(output.read_text()), out)
            with self.assertRaises(runner.ValidationError):
                runner.atomic_write(out, source, source)
            symlink = Path(d) / "alias"
            symlink.symlink_to(source)
            with self.assertRaises(runner.ValidationError):
                runner.atomic_write(out, source, symlink)
            hardlink = Path(d) / "hardlink"
            os.link(source, hardlink)
            with self.assertRaises(runner.ValidationError):
                runner.atomic_write(out, source, hardlink)
            self.assertEqual(list(Path(d).glob(".triple9-stage-*")), [])

    def test_cli_all_fail_leaves_existing_output_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            source, output = Path(d) / "base.json", Path(d) / "stage.json"
            source.write_text(json.dumps(feed(legacy())), encoding="utf-8")
            output.write_text("do not replace", encoding="utf-8")
            with patch.object(runner, "collect_adapters", return_value={"nhs": RuntimeError("secret")}):
                code = runner.main(["--input", str(source), "--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(output.read_text(), "do not replace")
            self.assertEqual(list(Path(d).glob(".triple9-stage-*")), [])

    def test_cli_partial_stages_only_verified_without_leaking_errors(self):
        with tempfile.TemporaryDirectory() as d:
            source, output = Path(d) / "base.json", Path(d) / "stage.json"
            source.write_text(json.dumps(feed(legacy())), encoding="utf-8")
            stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            data = result(jobs=[job(title="Current", lastCheckedAt=stamp)], success=False,
                          error="secret-token-in-detail-error")
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(runner, "collect_adapters", return_value=data), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(runner.main(["--input", str(source), "--output", str(output)]), 0)
            staged = json.loads(output.read_text())
            self.assertEqual(staged["jobs"][0]["title"], "Current")
            self.assertEqual(next(s for s in staged["sourceHealth"]["sources"] if s["id"] == "nhs")["status"], "partial")
            self.assertNotIn("secret-token", output.read_text() + stdout.getvalue() + stderr.getvalue())

    def test_cli_invalid_partial_or_corrupt_success_does_not_stage(self):
        with tempfile.TemporaryDirectory() as d:
            source, output = Path(d) / "base.json", Path(d) / "stage.json"
            source.write_text(json.dumps(feed(legacy())), encoding="utf-8")
            for data in (result(jobs=[job(cat="invalid")], success=False, error="secret"),
                         result(jobs=[job(cat="invalid")], success=True)):
                with patch.object(runner, "collect_adapters", return_value=data):
                    self.assertEqual(runner.main(["--input", str(source), "--output", str(output)]), 1)
                self.assertFalse(output.exists())
                self.assertEqual(list(Path(d).glob(".triple9-stage-*")), [])


class ExpiryTests(unittest.TestCase):
    def test_expired_base_job_is_dropped_and_open_ended_kept(self):
        old = job(closes="Closes 18 Nov 2026")
        today = job("C123-26-0002", title="Nurse", closes="Closes 19 Nov 2026")
        surrey = job("C123-26-0003", title="POLICE CONSTABLE & SERGEANT TRANSFEREES",
                     org="Surrey Police", closes="Open \u2013 see force website")
        out, stats = runner.merge_feed(feed(old, today, surrey), result(jobs=[]), NOW)
        titles = [item["title"] for item in out["jobs"]]
        self.assertNotIn("Paramedic", titles)
        self.assertIn("Nurse", titles)
        self.assertIn("POLICE CONSTABLE & SERGEANT TRANSFEREES", titles)
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(stats["openEnded"], 1)

    def test_job_closing_in_next_seven_days_stays(self):
        soon = job(closes="Closes 26 Nov 2026")
        out, stats = runner.merge_feed(feed(soon), result(jobs=[]), NOW)
        self.assertEqual(len(out["jobs"]), 1)
        self.assertEqual(stats["expired"], 0)

    def test_reverified_job_with_refreshed_closing_date_survives(self):
        old = job(closes="Closes 18 Nov 2026")
        current = job(closes="Closes 30 Nov 2026", lastCheckedAt="2026-11-19T22:15:00Z")
        out, stats = runner.merge_feed(feed(old), result(jobs=[current]), NOW)
        self.assertEqual(len(out["jobs"]), 1)
        self.assertEqual(out["jobs"][0]["closes"], "Closes 30 Nov 2026")
        self.assertEqual(stats["expired"], 0)

    def test_expired_new_candidate_is_not_added(self):
        candidate = job(closes="Closes 18 Nov 2026")
        out, stats = runner.merge_feed(feed(), result(jobs=[candidate]), NOW)
        self.assertEqual(out["jobs"], [])
        self.assertEqual(stats["expired"], 1)

    def test_expiry_guard_rejects_wiping_most_of_the_feed(self):
        old = feed(job(closes="Closes 18 Nov 2026"),
                   job("C123-26-0002", closes="Closes 17 Nov 2026"))
        with self.assertRaises(runner.ValidationError):
            runner.merge_feed(old, result(jobs=[]), NOW)


if __name__ == "__main__":
    unittest.main()
