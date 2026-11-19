# Managing featured offers

Edit `site/featured-offers.json`. This file is copied to the live site with the rest of `site/`; the list is empty until real, approved offers are added. Keep valid JSON with a top-level `offers` array. Each array entry is an object with these fields:

- `retailer`, `title`, `description` — required, non-empty text.
- `code`, `eligibility`, `terms` — optional text; omit them if not applicable.
- `url` — optional absolute `https://` or `http://` link to the offer. Other protocols and URLs containing credentials are rejected.
- `sponsored` — optional Boolean (`true` or `false`). Set **`true` for every paid advertisement**; this displays an Advertisement label and marks its link as sponsored. Omit or set to `false` only for unpaid offers.
- `start` and `expiry` — optional calendar dates in `YYYY-MM-DD` format. Offers appear from the start date through the expiry date, inclusive, using the UK date. The expiry is shown on the card; an absent date imposes no limit. Remove or update an offer when it is withdrawn or its terms change.

Use only verified details and obtain permission to publish them. Text is displayed as plain text, not HTML. Entries with missing required fields, invalid dates or URLs, an end date before the start, or an invalid `sponsored` value are not displayed; a notice appears instead. Keep the file as `{"offers":[]}` when there are no approved offers. Validate the JSON after editing and check the discounts page after deployment.
