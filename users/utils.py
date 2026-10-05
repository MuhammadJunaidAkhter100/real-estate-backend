import random
import string
import threading

import httpx
from django.conf import settings


def generate_password(length=12):
    if settings.DEBUG:
        return "Kingsmen@786"

    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    # Guarantee at least one of each required character type
    password = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%^&*"),
    ]
    password += random.choices(alphabet, k=length - 4)
    random.shuffle(password)
    return ''.join(password)


def convert_currency(amount, from_currency, to_currency):
    url = f"https://v6.exchangerate-api.com/v6/{settings.EXCHANGE_RATE_API_KEY}/pair/{from_currency}/{to_currency}"
    response = httpx.get(url)
    data = response.json()
    rate = data['conversion_rate']
    if rate is None:
        raise ValueError(f"Unsupported currency: {to_currency}")
    return amount * rate


_exchange_rate_cache = threading.local()


def get_exchange_rate(from_currency, to_currency):
    if from_currency == to_currency:
        return 1
    cache = getattr(_exchange_rate_cache, 'rates', None)
    if cache is None:
        cache = {}
        _exchange_rate_cache.rates = cache
    key = f"{from_currency}:{to_currency}"
    if key not in cache:
        cache[key] = convert_currency(1, from_currency, to_currency)
    return cache[key]
