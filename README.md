# Triple 9 Careers

- `jobs.json` is the live vacancy feed used by the homepage and jobs pages. The daily GitHub Actions workflow is configured to refresh a selected set of official sources at 05:10 UK time. Other employers' listings are not yet automatically refreshed and can be older. See [`automation/README.md`](automation/README.md) for coverage, source status and safeguards.
- `site/` contains the website. The existing cPanel task copies this folder into `public_html` approximately every 15 minutes; this is separate from collecting vacancies.
