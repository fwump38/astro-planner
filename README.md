# Astro Planner (Home Assistant integration)

Plan astrophotography from Home Assistant. It scores your dark-sky sites every night, **whether or not the Milky Way is up**, tells you
**which Messier objects and planets are worth shooting** from your location, and adds **calendars** (Milky Way pink/blue nights like the
[Capture the Atlas](https://capturetheatlas.com) calendar, moonless nights, meteor showers), **sensors**, **alert events** and services.
The companion [Astro Planner card](https://github.com/fwump38/astro-planner-card) shows it all on a dashboard.

It builds on [AstroWeather](https://github.com/mawinkler/astroweather) for the forecast and does the astronomy itself (no cloud service,
no API key).

> **Coming from Milky Way Planner?** This is the same project, renamed. The domain changed from `milky_way_planner` to `astro_planner`, so
> remove the old integration and add this one (your sites are picked up again from AstroWeather). Entities are now `…astro_planner…`
> (e.g. `sensor.astro_planner_best_site_tonight`), each site's score sensor is `sensor.<site>_sky_score`, and the Milky Way sensors,
> calendars and events keep their names. Update your automations and card.

## Install

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=fwump38&repository=astro-planner&category=integration)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/fwump38/astro-planner` as an **Integration**.
2. Install **Astro Planner** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Astro Planner**. Every AstroWeather site is pre-selected; untick any you
   don't want. Tick **Review each site's details** only if you want to set a name, Bortle class, drive time or coordinates yourself.
   Everything else is under **Configure**. **Edit all sites as a list** shows every site's details as one list, which is also a quick way to back up
   or restore them.

**Where site values come from.** Coordinates are read from AstroWeather. The Bortle class is *estimated* from nearby towns
(GeoNames, Walker's law), which is rough: it ignores industry, roads and terrain, so set the Bortle class yourself in the site's
details if you know it. Drive time is looked up (OSRM) after setup. Each site's score sensor has a `source` attribute showing
which of these were configured, estimated, read from AstroWeather or computed.

With no sites, everything is computed for your Home Assistant location.

## Light domes

Cities near your sites affect the core differently depending on which way it is: a city behind the core at low altitude washes it
out, a city on the opposite horizon barely matters. The planner models each city's glow by distance and population, compares its
direction with where the core is at each moment, and lowers a night's score accordingly.

**Options → Light domes → Find nearby cities** builds the list for you:

1. It downloads the [GeoNames](https://www.geonames.org) `cities1000` dump (~11 MB, no account needed), once, and caches it for 30 days.
   If GeoNames can't be reached it falls back to [Wikidata](https://www.wikidata.org).
2. It keeps places that would actually add glow at your Home Assistant location or any site, within the search radius you choose.
3. It folds nearby towns (within 20 km) into the larger city's metro area, so a city and its suburbs count as one dome.
4. You review and edit the result before it's saved.

The list is stored in the integration and is also what the card uses if you haven't given the card a list of its own.
The lookups send your sites' coordinates only to Wikidata, and only if GeoNames fails (the GeoNames download is the same file for everyone).

## How nights are scored

A site's score (0-100) for a night is the **forecast quality** (cloud, transparency, seeing, wind; rain and fog penalised) times the
**sky quality**: Bortle class, how much moon-free astronomical darkness the night has, and city glow across the whole sky. The sky alone
is worth up to **70**; **the Milky Way core being up in dark, moonless sky adds the rest**, so those nights always score higher at the same
site. Out of season, sites still rank against each other. Beyond the forecast horizon the score is astronomy-only (`astro_only: true`).

Each site also gets a ranked list of **targets** for the night (the Milky Way core, Messier objects, planets, your custom targets), scored by
time above the minimum altitude, height, the moon's position and brightness, the site's Bortle class (faint galaxies and nebulae suffer
from light pollution, planets hardly) and the forecast over the hours the target is actually up.

## Targets

Options → **Targets** sets the minimum altitude (default 30°), the faintest magnitude (default 12), an optional **field of view** (the smaller
side of your camera and scope, in arcminutes; objects larger than it are left out of rankings and flagged in the card) and a **watch list**.
Options → **Custom targets** adds your own objects by name, `ra` (hours, `5.59` or `05:35:17`) and `dec` (degrees, `22.01` or `+22:00:52`).
The catalog is the 110 Messier objects (J2000, from [OpenNGC](https://github.com/mattiaverga/OpenNGC), CC BY-SA 4.0) plus the planets.

## Calendars

| Entity | Contains |
|---|---|
| `calendar.milky_way_best_nights` (pink) | Nights where the core is up in dark, moonless sky for at least 3 h (configurable) |
| `calendar.milky_way_short_window_nights` (blue) | Nights with a shorter, but non-empty, window |
| `calendar.astro_planner_moonless_nights` | The longest moon-free stretch of astronomical darkness at your Home Assistant location, on nights where it lasts 2 h or more |
| `calendar.astro_planner_meteor_showers` | The peak night of each annual meteor shower: ZHR, radiant height, moon and an outlook rating for your location |

Each event runs from the start to the end of the core window, so the calendar entity is **on during the window** and works with
Home Assistant's calendar triggers (start/end, with an offset). The description carries the details: window length, shape
(arch/angled/upright) and direction, moon illumination and rise/set, and, for the next few nights, the best site and its score.
Home Assistant gives every event in a calendar one colour, which is why the two grades are separate calendars (they default to pink and blue).

Night windows are computed at the middle of your sites, like the card's calendar. Times have 10-minute resolution.

## Sensors

Modelled on the `sun` integration.

**Milky Way**: `sensor.milky_way` (`visible` / `twilight` / `moonlit` / `below_horizon`, with elevation, azimuth, orientation and
moon attributes), `binary_sensor.milky_way_visible`, `sensor.milky_way_next_window_start`, `…_next_window_end`, `…_next_window_length`,
`sensor.milky_way_next_good_night` (start of the next pink night), `sensor.milky_way_tonight` (`best` / `short` / `none`),
.

**Galactic core**: `sensor.galactic_core_elevation`, `…_azimuth`, `…_orientation`, `…_next_rising`, `…_next_setting`,
`binary_sensor.galactic_core_above_horizon`.

**Moon** (the core Moon integration has the phase but not these): `sensor.moon_next_rising`, `sensor.moon_next_setting`,
`sensor.moon_elevation`, `sensor.moon_illumination`.

**Astro Planner** (work in any season): `sensor.astro_planner_best_site_tonight`, `sensor.astro_planner_best_target_tonight` (e.g. *M31 – Andromeda
Galaxy*, with the site, score and alternatives), `sensor.astro_planner_dark_hours_tonight`, `sensor.astro_planner_moon_free_dark_hours_tonight`,
`sensor.astro_planner_messier_objects_up_tonight`, `sensor.astro_planner_next_meteor_shower`.

**Per site**: `sensor.<site>_sky_score`, tonight's 0–100 score with the next nights, whether the Milky Way is up, cloud cover, drive time and the
best targets as attributes.

## Events

`event.astro_planner_events` fires:

| Event type | When | Attributes |
|---|---|---|
| `window_start` / `window_end` | The core window opens / closes | `at` |
| `forecast_good` | A night's forecast score first reaches your threshold (default 70) at a site | `night`, `site`, `entity`, `score`, `best_target`, `best_target_id` |
| `forecast_dropped` | A night that was good falls clearly below it | `night`, `site`, `entity`, `score` |
| `target_good` / `target_dropped` | The same threshold crossings for each target on your **watch list** (the Milky Way core included) | `night`, `site`, `entity`, `target`, `target_id`, `score` |

Only nights covered by the forecast count, so astronomy-only guesses never trigger alerts.

## Service: `astro_planner.best_night`

Returns the best upcoming nights with your sites ranked by forecast. Example:

```yaml
action: astro_planner.best_night
data:
  days: 14          # look this far ahead (default 30)
  count: 3          # how many nights to return
  min_grade: best   # "best" = pink only, "short" = include blue, "any" = every night (rank by sky quality)
  min_score: 60
  sort: score       # score | date | window
response_variable: nights
```

Each night has `date`, `grade`, `window_start`/`window_end`, `orientation`, moon details, the `best_site`, its `best_targets`, and a ranked `sites`
list (score, cloud, Bortle, drive minutes). Use `min_grade: any` outside Milky Way season.

## Service: `astro_planner.visible_targets`

Lists what is up after dark at your Home Assistant location: for each target its kind, magnitude, size, constellation, best time of year, and per
night the minutes above the minimum altitude, peak altitude and direction, start/end and moon interference.

```yaml
action: astro_planner.visible_targets
data:
  days: 1                      # nights from tonight (or `start`)
  kinds: [galaxy, nebula]      # optional; also globular_cluster, open_cluster, planetary_nebula, planet, custom, ...
  min_minutes: 120             # skip anything up for less than this
response_variable: targets
```

### Automation example

```yaml
triggers:
  - trigger: calendar
    event: start
    entity_id: calendar.milky_way_best_nights
    offset: "-3:00:00"
actions:
  - action: astro_planner.best_night
    data: { days: 1, count: 1 }
    response_variable: tonight
  - condition: template
    value_template: "{{ tonight.nights | length > 0 and tonight.nights[0].score >= 60 }}"
  - action: notify.mobile_app_phone
    data:
      message: >
        Milky Way tonight at {{ tonight.nights[0].best_site.name }}
        (score {{ tonight.nights[0].score }}), core {{ tonight.nights[0].window_start | as_timestamp | timestamp_custom('%H:%M') }}
```

## Using it with the card

The [Astro Planner card](https://github.com/fwump38/astro-planner-card) is only a display for this integration: it calculates nothing
itself and needs this integration installed. It uses a small websocket API (`astro_planner/info`, `overview`, `calendar`, `sky_hours`, `targets`,
`showers`, `light_domes` and, for admins, `find_light_domes`, API version 3). `overview` returns each site with its settings and where they came from,
and each site's scored nights; the card shows the Bortle class with a "~" when it was estimated.

## Privacy

Drive times (optional, on by default) are looked up from the public [OSRM demo server](https://router.project-osrm.org), which receives
your Home Assistant location and each site's location, rounded to about 1 km. Each route is requested once and cached. If it can't be
reached, a straight-line estimate is used. Turn it off in the options, or give a site a drive time.

The Bortle estimate downloads the same public [GeoNames](https://www.geonames.org) `cities1000` file as the light-dome lookup (and falls back to
Wikidata, which would receive a bounding box around your sites). Your coordinates are not sent anywhere for the GeoNames route.

## Development

```
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m pytest
```

`tests/fixtures/ts-nights.json` was generated from the card's old TypeScript astronomy (since removed) and still pins these calculations to it;
`tests/test_capture_the_atlas.py` checks them against Capture the Atlas's published 2026 calendar.

Meteor shower peak dates are the usual IMO values and move by about a day a year; they are a guide to the night to go out, not an ephemeris.

The card's dev harness uses `scripts/export_card_mock.py` to write canned responses (see the card's README).

City data: [GeoNames](https://www.geonames.org), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); fallback [Wikidata](https://www.wikidata.org) (CC0).

MIT licensed.
