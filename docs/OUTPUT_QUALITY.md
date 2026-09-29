# Output Quality V5.3

The runtime now performs a conservative post-extraction quality pass before statistical enrichment.

## Address repair

`formatted_address` is rebuilt from structured components only when it is missing or when the city occurs more than once. The original value is retained in `formatted_address_original` when a repair is made.

The formatter treats house number/suffix, street type/name, building, residence, zones, BP, TSA, CS, postal routing code, postal code, city, CEDEX and country as separate components and de-duplicates exact components.

This fixes outputs such as:

```text
30 RUE DES GRANDS MORTIERS, 37705 ST PIERRE DES CORPS ST PIERRE DES CORPS
```

into:

```text
30 RUE DES GRANDS MORTIERS, 37705 ST PIERRE DES CORPS, France
```

without changing verification truth.

## JSON-wide audit

Each extraction now gets `output_quality` with `PASS`, `WARN` or `ERROR`, issue counts and issue details. Checks include:

- duplicated city in `formatted_address`;
- French postal-code shape when country is known as France;
- postal code without city;
- latitude/longitude range;
- contradiction between `is_verified_real_address` and verification status;
- verified address without provider;
- duplicate line numbers;
- `quantity * unit_price` vs line total when no discount is present;
- net + VAT vs gross/amount due.

Warnings do not silently rewrite financial values. Address repair is limited to presentation-level `formatted_address` unless a field is otherwise handled by the statistical-learning layer.
