def apply_country_filter(queryset, user):
    """
    Filter queryset by user's current_country if it's set to a specific country.
    Returns the (possibly filtered) queryset.

    - blank / None / 'all' → no country filter (return queryset unchanged)
    - model has `associated_country` → filter on that
    - model has `countries` (JSONField list) → filter where list contains the country
    - model has `desired_country` → filter on that
    - otherwise                       → return queryset unchanged
    """
    country = getattr(user, 'current_country', None)
    if not country or country == 'all':
        return queryset

    field_names = {f.name for f in queryset.model._meta.get_fields()}
    if 'associated_country' in field_names:
        return queryset.filter(associated_country=country)
    if 'operating_countries' in field_names:
        # operating_countries is a JSONField list e.g. ["UK", "UAE"]
        # Use icontains on the JSON text representation with quoted value
        # e.g. '"UK"' matches ["UK", "UAE"] but not ["United Kingdom"]
        return queryset.filter(operating_countries__icontains=f'"{country}"')
    if 'countries' in field_names:
        # countries is a JSONField list e.g. ["UK", "UAE"]
        # Use icontains on the JSON text representation with quoted value
        # e.g. '"UK"' matches ["UK", "UAE"] but not ["United Kingdom"]
        return queryset.filter(countries__icontains=f'"{country}"')
    if 'desired_country' in field_names:
        return queryset.filter(desired_country=country)
    return queryset
