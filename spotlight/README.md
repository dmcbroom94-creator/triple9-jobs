# Blue Light Spotlight images

The Spotlight page (triple9careers.co.uk/spotlight.html) reads `spotlight.json` in the repo root.
Only add people who have asked or agreed to be featured, and who have confirmed they work (or have worked) in the emergency services. We do not show which service they are in.

To add someone:
1. Upload two images to this `spotlight/` folder: a screenshot of their Instagram page (portrait, about 1080×1350) and their profile photo (square). Use short names with no spaces, e.g. `bluelinefitness.jpg` and `bluelinefitness-photo.jpg`.
2. Add an entry to the `people` list in `spotlight.json`:

```json
{
  "name": "Blue Line Fitness",
  "handle": "bluelinefitness",
  "category": "fitness",
  "blurb": "Shift-friendly workout plans for 12-hour nights.",
  "screenshot": "spotlight/bluelinefitness.jpg",
  "photo": "spotlight/bluelinefitness-photo.jpg"
}
```

Categories: business, fitness, wellbeing, coaching, creative, charity, content.
Changes show on the site straight away (no rebuild needed). To remove someone, delete their entry and images.
