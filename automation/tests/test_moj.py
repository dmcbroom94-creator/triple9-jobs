"""Offline fixture tests for the Ministry of Justice reader; no network access."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "collectors" / "moj.py"
spec = importlib.util.spec_from_file_location("moj", MODULE)
moj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(moj)


def url(jid):
    return f"{moj.BASE}/careers/JobDetail/example/{jid}"


def card(jid, title, unit=""):
    return f'''<article class="article article--result"><h3><a href="{url(jid)}">{jid} - {title}</a></h3>
    <div class="article__content--info"><p class="article__content--label">Business Unit:</p>
    <p class="paragraph">{unit}</p></div></article>'''


def search(cards, start, end, total, next_url=None):
    legend = f'{start}-{end} of {total} results' if total else '0 results'
    link = f'<a class="paginationNextLink" href="{next_url}">Next &gt;&gt;</a>' if next_url else ''
    return f'<div class="list-controls__text__legend">{legend}</div>{"".join(cards)}{link}'


def detail(jid="100", title="Probation Services Officer", unit="Probation Service", salary="£28,064 - £33,549",
           location="Bristol", region="South West", close="10-Dec-2099", approach="External", apply=True,
           extra="", publisher="Ministry of Justice"):
    fields = {"Salary": salary, "Vacancy Approach": approach, "Location": location, "Region": region,
              "Closing Date": close, "Business Unit": unit, "Job ID": jid}
    rows = ''.join(f'<div class="article__content__view__field"><div class="article__content__view__field__label">{k}</div>'
                   f'<div class="article__content__view__field__value">{v}</div></div>' for k, v in fields.items())
    link = f'<a class="button" href="/careers/ApplicationMethods?jobId={jid}&amp;record={jid}">Apply</a>' if apply else ''
    return f'<h1>{publisher}</h1><h2 class="banner__text__title">{jid} - {title}</h2>{rows}{link}{extra}'


def field(label, content):
    return f'''<div class="article__content__view__field"><div class="article__content__view__field__label">{label}</div>
    <div class="article__content__view__field__value">{content}</div></div>'''


def public_notice():
    # Adapted from the two observed MoJ detail layouts, not a whole page copy.
    return '<p>This job is broadly open to the following groups:</p><ul><li>UK nationals</li><li>nationals of the Republic of Ireland</li></ul>'


def campaign_detail(jid='23951'):
    title = '202611: Prison Officer - HMP Woodhill'
    html = detail(jid=jid, approach='', location='Milton Keynes', extra=field('Job Description', public_notice()))
    html = html.replace(f'{jid} - Probation Services Officer</h2>', title + '</h2>')
    metadata = (f'<meta property="og:title" content="{title}"><meta property="og:url" content="{url(jid)}">'
                f'<link rel="canonical" href="{url(jid)}">')
    return metadata + html


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.visits = []

    def get(self, requested):
        self.visits.append(requested)
        value = self.pages[requested]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, tuple) else (value, requested)


class FakeResponse:
    def __init__(self, status=200, content='', headers=None):
        self.status_code = status
        self.body = content.encode('utf-8')
        self.headers = headers or {}
        self.encoding = 'utf-8'

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.headers = {}
        self.visits = []

    def get(self, url, **kwargs):
        self.visits.append(url)
        return self.pages[url]

    def close(self):
        pass


class RobotsTests(unittest.TestCase):
    def test_wildcards_anchors_allow_tie_and_queries(self):
        rules = moj.Robots('''User-agent: *
Allow: /$
Allow: /careers
Disallow: /careers/*qtvc=
Disallow: /private
Allow: /private/public
''')
        self.assertTrue(rules.allowed(moj.SEARCH))
        self.assertFalse(rules.allowed(moj.SEARCH + '?x=1&qtvc=abc'))
        self.assertFalse(rules.allowed(moj.BASE + '/private/secret'))
        self.assertTrue(rules.allowed(moj.BASE + '/private/public'))

    def test_specific_agent_and_missing_groups(self):
        rules = moj.Robots('User-agent: *\nDisallow: /\nUser-agent: Triple9CareersVacancyReader\nAllow: /careers\n')
        self.assertTrue(rules.allowed(moj.SEARCH))
        with self.assertRaises(moj.Incomplete):
            moj.Robots('garbled garbage')

    def test_disallow_encoded_query(self):
        r = moj.Robots('User-agent: *\nDisallow: /careers/*qtvc=\n')
        self.assertFalse(r.allowed(moj.SEARCH + '?%71tvc=abc'))
        r = moj.Robots('User-agent: *\nDisallow: /careers/a%2Fb\n')
        self.assertFalse(r.allowed(moj.BASE + '/careers/a%2fb'))
        self.assertTrue(r.allowed(moj.BASE + '/careers/a/b'))

    def test_rate_directives(self):
        r = moj.Robots('User-agent: *\nAllow: /careers\nCrawl-delay: 3\nRequest-rate: 1/8\n')
        self.assertEqual(r.delay, 8)
        with self.assertRaises(moj.Incomplete):
            moj.Robots('User-agent: *\nCrawl-delay: bananas\n')


class TransportTests(unittest.TestCase):
    def run_with(self, session):
        with patch.object(moj.requests, 'Session', return_value=session), patch.object(moj.time, 'sleep'):
            return moj.run(max_pages=1, max_details=0, max_seconds=5)

    def test_unavailable_robots_stops_source(self):
        session = FakeSession({moj.ROBOTS: FakeResponse(status=403)})
        result = self.run_with(session)
        self.assertFalse(result['sources'][0]['success'])
        self.assertIn('HTTP 403', result['sources'][0]['error'])
        self.assertEqual(session.visits, [moj.ROBOTS])
        self.assertEqual(result['closed_urls'], [])

    def test_robots_disallow_prevents_search_request(self):
        session = FakeSession({moj.ROBOTS: FakeResponse(content='User-agent: *\nDisallow: /careers\n')})
        result = self.run_with(session)
        self.assertFalse(result['sources'][0]['success'])
        self.assertIn('robots.txt disallows', result['sources'][0]['error'])
        self.assertEqual(session.visits, [moj.ROBOTS])

    def test_foreign_redirect_is_not_followed(self):
        session = FakeSession({moj.ROBOTS: FakeResponse(content='User-agent: *\nAllow: /\n'),
                               moj.SEARCH: FakeResponse(status=302, headers={'Location': 'https://evil.example/'})})
        result = self.run_with(session)
        self.assertFalse(result['sources'][0]['success'])
        self.assertIn('official', result['sources'][0]['error'])
        self.assertEqual(session.visits, [moj.ROBOTS, moj.SEARCH])

    def test_invalid_and_unbounded_limits_do_not_issue_requests(self):
        invalid = ({'max_pages': 0}, {'max_pages': 201}, {'max_pages': 1.5}, {'max_pages': True},
                   {'max_details': -1}, {'max_details': 601}, {'max_details': False},
                   {'max_seconds': float('inf')}, {'max_seconds': float('nan')}, {'max_seconds': 1801},
                   {'max_seconds': 10**1000},
                   {'interval': 0}, {'interval': 61}, {'interval': float('nan')},
                   {'timeout': None}, {'timeout': (5,)}, {'timeout': (5, float('inf'))},
                   {'timeout': (0, 5)}, {'timeout': (5, 61)})
        with patch.object(moj.requests, 'Session') as session:
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    moj.run(**kwargs)
            session.assert_not_called()


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 22, tzinfo=timezone.utc)

    def parse(self, html, jid='100'):
        return moj._detail(html, url(jid), jid, self.now)

    def test_fields_are_from_detail_and_verified(self):
        status, job = self.parse(detail())
        self.assertEqual(status, 'open')
        self.assertEqual(job['title'], '100 - Probation Services Officer')
        self.assertEqual(job['pay'], '£28,064 - £33,549')
        self.assertEqual(job['place'], 'Bristol')
        self.assertEqual(job['region'], 'sw')
        self.assertEqual(job['closes'], 'Closes 10-Dec-2099')
        self.assertEqual(job['org'], 'Ministry of Justice')
        self.assertEqual(job['sourceJobId'], '100')
        self.assertEqual(job['lastCheckedAt'], '2026-11-19T22:00:00Z')
        self.assertEqual(job['url'], url('100'))
        self.assertFalse({'jroles', 'hours', 'pmin', 'pmax'} & job.keys())

    def test_no_date_and_pay_only_after_positive_apply(self):
        _, job = self.parse(detail(close='', salary=''))
        self.assertEqual(job['closes'], 'Open – see advert')
        self.assertEqual(job['pay'], 'See advert')
        self.assertEqual(self.parse(detail(salary='N/A'))[1]['pay'], 'See advert')
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(close='', apply=False))
        html = detail().replace('>Closing Date</div>', '>Other Date</div>')
        with self.assertRaises(moj.Incomplete):
            self.parse(html)

    def test_close_and_ambiguous_or_expired_not_closed(self):
        state, _ = self.parse(detail(extra='<p>This vacancy is now closed.</p>'))
        self.assertEqual(state, 'closed')
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(close='01-Jan-2020'))
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(close='soon'))

    def test_no_generic_closure_from_prose(self):
        status, _ = self.parse(detail(extra='<p>A vacancy may close early if enough applications are received.</p>'))
        self.assertEqual(status, 'open')

    def test_inactive_or_wrong_apply_is_not_positive_open_evidence(self):
        for anchor in ('<a href="/careers/ApplicationMethods?jobId=999">Apply</a>',
                       '<a aria-disabled="true" href="/careers/ApplicationMethods?jobId=100">Apply</a>',
                       '<div hidden><a href="/careers/ApplicationMethods?jobId=100">Apply</a></div>',
                       '<div style="display:none"><a href="/careers/ApplicationMethods?jobId=100">Apply</a></div>',
                       '<div aria-hidden="true"><a href="/careers/ApplicationMethods?jobId=100">Apply</a></div>',
                       '<a href="/careers/ApplicationMethods?jobId=100"><span hidden>Apply</span></a>',
                       '<a href="https://evil.example/careers/ApplicationMethods?jobId=100">Apply</a>'):
            with self.subTest(anchor=anchor), self.assertRaises(moj.Incomplete):
                self.parse(detail(apply=False, extra=anchor))

    def test_id_brand_location_and_approach(self):
        for html in (detail(jid='101'), detail(publisher='Other'), detail(location='')):
            with self.subTest(html=html[:100]), self.assertRaises(moj.Incomplete):
                self.parse(html)
        self.assertEqual(self.parse(detail(approach='Internal'))[0], 'skip')
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(approach=''))
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(approach='Internal and External'))

    def test_campaign_title_with_identity_and_public_evidence(self):
        status, job = self.parse(campaign_detail(), '23951')
        self.assertEqual(status, 'open')
        self.assertEqual(job['title'], '202611: Prison Officer - HMP Woodhill')
        self.assertEqual(job['sourceJobId'], '23951')
        self.assertEqual(job['place'], 'Milton Keynes')

    def test_campaign_title_requires_independent_matching_metadata(self):
        html = campaign_detail()
        variants = (
            html.replace('202611: Prison Officer - HMP Woodhill</h2>', '202611: Prison Officer - HMP Other</h2>'),
            html.replace('content="202611: Prison Officer - HMP Woodhill"', 'content="Different title"'),
            html.replace(f'<link rel="canonical" href="{url("23951")}">', ''),
            html.replace(f'content="{url("23951")}"', 'content=""'),
            html.replace(f'href="{url("23951")}"', 'href=""', 1),
            html.replace(f'href="{url("23951")}"', f'href="{url("999")}"', 1),
            html.replace(f'content="{url("23951")}"', 'content="https://evil.example/23951"'),
            html + f'<meta property="og:url" content="{url("23951")}">',
            html.replace('202611:', '202613:'),
            html.replace('202611:', '999:'),
            html.replace('>Job ID</div>', '>Other ID</div>'),
            html.replace('>23951</div>', '>999</div>'),
            html.replace('<h2 class="banner__text__title">', '<h2 class="banner__text__title" hidden>'),
            html + '<h2 class="banner__text__title">another heading</h2>',
        )
        for variant in variants:
            with self.subTest(variant=variant[:120]), self.assertRaises(moj.Incomplete):
                self.parse(variant, '23951')

    def test_public_eligibility_from_both_observed_fields(self):
        for label in ('Job Description', 'Additional Information'):
            with self.subTest(label=label):
                html = detail(approach='', extra=field(label, public_notice()))
                self.assertEqual(self.parse(html)[0], 'open')
        bullet = '<p>This job is broadly open to the following groups:</p><p>· UK nationals</p>'
        self.assertEqual(self.parse(detail(approach='', extra=field('Additional Information', bullet)))[0], 'open')
        html = detail(jid='24132', title='Operational Support Grade - HMP Hollesley',
                      approach='', location='Ipswich', extra=field('Additional Information', public_notice()))
        self.assertEqual(self.parse(html, '24132')[0], 'open')

    def test_nationality_evidence_not_generic_hidden_or_contradictory(self):
        bad = (
            field('Other', public_notice()),
            field('Job Description', '<p>We encourage applications from everyone.</p>'),
            field('Job Description', '<p>This job is broadly open to the following groups: internal staff</p>'),
            field('Job Description', '<p>This job is NOT broadly open to the following groups: UK nationals</p>'),
            field('Job Description', '<p>It is not true that this job is broadly open to the following groups:</p><p>UK nationals</p>'),
            field('Job Description', '<div hidden>' + public_notice() + '</div>'),
            field('Job Description', public_notice().replace('UK nationals', '<span hidden>UK nationals</span>')),
            field('Job Description', public_notice() + '<p>This vacancy is only open to current civil servants.</p>'),
            field('Job Description', public_notice() + '<p>Only existing staff may apply.</p>'),
            field('Additional Information', public_notice() + '<p>Internal candidates only.</p>'),
            field('Additional Information', public_notice() + '<p>This vacancy is not open to external applicants.</p>'),
        )
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(moj.Incomplete):
                self.parse(detail(approach='', extra=extra))
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(approach='External', extra=field('Eligibility', 'Internal applicants only')))
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(approach='', extra=field('Job Description', public_notice()) + field('Who Can Apply', 'Civil servants')))
        for approach in ('Not External', 'Internal and External', 'Other'):
            with self.subTest(approach=approach), self.assertRaises(moj.Incomplete):
                self.parse(detail(approach=approach, extra=field('Job Description', public_notice())))

    def test_public_evidence_does_not_replace_other_required_checks(self):
        for html in (detail(approach='', apply=False, extra=field('Job Description', public_notice())),
                     detail(approach='', location='', extra=field('Job Description', public_notice())),
                     detail(approach='', close='01-Jan-2020', extra=field('Job Description', public_notice())),
                     detail(approach='', publisher='Other', extra=field('Job Description', public_notice()))):
            with self.subTest(html=html[:100]), self.assertRaises(moj.Incomplete):
                self.parse(html)

    def test_closed_page_needs_independent_job_identity_and_brand(self):
        for html in ('<p>This job is now closed</p>',
                     detail(publisher='Other', extra='<p>This job is now closed</p>'),
                     detail(extra='<p>This job is now closed</p>').replace('>Job ID</div>', '>Other ID</div>'),
                     detail(extra='<p>This job is now closed</p>').replace('>Job ID</div>', '>Job ID</div><div hidden>')):
            with self.subTest(html=html[:100]), self.assertRaises(moj.Incomplete):
                self.parse(html)

    def test_strict_classification(self):
        self.assertEqual(self.parse(detail(title='Prison Officer'))[1]['cat'], 'justice')
        self.assertEqual(self.parse(detail(title='Control Room Operator', unit='HMP Bristol'))[1]['cat'], 'control')
        self.assertEqual(self.parse(detail(title='Control Room Operator', unit='Courts'))[1]['cat'], 'control')
        self.assertEqual(self.parse(detail(title='Control Officer', unit='Courts'))[0], 'skip')
        self.assertEqual(self.parse(detail(title='Case Administrator', unit='NPS South West'))[1]['cat'], 'justice')
        self.assertEqual(self.parse(detail(title='Portfolio Manager', unit='HMPPS'))[0], 'skip')
        self.assertEqual(self.parse(detail(title='Talent Pool - HMPPS', unit='HMPPS'))[0], 'skip')
        self.assertEqual(self.parse(detail(title='Prison Officer - Expression of Interest'))[0], 'skip')

    def test_unknown_region_and_no_guessed_region(self):
        self.assertEqual(self.parse(detail(region='Nationwide'))[1]['region'], 'uk')

    def test_reject_foreign_url_and_path(self):
        self.assertIsNone(moj._job_id('https://jobs.justice.gov.uk.evil.example/careers/JobDetail/a/100'))
        self.assertIsNone(moj._job_id(moj.BASE + '/careers/JobDetail/a/100/attachmentPreview'))
        with self.assertRaises(moj.Incomplete):
            moj._search_page(search([card('100', 'Prison Officer')], 1, 1, 2, 'https://evil.example/next'), moj.SEARCH)

    def test_hidden_apply_should_not_count(self):
        html = detail(apply=False, extra='<div hidden><a href="/careers/ApplicationMethods?jobId=100">Apply</a></div>')
        with self.assertRaises(moj.Incomplete):
            self.parse(html)

    def test_conflicting_fields_are_rejected(self):
        extra = '<div class="article__content__view__field"><div class="article__content__view__field__label">Job ID</div><div class="article__content__view__field__value">101</div></div>'
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(extra=extra))

    def test_hidden_closure_and_mismatched_closed_job(self):
        html = detail(extra='<p style="display: none">This job is now closed.</p>')
        self.assertEqual(self.parse(html)[0], 'open')
        html = detail(extra='<div role="alert"><span hidden>This job is now closed.</span></div>')
        self.assertEqual(self.parse(html)[0], 'open')
        with self.assertRaises(moj.Incomplete):
            self.parse(detail(jid='101', extra='<p>This job is now closed.</p>'))


class CollectionTests(unittest.TestCase):
    def test_all_real_next_links_dedup_and_detail_fields(self):
        page2 = moj.SEARCH + '/?jobRecordsPerPage=2&jobOffset=2'
        first = search([card('100', 'Probation Officer'), card('200', 'Portfolio Manager', 'HMPPS')], 1, 2, 3, page2)
        second = search([card('300', 'Case Administrator', 'NPS South West')], 3, 3, 3)
        client = FakeClient({moj.SEARCH: first, page2: second, url('100'): detail(),
                             url('300'): detail(jid='300', title='Case Administrator', unit='NPS South West')})
        result = moj._collect(client, max_pages=10, max_details=10)
        source = result['sources'][0]
        self.assertTrue(source['success'], source['error'])
        self.assertEqual([j['sourceJobId'] for j in source['jobs']], ['100', '300'])
        self.assertNotIn(url('200'), client.visits)
        self.assertIn(page2, client.visits)
        self.assertEqual(result['closed_urls'], [])

    def test_page_limit_is_partial_but_keeps_verified_jobs(self):
        page2 = moj.SEARCH + '/?jobRecordsPerPage=1&jobOffset=1'
        client = FakeClient({moj.SEARCH: search([card('100', 'Probation Officer')], 1, 1, 2, page2),
                             url('100'): detail()})
        result = moj._collect(client, max_pages=1, max_details=10)
        self.assertFalse(result['sources'][0]['success'])
        self.assertIn('page limit', result['sources'][0]['error'])
        self.assertEqual(len(result['sources'][0]['jobs']), 1)

    def test_partial_page_structural_failure_and_missing_next(self):
        for html in (search([], 1, 1, 1), search([card('100', 'Probation Officer')], 1, 1, 2),
                     '<html>maintenance</html>'):
            result = moj._collect(FakeClient({moj.SEARCH: html}), max_pages=3, max_details=2)
            self.assertFalse(result['sources'][0]['success'])
            self.assertEqual(result['closed_urls'], [])

    def test_duplicate_id_is_not_a_complete_scan(self):
        page2 = moj.SEARCH + '/?jobOffset=1'
        client = FakeClient({moj.SEARCH: search([card('100','Probation Officer')],1,1,2,page2),
                             page2: search([card('100','Probation Officer')],2,2,2), url('100'): detail()})
        result = moj._collect(client, max_pages=5, max_details=5)
        self.assertFalse(result['sources'][0]['success'])
        self.assertIn('duplicate', result['sources'][0]['error'])
        self.assertEqual(len(result['sources'][0]['jobs']), 1)

    def test_changed_totals_and_loop_are_incomplete(self):
        page2 = moj.SEARCH + '/?jobOffset=1'
        client = FakeClient({moj.SEARCH: search([card('100', 'Probation Officer')],1,1,3,page2),
                             page2: search([card('200', 'Probation Officer')],2,2,2), url('100'):detail()})
        self.assertIn('changed', moj._collect(client, max_pages=3, max_details=5)['sources'][0]['error'])
        client = FakeClient({moj.SEARCH: search([card('100','Probation Officer')],1,1,3,moj.SEARCH), url('100'): detail()})
        self.assertIn('loop', moj._collect(client, max_pages=3, max_details=5)['sources'][0]['error'])

    def test_closed_only_with_explicit_marker_not_404(self):
        page = search([card('100', 'Prison Officer'), card('200', 'Probation Officer')],1,2,2)
        client = FakeClient({moj.SEARCH: page, url('100'): detail(extra='<p>This job is now closed</p>'),
                             url('200'): moj.Incomplete('HTTP 404 at ' + url('200'))})
        result = moj._collect(client, max_pages=3, max_details=3)
        self.assertFalse(result['sources'][0]['success'])
        self.assertEqual(result['closed_urls'], [url('100')])
        self.assertEqual(result['sources'][0]['jobs'], [])

    def test_detail_limit_and_block_stop(self):
        page = search([card('100', 'Prison Officer'), card('200', 'Probation Officer')],1,2,2)
        client = FakeClient({moj.SEARCH: page, url('100'): moj.Incomplete('HTTP 403 at source'), url('200'): detail(jid='200')})
        result = moj._collect(client, max_pages=3, max_details=3)
        self.assertFalse(result['sources'][0]['success'])
        self.assertNotIn(url('200'), client.visits)
        self.assertEqual(result['closed_urls'], [])
        client = FakeClient({moj.SEARCH: page})
        result = moj._collect(client, max_pages=3, max_details=0)
        self.assertIn('detail limit', result['sources'][0]['error'])

    def test_zero_results_only_with_explicit_counter(self):
        result = moj._collect(FakeClient({moj.SEARCH:search([],0,0,0)}), max_pages=2, max_details=2)
        self.assertTrue(result['sources'][0]['success'])
        self.assertEqual(result['sources'][0]['jobs'], [])

    def test_unknown_context_is_checked_not_silently_skipped(self):
        page = search([card('100', 'Case Administrator')], 1, 1, 1)
        client = FakeClient({moj.SEARCH: page, url('100'): detail(title='Case Administrator', unit='')})
        result = moj._collect(client, max_pages=2, max_details=2)
        self.assertIn(url('100'), client.visits)
        self.assertFalse(result['sources'][0]['success'])
        self.assertIn('business unit missing', result['sources'][0]['error'])
        client = FakeClient({moj.SEARCH: page, url('100'): detail(title='Case Administrator', unit='Probation Service')})
        result = moj._collect(client, max_pages=2, max_details=2)
        self.assertTrue(result['sources'][0]['success'])
        self.assertEqual(len(result['sources'][0]['jobs']), 1)

    def test_unknown_detail_does_not_erase_successful_jobs_or_claim_completion(self):
        page = search([card('100', 'Prison Officer'), card('200', 'Probation Officer')], 1, 2, 2)
        client = FakeClient({moj.SEARCH: page, url('100'): '<html>Unexpected layout</html>',
                             url('200'): detail(jid='200')})
        result = moj._collect(client, max_pages=2, max_details=2)
        self.assertFalse(result['sources'][0]['success'])
        self.assertIn('job 100:', result['sources'][0]['error'])
        self.assertEqual([j['sourceJobId'] for j in result['sources'][0]['jobs']], ['200'])
        self.assertEqual(result['closed_urls'], [])

    def test_block_during_pagination_stops_all_further_requests(self):
        page2 = moj.SEARCH + '/?jobOffset=1'
        page = search([card('100', 'Prison Officer')], 1, 1, 2, page2)
        client = FakeClient({moj.SEARCH: page, page2: moj.Incomplete('HTTP 429 at search'), url('100'): detail()})
        result = moj._collect(client, max_pages=2, max_details=2)
        self.assertFalse(result['sources'][0]['success'])
        self.assertEqual(client.visits, [moj.SEARCH, page2])
        self.assertEqual(result['sources'][0]['jobs'], [])


if __name__ == '__main__':
    unittest.main()
