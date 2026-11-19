import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('tal', Path(__file__).resolve().parents[1] / 'collectors' / 'tal.py')
tal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tal)

ROBOTS = 'User-agent: *\nDisallow: /*/ats/\nCrawl-delay: 10\n'
BASE = 'https://asp.tal.net/vx/lang-en-GB/mobile-0/appcentre-13/brand-3/candidate/so/pm/6/pl/24/opp/'
URL1 = BASE + '111-Police-Constable-Transferee/en-GB'
URL2 = BASE + '222-Emergency-Call-Handler-Dispatcher/en-GB'
OLD = BASE + '999-Previous-Job/en-GB'
PAGE2 = tal.BOARD + '?page=2'


def row(oid, title, url):
    return '<tr class="search_res details_row" data-oppid="%s"><td><a class="subject" href="%s">%s</a></td></tr>' % (oid, url, title)


def board(rows, next_url=None, extra=''):
    nxt = '<div class="pagination"><a rel="next" href="%s">Next</a></div>' % next_url if next_url else ''
    return '<html><div class="search-results"><div id="results_list"><table class="solr_search_list">%s</table>%s</div>%s</div><span>End of search results</span></html>' % (''.join(rows), extra, nxt)


def field(name, value):
    return '<div class="form-group"><span class="hform_lbl_text">%s</span><div class="form_value"><div class="form-control-static">%s</div></div></div>' % (name, value)


def detail(title='Police Constable Transferee', oid='111', closing='31/12/2099, 23:55', apply=True, salary='£32,256 - £52,014', location='Forcewide'):
    date_field = field('Closing date', closing) if closing is not None else ''
    submit = '<form class="apply-form" action="%s%s/apply/en-GB"><input type="submit" value="Apply"></form>' % (BASE, oid) if apply else ''
    return '<html><div id="vac_desc"><h1>%s</h1>%s%s%s%s</div>%s</html>' % (title, field('Location', location), field('Salary', salary) if salary is not None else '', field('Rank', 'Constable'), date_field, submit)


class Response:
    def __init__(self, body, status=200):
        self.text = body
        self.content = body.encode()
        self.status_code = status
        self.headers = {'Content-Type': 'text/html'}


