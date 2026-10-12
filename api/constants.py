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
COUNTRY_DIAL_CODE_MAP = {country['name']: country['dialCode'] for country in COUNTRY_DATA}

COUNTRY_CURRENCY_MAP = {
    "UAE": "AED",
    "UK": "GBP",
    "Oman": "OMR",
    "Saudi Arabia": "SAR",
    "Indonesia": "USD",
}
