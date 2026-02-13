# Wikidata Class/Property Reference (verified 2026-02-12)

This file tracks real Wikidata IDs used by the app presets and tests.

| Use case | Wikidata class | Wikidata property | Notes |
| --- | --- | --- | --- |
| Film matching by EIDR | `Q11424` (film) | `P2704` (EIDR content ID) | Good for `schema.org/Movie` IDs such as EIDR. |
| Language matching by label | `Q34770` (language) | `rdfs:label` | Label-based matching (not a Wikidata property item). |
| Country matching by ISO-2 | `Q6256` (country) | `P297` (ISO 3166-1 alpha-2 code) | Stable identifier for countries. |
| City matching by Wikidata URL | `Q515` (city) | N/A (`wdc_value_is_wikidata=true`) | Use when WDC values already store Wikidata entity URLs. |

Primary sources:
- https://www.wikidata.org/wiki/Q11424
- https://www.wikidata.org/wiki/Property:P2704
- https://www.wikidata.org/wiki/Q34770
- https://www.wikidata.org/wiki/Q6256
- https://www.wikidata.org/wiki/Property:P297
- https://www.wikidata.org/wiki/Q515
