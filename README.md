# Powerquartier for Home Assistant

[![CI](https://github.com/musicpete/ha-powerquartier/actions/workflows/ci.yml/badge.svg)](https://github.com/musicpete/ha-powerquartier/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
![Version](https://img.shields.io/badge/version-0.1.7-blue)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.11%2B-03A9F4)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Unofficial Home Assistant integration for the **Powerquartier** portal of the
energy community *Fanclub Burgenland Energieunabhängig*
([burgenland.powerquartier.at](https://burgenland.powerquartier.at)).
It imports how much of your electricity came **from the community** and how
much was regular **grid power**, hourly and per meter, and adds the community
"Stromampel" forecast and your tariff as sensors.

> **Not affiliated** with Burgenland Energie, Netz Burgenland or Exnaton.
> The portal has no public API; this integration reads the same data the web
> app shows. A portal update can break it until the integration is updated.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Entities and statistics](#entities-and-statistics)
- [Actions](#actions)
- [Data quality and retries](#data-quality-and-retries)
- [Dashboard examples](#dashboard-examples)
- [Privacy and security](#privacy-and-security)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Development](#development)
- [Changelog](CHANGELOG.md) · [License](LICENSE)

## Features

| Area | What you get |
| --- | --- |
| Energy per meter | Hourly long-term statistics: energy from the community and grid residual, for every portal profile (e.g. one per meter) |
| History | Full history since you joined (backfill with one action) |
| Stromampel | Current community availability (community / mixed / grid) with a 24-hour forecast and the next community window |
| Prices | Community price of the current 15-minute slot, purchase and feed-in tariff (net) |
| Status | Last imported day per meter, community share, missing days, last error, next retry |
| Reliability | Retries per request, per run and per missing day; preliminary values are corrected automatically |

## Requirements

- Home Assistant **2025.11** or newer
- HACS (up to HA 2026.10) or the built-in Marketplace (HA 2026.11+), or file access for a manual install
- A Powerquartier portal account (email and password)

## Installation

### HACS

1. HACS → **⋮** → **Custom repositories**
2. Repository `https://github.com/musicpete/ha-powerquartier`, type **Integration** → **Add**
3. Search **Powerquartier (Energiegemeinschaft)** → **Download**
4. Restart Home Assistant

### Marketplace (HA 2026.11+)

Settings → Marketplace → **Add from link** → `https://github.com/musicpete/ha-powerquartier`
(category *Integration*; a connected GitHub account is required) → install → restart.

### Manual

Copy `custom_components/powerquartier` to `/config/custom_components/powerquartier`
and restart. Updates are then manual as well.

## Configuration

1. Settings → Devices & services → **Add integration** → **Powerquartier**
2. Enter your portal email and password
3. Select the profiles (meters) to import

The first import starts about two minutes later and covers the last 30 days.
After that it runs daily at **07:17** and **19:17**.

**Options** (Configure on the integration): number of days re-imported on each
run (default 30, range 3–90).

## Entities and statistics

`<profil>` is derived from the portal profile name, e.g. `profil_a`,
`pruefprofil_b`.

| ID | Type | Description |
| --- | --- | --- |
| `powerquartier:<profil>_gemeinschaft` | statistic (kWh, hourly) | Energy allocated from the community |
| `powerquartier:<profil>_netz` | statistic (kWh, hourly) | Grid residual from your supplier |
| `sensor.powerquartier_stromampel` | enum | `gemeinschaft` / `gemischt` / `netz`; attribute `forecast` (next 24 h, 15-min slots) |
| `sensor.powerquartier_naechstes_gemeinschaftsfenster` | timestamp | Start of the next community window; attribute `ende` |
| `sensor.powerquartier_gemeinschaftspreis` | EUR/kWh | Community price of the current slot |
| `sensor.powerquartier_bezugstarif` | EUR/kWh | Purchase tariff (net) |
| `sensor.powerquartier_einspeisetarif` | EUR/kWh | Feed-in tariff (net) |
| `sensor.powerquartier_<profil>_letzter_datentag` | date | Latest imported day; attributes `kwh_gemeinschaft`, `kwh_netz`, `anteil_gemeinschaft_pct`, `vorlaeufig_ab`, `fehlende_tage`, `fehler`, `naechster_versuch`, `kwh_pro_jahr`, `kwh_pro_monat` (whole history) |

The `powerquartier:` entries are **long-term statistics**, not entities: use
them in *statistics graph* cards, the Energy dashboard or Developer tools →
Statistics. They do not appear in the History panel.

## Actions

### `powerquartier.refresh`

Re-imports data from the portal.

| Field | Default | Description |
| --- | --- | --- |
| `days` | 30 | Days up to yesterday; e.g. `1000` reloads the full history hourly |
| `profile` | all | Only this profile, e.g. `profil_a` |

Cumulative sums continue from the last statistics row before the window, so
data imported earlier under the same IDs stays consistent.

## Data quality and retries

- The grid operator delivers meter data **1–2 days late**; recent days are
  marked preliminary and corrected by later runs.
- **Per request:** timeouts, connection errors and HTTP 5xx are retried 3
  times (2 s, 5 s, 15 s). A day page without a chart is retried too and is
  never imported as an empty day.
- **Per run:** a failed import is retried after 30 minutes, up to 3 times;
  `naechster_versuch` shows when.
- **Missing days:** a day without data after earlier data is remembered
  (`fehlende_tage`, kept across restarts) and checked on every run for up to
  400 days. When it fills, the import restarts from that day so all later
  sums stay correct. Days younger than 3 days count as "not delivered yet".

## Dashboard examples

Daily energy, community vs. grid:

```yaml
type: statistics-graph
title: House · kWh per day
chart_type: bar
period: day
days_to_show: 30
stat_types: [change]
entities:
  - entity: powerquartier:profil_a_gemeinschaft
    name: Community
    color: green
  - entity: powerquartier:profil_a_netz
    name: Grid
    color: grey
```

Next 24 hours as an hourly strip:

```yaml
type: markdown
content: >-
  {% set f = state_attr('sensor.powerquartier_stromampel','forecast') or [] %}
  {% set sq = {'gemeinschaft':'🟩','gemischt':'🟨','netz':'🟥'} %}
  {% for s in f | selectattr('start','search','T\\d\\d:00:00') %}{{ sq.get(s.status,'⬜') }}{% endfor %}
```

## Privacy and security

**What is stored in Home Assistant**

| Data | Where | Why |
| --- | --- | --- |
| Portal email and password | Config entry (`.storage/core.config_entries`, plain text like every HA integration credential) | Automatic login |
| Profile ids and names you selected | Config entry | Which meters to import |
| Hourly kWh per profile | Recorder long-term statistics | The purpose of the integration |
| Days with missing data | `.storage/powerquartier.<entry>_gaps` | Retrying them |

The config entry's id is a SHA-256 hash of the email address and its title is
"Powerquartier", so the address does not appear in the UI, entity registry or
logs.

**What is sent where**

- Only to the portal host (`burgenland.powerquartier.at`) over HTTPS with
  certificate verification. The password is sent only if the login form
  points to that host; any other target is refused.
- The session cookie lives in a cookie jar used by this integration only.

**What is read and never stored**

Portal pages also contain your name, address and bank details. The parser
reads only profile ids and names, community id, rate plan prices and energy
values; everything else is discarded right away and never logged.

**Logs and errors** contain profile names (e.g. "Profil-A"), dates and HTTP
status codes, no credentials or cookies. Check them anyway before posting a
log publicly.

**Permissions:** the `powerquartier.refresh` action is admin-only.

Report security issues as described in [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| "Portal unreachable or unexpected response" during setup | Check the log for `custom_components.powerquartier`; the message names the step that failed |
| "Re-authentication required" | Open the notification and enter the current portal password |
| Status sensor shows `fehler` | Wait for `naechster_versuch`; persistent errors usually mean the portal changed |
| Days at 0 kWh | See `fehlende_tage`; they are re-checked automatically |

## How it works

- **Login:** Ory Kratos browser flow (`/self-service/login/browser` → POST with
  CSRF token); session cookie in a private cookie jar; re-login on expiry.
- **Profiles:** read from the profile-selection page or the account page;
  switching uses the `exnaton.selectedAccountId` cookie and is verified via
  `isSelected` before any data is read.
- **Data:** Next.js RSC payloads (redirects to `?_rsc=` are followed):
  `/de-AT/analyse/you` (one request per local day → 15-minute Wh values),
  `/de-AT/account` (profiles, `ratePlan`), overview page (Stromampel).
- All parsing lives in `api.py`.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the test data policy, the
pre-commit hooks (gitleaks secret scan and a personal-data check) and how to
run the tests. CI runs the same checks, the tests and Home Assistant/HACS
validation on every push.
