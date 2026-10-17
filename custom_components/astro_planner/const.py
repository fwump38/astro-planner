"""Constants for Astro Planner."""

DOMAIN = "astro_planner"
API_VERSION = 3

CONF_SITES = "sites"
CONF_ENTITY = "entity"
CONF_NAME = "name"
CONF_BORTLE = "bortle"
CONF_DRIVE_MINUTES = "drive_minutes"
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_MIN_GC_ALTITUDE = "min_gc_altitude"
CONF_MAX_MOON = "max_moon_illumination"
CONF_CAL_MIN_ALTITUDE = "calendar_min_altitude"
CONF_CAL_BEST_MINUTES = "calendar_best_minutes"
CONF_WEIGHTS = "weights"
CONF_LIGHT_DOMES = "light_domes"
CONF_LIGHT_DOMES_INFO = "light_domes_info"
CONF_DOME_SIGMA = "dome_sigma"
CONF_DOME_WARNING = "dome_warning_threshold"
CONF_NIGHTS = "nights"
CONF_GOOD_SCORE = "good_score_threshold"
CONF_AUTO_DRIVE = "auto_drive_time"
CONF_TARGET_MIN_ALTITUDE = "target_min_altitude"
CONF_TARGET_MAX_MAGNITUDE = "target_max_magnitude"
CONF_FOV = "fov_arcmin"
CONF_WATCH_TARGETS = "watch_targets"
CONF_CUSTOM_TARGETS = "custom_targets"

DEFAULT_BORTLE = 3
DEFAULTS = {
    CONF_MIN_GC_ALTITUDE: 10,
    CONF_MAX_MOON: 10,
    CONF_CAL_MIN_ALTITUDE: 0,
    CONF_CAL_BEST_MINUTES: 180,
    CONF_DOME_SIGMA: 25,
    CONF_DOME_WARNING: 0.15,
    CONF_NIGHTS: 7,
    CONF_GOOD_SCORE: 70,
    CONF_AUTO_DRIVE: True,
    CONF_TARGET_MIN_ALTITUDE: 30,
    CONF_TARGET_MAX_MAGNITUDE: 12,
    CONF_FOV: 0,
}

FORECAST_REFRESH_MINUTES = 30
LIVE_REFRESH_SECONDS = 60
# How far ahead the calendars, "next good night" lookups and window events search.
SEARCH_DAYS = 400

# Pink = a full-length window, blue = a short one (Capture the Atlas's colour rule).
GRADE_BEST = "best"
GRADE_SHORT = "short"
COLOR_BEST = "#e91e8c"
COLOR_SHORT = "#2196f3"

EVENT_WINDOW_START = "window_start"
EVENT_WINDOW_END = "window_end"
EVENT_FORECAST_GOOD = "forecast_good"
EVENT_FORECAST_DROPPED = "forecast_dropped"
EVENT_TARGET_GOOD = "target_good"
EVENT_TARGET_DROPPED = "target_dropped"

# A night counts as "moonless" for the calendar when it has at least this many minutes of moon-free astronomical darkness.
MOONLESS_MIN_MINUTES = 120
# Per site and night, the top-ranked targets kept in the coordinator's data (watched targets are always kept).
TOP_TARGETS = 15
