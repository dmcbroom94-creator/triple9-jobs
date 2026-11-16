# Triple 9 Careers

- `jobs.json` – live job list, updated automatically every morning. The website loads it from here.
- `site/` – the full website. The hosting copies this folder into `public_html` every 15 minutes (cPanel cron job), so changes pushed here go live automatically.
