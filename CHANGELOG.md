# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.9] – 2026-11-19

### Changed
- Stromampel `forecast` attribute now holds every upcoming slot the portal
  delivers (several days), not just the next 24 hours.
  `anteil_gemeinschaft_naechste_24h_pct` is still computed over 24 hours.

### Added
- `forecast` entries carry the community's expected production and
  consumption per slot (`erzeugung_kwh`, `verbrauch_kwh`).
- Attribute `prognose_bis`: end of the available forecast.

## [0.1.8] – 2026-11-18

### Fixed
- The last imported day (and its values) is kept across restarts, so the
  "last data day" sensors are no longer unknown until the next import.

## [0.1.7] – 2026-11-18

### Added
- Status sensor attributes `kwh_pro_jahr` and `kwh_pro_monat` (community and
  grid kWh per year/month) computed from the stored statistics, i.e. the
  whole history, refreshed after every import and at startup. Not written
  to the recorder database.

## [0.1.6] – 2026-11-18

### Security
- The password is only sent if the portal's login form points to the portal
  host over HTTPS.
- The config entry no longer uses the email address as id or title (hashed
  id; existing entries are migrated automatically).
- `powerquartier.refresh` is admin-only.
- Repository tooling: pre-commit hooks (gitleaks, personal-data check),
  CI with tests and validation, documented synthetic test-data policy.

### Changed
- Test fixtures contain synthetic placeholder data only.

## [0.1.5] – 2026-11-18

### Added
- Retries for transient errors per request (timeouts, connection errors,
  HTTP 5xx; 2 s / 5 s / 15 s).
- Retry of a failed run after 30 minutes, up to 3 times
  (`naechster_versuch` attribute).
- Tracking of missing days (`fehlende_tage`), re-checked on every run for up
  to 400 days; a filled gap triggers a re-import from that day.

### Changed
- `powerquartier.refresh` raises an error when a profile's import fails.

## [0.1.4] – 2026-11-18

### Fixed
- A day page without a chart is retried instead of being imported as an
  empty day.

## [0.1.3] – 2026-11-18

### Fixed
- Translation error `UNCLOSED_TAG` in the profile selection step.

## [0.1.2] – 2026-11-18

### Fixed
- Follow the portal's `?_rsc=` redirects; only a redirect to the sign-in page
  counts as an expired session.
- Setup no longer blocks on an abandoned dialog ("already being configured").

## [0.1.1] – 2026-11-18

### Fixed
- Profile discovery right after login (profile-selection page).
- Profile is restored after an automatic re-login.

## [0.1.0] – 2026-11-18

### Added
- Initial release: hourly community/grid energy statistics per profile,
  Stromampel forecast, community price and tariff sensors, status sensor per
  profile, `powerquartier.refresh` action.