class Session:
    def __init__(self, mapping):
        self.mapping, self.calls, self.headers = mapping, [], {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.mapping.get(url)
        if value is None:
            raise OSError('unexpected fixture URL: ' + url)
        if isinstance(value, BaseException):
            raise value
        return value if isinstance(value, Response) else Response(value)


def fixtures():
    return {tal.ROBOTS: ROBOTS, tal.BOARD: board([row('111', 'Police Constable Transferee', URL1)]), URL1: detail()}


def scan(mapping, existing=(), **kwargs):
    session = Session(mapping)
    return tal.run(existing, session=session, sleep=lambda _: None, **kwargs), session


class TalTests(unittest.TestCase):
    def test_verified_discovery_uses_detail_and_serializes(self):
        r, sess = scan(fixtures())
        self.assertTrue(r['sources'][0]['success'], r)
        job = r['sources'][0]['jobs'][0]
        self.assertEqual(job['pay'], '£32,256 - £52,014')
        self.assertEqual(job['closes'], 'Closes 31 Dec 2099')
        self.assertEqual((job['cat'], job['ptype'], job['sourceJobId']), ('police', 'officer', '111'))
        self.assertEqual(job['region'], 'uk')
        self.assertTrue(job['lastCheckedAt'].endswith('Z'))
        self.assertFalse({'ranks', 'hours', 'pmin', 'pmax'} & set(job))
        self.assertEqual(r['closed_urls'], [])
        self.assertEqual([x[0] for x in sess.calls], [tal.ROBOTS, tal.BOARD, URL1])
        self.assertTrue(all(not args['allow_redirects'] for _, args in sess.calls))
        json.dumps(r)

    def test_pagination_and_duplicate_ids(self):
        f = fixtures()
        f[tal.BOARD] = board([row('111', 'Police Constable Transferee', URL1)], PAGE2)
        f[PAGE2] = board([row('111', 'Police Constable Transferee', URL1), row('222', 'Emergency Call Handler / Dispatcher', URL2)])
        f[URL2] = detail('Emergency Call Handler / Dispatcher', '222')
        r, session = scan(f)
        self.assertTrue(r['sources'][0]['success'], r)
        self.assertEqual([x['sourceJobId'] for x in r['sources'][0]['jobs']], ['111', '222'])
        self.assertEqual(r['sources'][0]['jobs'][1]['cat'], 'control')
        self.assertNotIn('ptype', r['sources'][0]['jobs'][1])
        self.assertEqual([u for u, _ in session.calls].count(URL1), 1)

    def test_pagination_failure_discards_partial(self):
        f = fixtures()
        f[tal.BOARD] = board([row('111', 'Police Constable Transferee', URL1)], PAGE2)
        f[PAGE2] = Response('denied', 403)
        r, _ = scan(f)
        self.assertFalse(r['sources'][0]['success'])
        self.assertEqual(r['sources'][0]['jobs'], [])
        self.assertIn('403', r['sources'][0]['error'])

    def test_loop_and_unknown_pagination_are_failures(self):
        f = fixtures(); f[tal.BOARD] = board([row('111', 'Police Constable Transferee', URL1)], tal.BOARD)
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])
        f[tal.BOARD] = board([row('111', 'Police Constable Transferee', URL1)]) + '<div class="pagination"><button>Next</button></div>'
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])

    def test_mismatched_count_or_layout_fail(self):
        f = fixtures(); f[tal.BOARD] = board([row('111', 'Police Constable Transferee', URL1)], extra='Showing 1 to 1 of 2')
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])
        f[tal.BOARD] = board([row('111', 'Police Constable Transferee', URL1)]).replace('End of search results', 'Results')
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])
        f[tal.BOARD] = '<html>captcha</html>'
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])

    def test_http_200_and_future_date_are_not_open(self):
        f = fixtures(); f[URL1] = detail(apply=False)
        r, _ = scan(f)
        self.assertFalse(r['sources'][0]['success'])
        self.assertIn('open application', r['sources'][0]['error'])
        self.assertEqual(r['sources'][0]['jobs'], [])

    def test_disabled_apply_is_not_open(self):
        f = fixtures(); f[URL1] = detail().replace('type="submit"', 'type="submit" disabled')
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])

    def test_no_date_only_when_open(self):
        f = fixtures(); f[URL1] = detail(closing=None, salary=None)
        r, _ = scan(f)
        self.assertTrue(r['sources'][0]['success'], r)
        j = r['sources'][0]['jobs'][0]
        self.assertEqual(j['closes'], 'Open – see advert')
        self.assertEqual(j['pay'], 'See advert')

    def test_bad_or_past_dates_fail(self):
        for bad in ('31/02/2099, 23:55', 'some future day', '01/01/2000'):
            f = fixtures(); f[URL1] = detail(closing=bad)
            r, _ = scan(f)
            self.assertFalse(r['sources'][0]['success'], bad)
            self.assertFalse(r['sources'][0]['jobs'])

    def test_explicit_closed_existing_only(self):
        f = fixtures(); f[OLD] = '<html><p>Sorry, this vacancy has now closed.</p></html>'
        r, _ = scan(f, [OLD, OLD, 'https://evil.invalid/opp/999'])
        self.assertTrue(r['sources'][0]['success'], r)
        self.assertEqual(r['closed_urls'], [OLD])

    def test_timeout_or_404_never_closes(self):
        for failure in (Response('not found', 404), Response('forbidden', 403), TimeoutError('timeout')):
            f = fixtures(); f[OLD] = failure
            r, _ = scan(f, [OLD])
            self.assertFalse(r['sources'][0]['success'])
            self.assertEqual(r['closed_urls'], [])
            self.assertEqual(r['sources'][0]['jobs'], [])

    def test_general_early_close_warning_does_not_mark_closed(self):
        f = fixtures(); f[OLD] = '<html><p>We reserve the right to close this vacancy early.</p></html>'
        r, _ = scan(f, [OLD]); self.assertFalse(r['sources'][0]['success']); self.assertEqual(r['closed_urls'], [])

    def test_robots_disallow_and_delay(self):
        f = fixtures(); f[tal.ROBOTS] = 'User-agent: *\nDisallow: /vx/\nCrawl-delay: 10\n'
        r, sess = scan(f)
        self.assertFalse(r['sources'][0]['success'])
        self.assertEqual(len(sess.calls), 1)
        self.assertIn('robots', r['sources'][0]['error'])
        rules, delay = tal._robots_rules('User-agent: *\nDisallow: /*/ats/\nCrawl-delay: 17\n')
        self.assertEqual(delay, 17)
        self.assertFalse(tal._allowed('https://asp.tal.net/vx/ats/private', rules))
        self.assertTrue(tal._allowed(tal.BOARD, rules))

    def test_reject_unsafe_and_redirects(self):
        for unsafe in ('https://asp.tal.net.evil.test/vx/a', 'http://asp.tal.net/vx/a', 'https://asp.tal.net@evil.test/vx/a', 'https://asp.tal.net/vx/%2e%2e/private'):
            with self.assertRaises(tal.SourceError): tal._url(unsafe)
        f = fixtures(); f[tal.BOARD] = Response('', 302)
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])

    def test_conflicting_or_forged_ids_and_titles_fail(self):
        f = fixtures(); f[tal.BOARD] = board([row('333', 'Police Constable Transferee', URL1)])
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])
        f = fixtures(); f[URL1] = detail(title='Different Role')
        r, _ = scan(f); self.assertFalse(r['sources'][0]['success'])

    def test_ambiguous_roles_are_skipped(self):
        f = fixtures(); f[tal.BOARD] = board([row('111', 'Police Constable Volunteer', URL1)])
        r, session = scan(f)
        self.assertTrue(r['sources'][0]['success']); self.assertEqual(r['sources'][0]['jobs'], [])
        self.assertEqual(len(session.calls), 2)

    def test_officer_title_without_officer_rank_is_skipped(self):
        f = fixtures(); f[URL1] = detail().replace(field('Rank', 'Constable'), field('Rank', 'Staff'))
        r, _ = scan(f)
        self.assertTrue(r['sources'][0]['success']); self.assertEqual(r['sources'][0]['jobs'], [])

    def test_detective_sergeant_requires_sergeant_rank(self):
        url = BASE + '2164-Detective-Sergeant/en-GB'
        f = {tal.ROBOTS: ROBOTS, tal.BOARD: board([row('2164', 'Detective Sergeant', url)]), url: detail('Detective Sergeant', '2164').replace(field('Rank', 'Constable'), field('Rank', 'Sergeant'))}
        r, _ = scan(f)
        self.assertTrue(r['sources'][0]['success'], r)
        self.assertEqual(r['sources'][0]['jobs'][0]['sourceJobId'], '2164')
        f[url] = detail('Detective Sergeant', '2164')
        r, _ = scan(f)
        self.assertEqual(r['sources'][0]['jobs'], [])

    def test_unrecognised_robots_response_fails_closed(self):
        f = fixtures(); f[tal.ROBOTS] = '<html>temporary access challenge</html>'
        r, session = scan(f)
        self.assertFalse(r['sources'][0]['success']); self.assertEqual(len(session.calls), 1)

    def test_detail_request_limit_marks_incomplete(self):
        f = fixtures(); f[tal.BOARD] = board([row('111', 'Police Constable Transferee', URL1), row('222', 'Emergency Call Handler / Dispatcher', URL2)])
        r, _ = scan(f, max_detail_requests=1)
        self.assertFalse(r['sources'][0]['success']); self.assertEqual(r['sources'][0]['jobs'], [])

    def test_missing_dependencies_report_error(self):
        old = tal.requests
        try:
            tal.requests = None
            r = tal.run()
            self.assertFalse(r['sources'][0]['success'])
            self.assertIn('dependency', r['sources'][0]['error'])
        finally:
            tal.requests = old


if __name__ == '__main__':
    unittest.main()
