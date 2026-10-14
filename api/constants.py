import csv
from pathlib import Path


with Path(__file__).with_name('countries.csv').open(encoding='utf-8', newline='') as country_file:
    COUNTRY_DATA = tuple(csv.DictReader(country_file))

COUNTRY_NAMES = [country['name'] for country in COUNTRY_DATA]
COUNTRY_ALIASES = {
    'UAE': 'United Arab Emirates',
    'UK': 'United Kingdom',
}
AVAILABLE_COUNTRIES = COUNTRY_NAMES + list(COUNTRY_ALIASES)
COUNTRY_CODE_MAP = {country['name']: country['code'] for country in COUNTRY_DATA}
COUNTRY_NAME_BY_CODE = {country['code']: country['name'] for country in COUNTRY_DATA}
COUNTRY_DIAL_CODE_MAP = {country['name']: country['dialCode'] for country in COUNTRY_DATA}

# The currency prices are shown in for each country (ISO 4217), from the
# `currency` column of countries.csv. It matches the frontend's country list
# (src/data/worldCountries.ts); Indonesia is priced in USD on purpose.
COUNTRY_CURRENCY_MAP = {country['name']: country['currency'] for country in COUNTRY_DATA}
COUNTRY_CURRENCY_MAP.update({
    alias: COUNTRY_CURRENCY_MAP[name] for alias, name in COUNTRY_ALIASES.items()
})
