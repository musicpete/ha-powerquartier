# Contributing

## Test data policy

This integration talks to a portal that holds personal data. None of it may
enter this repository - not in code, tests, fixtures, docs, examples, issues
or commit messages.

- **Synthetic values only.** Use `example.com` addresses, all-zero
  identifiers (`00000000001`, `AT0000000000000000000000000000001`),
  placeholder UUIDs (`aaaaaaaa-0000-0000-0000-000000000001`) and neutral
  names (`Profil-A`, `Tarif A`, `Musterstraße 1`).
- **Nothing derived from a real account.** Do not rename, round or shift real
  values; that still reveals the structure (number of meters, tariffs,
  amounts, dates, membership length).
- **Capture formats, not data.** To reproduce a portal response, write down
  its field names and types and build the fixture from scratch. Never commit
  HAR files, response dumps, screenshots or invoice PDFs.
- **Logs and errors** must not contain credentials, tokens, customer,
  contract, meter or invoice numbers.

## Checks

Run before every commit (installed once with `pre-commit install`):

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

The hooks run [gitleaks](https://github.com/gitleaks/gitleaks) for secrets
and `scripts/check_privacy.py` for personal data (e-mail addresses outside
example domains, IBANs, Austrian points of delivery, phone numbers, JWTs,
non-placeholder UUIDs). CI runs the same checks plus the tests and
Home Assistant/HACS validation on every push.

## Tests

```bash
pip install -r requirements_test.txt
pytest tests
pytest tests_api -p no:homeassistant -p no:pytest_homeassistant_custom_component
```

## Security

See [SECURITY.md](SECURITY.md). Never include real data in a report.
