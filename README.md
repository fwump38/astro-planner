# Milky Way Planner (Home Assistant integration)

Plan Milky Way photography from Home Assistant. It adds **calendars of the good nights** (pink and blue, like the
[Capture the Atlas](https://capturetheatlas.com) calendar), **sun-style sensors for the galactic core and the moon**,
**alert events**, a **weather-scored `best_night` service**, and it lets the companion
[Milky Way Planner card](https://github.com/fwump38/milky-way-planner-card) use the same numbers.

It builds on [AstroWeather](https://github.com/mawinkler/astroweather) for the forecast and does the astronomy itself
(no cloud service, no API key). The card's calculations were ported to Python and are tested against the card's own output.

## Install

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/fwump38/milky-way-planner` as an **Integration**.
2. Install **Milky Way Planner** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Milky Way Planner**, then pick your AstroWeather sites and give each a
   Bortle class (and optionally a drive time). Everything else is under **Configure**.

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

## Calendars

| Entity | Contains |
|---|---|
| `calendar.milky_way_best_nights` (pink) | Nights where the core is up in dark, moonless sky for at least 3 h (configurable) |
| `calendar.milky_way_short_window_nights` (blue) | Nights with a shorter, but non-empty, window |

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
`sensor.milky_way_best_site_tonight`.

**Galactic core**: `sensor.galactic_core_elevation`, `…_azimuth`, `…_orientation`, `…_next_rising`, `…_next_setting`,
`binary_sensor.galactic_core_above_horizon`.

**Moon** (the core Moon integration has the phase but not these): `sensor.moon_next_rising`, `sensor.moon_next_setting`,
`sensor.moon_elevation`, `sensor.moon_illumination`.

**Per site**: `sensor.<site>_milky_way_score`, tonight's 0–100 score with the next nights, window, cloud cover and drive time as attributes.

## Events

`event.milky_way_events` fires:

| Event type | When | Attributes |
|---|---|---|
| `window_start` / `window_end` | The core window opens / closes | `at` |
| `forecast_good` | A night's forecast score first reaches your threshold (default 70) at a site | `night`, `site`, `entity`, `score` |
| `forecast_dropped` | A night that was good falls clearly below it | same |

Only nights covered by the forecast count, so astronomy-only guesses never trigger alerts.

## Service: `milky_way_planner.best_night`

Returns the best upcoming nights with your sites ranked by forecast. Example:

```yaml
action: milky_way_planner.best_night
data:
  days: 14          # look this far ahead (default 30)
  count: 3          # how many nights to return
  min_grade: best   # "best" = pink only, "short" = include blue
  min_score: 60
  sort: score       # score | date | window
response_variable: nights
```

Each night has `date`, `grade`, `window_start`/`window_end`, `orientation`, moon details, the `best_site`, and a ranked `sites` list
(score, cloud, Bortle, drive minutes). Beyond the forecast horizon the score is astronomy-only (`astro_only: true`).

### Automation example

```yaml
triggers:
  - trigger: calendar
    event: start
    entity_id: calendar.milky_way_best_nights
    offset: "-3:00:00"
actions:
  - action: milky_way_planner.best_night
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

If this integration is installed, the card automatically asks it for the astronomy (calendar nights, moon rise/set, core window)
and falls back to its own identical calculations if it isn't there, so the card still works on its own. The integration exposes a
small websocket API (`milky_way_planner/info`, `nights`, `light_domes` and, for admins, `find_light_domes`) for this.

## Privacy

Drive times (optional, on by default) are looked up from the public [OSRM demo server](https://router.project-osrm.org), which receives
your Home Assistant location and each site's location, rounded to about 1 km. Each route is requested once and cached. If it can't be
reached, a straight-line estimate is used. Turn it off in the options, or give a site a drive time.

## Development

```
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m pytest
```

`tests/fixtures/ts-nights.json` is generated from the card's TypeScript and pins the two implementations to each other.

City data: [GeoNames](https://www.geonames.org), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); fallback [Wikidata](https://www.wikidata.org) (CC0).

MIT licensed.
