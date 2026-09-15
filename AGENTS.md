# allocine-to-trakt

Single-script tool: exports a public Allociné profile's movie/series ratings into a Trakt-importable JSON. Everything runs locally; no Trakt API access (Trakt API keys are paid).

## Run

```bash
./install.sh
./export.sh
```

- With no arguments, `export.sh` launches the interactive wizard; scripted runs can pass `--url` directly.
- `overrides.json` is created automatically; `--overrides` is optional.
- Always use `.venv/bin/python`, never system `python3` (its SSL certs are broken on this machine — `urllib`/pip fail; `requests` inside the venv bundles certifi).
- The first complete audit is the slow one (duration depends on profile size and remote service speed); later runs replay from caches.
- `--review`: interactive terminal walk-through with arrow-key radio menus (validate, choose IMDb/TMDB proposal, enter a manual ID, search TMDB, exclude, skip, quit). Decisions persist to `cache/review-ok.json` + `overrides.json`.
- `--refresh-scrape`: force re-scraping the profile (otherwise `cache/items-*.json` is reused, so new Allociné ratings need this flag).
- `--date YYYY-MM-JJ` sets the fixed `watched_at`/`rated_at` (default: today, 12:00 UTC). Allociné stores no rating dates, so all dates are this fixed value.

## Secrets

- `.env` holds `TMDB_API_KEY` (chmod 600). Never print it, never commit it.
- Do not try the Trakt API: key creation is paid.

## Cache semantics (`cache/`) — statuses are replayed, not recomputed

| File | Content |
|---|---|
| `items-<member>.json` | scraped profile (title/rating/Allociné URL per item) |
| `detail/` | per-item Allociné page data: year, original title, `credits_checked`, duration/actors/director (from JSON-LD) |
| `imdb.json` / `imdbcands.json` | IMDb suggestion resolutions / candidates+cast for items in doubt |
| `tmdb.json` / `tmdbmeta.json` / `find.json` | TMDB cross-checks, candidate runtime/cast/director, imdb→TMDB lookups |
| `cross.json` | auto-arbitration decisions — **replayed every run; delete a key (or the file) to re-decide** |
| `wikidata.json` | last-resort P345 resolutions |
| `review-ok.json` | human decisions (`validated` / `exclude`) |

Deleting a cache changes outcomes. The scoring engine caches are: `imdbcands.json`, `tmdbmeta.json`, `cross.json` (purge these, not `imdb.json`, to re-arbitrate).

## Decision rules (conservative, by design)

- Auto-apply a mapping only with ≥2 strong signals (runtime within ±2 min **for movies only**, ≥2 shared actors, same director) or margin ≥3 over a disqualified incumbent; never on a single weak signal.
- Year Δ>1 is a **hard conflict for shows** (avoids remapping a later series to an earlier one with a similar title) but only a soft note for movies (French re-releases sometimes append a new year to the original title).
- Runtime mismatch is a **soft** conflict (TMDB runtimes are sometimes wrong); don't let one block a replace backed by 4 actors + director. Casting disjoint is **hard** — but beware name-order artifacts (see below).
- Person matching handles Korean/Japanese order flips: Allociné westernizes ("Jun-yeol Ryu"), TMDB/IMDb don't ("Ryu Jun-yeol") — `person_variants` adds a token-sorted variant for this. Without it, Asian shows get false "casting disjoint".
- Margin is computed against **credible competitors only** (année within ±1 of the Allociné year); a candidate years apart is not the film being looked up.
- Resolution order in `main()`: IMDb cascade → TMDB fallback → TMDB check → **Wikidata** → **cross pass (doubt items + audit of every other item)**. Wikidata runs before the cross pass so its resolutions get duration/cast verification; items with no Allociné credits get `cross.json` action `skip` (never re-fetched).
- `cross.json` is saved incrementally every 25 items (runs are killable/resumable); statuses are replayed, not recomputed.
- Statuses: `ok`, `ok_cross`, `ok_wikidata`, `ok_tmdb` (weak: `ok_year_only`/`ok_year_near`/`ok_year_adjusted` unless TMDB-confirmed or cross-validated), `override` (human, wins over everything), `excluded`, `unresolved_*` (excluded from import).
- Expected end state: `VALIDATION : OK`, unique `(type, imdb_id)`, ratings 1–10 (Allociné /5 × 2), strictly identical duplicate entries deduped.

## Allociné scraping quirks

- Profile lists: `allocine.fr/membre-{id}/films|series/?page=N`. Pagination ends when requesting `page=N+1` redirects back to a lower page; empty card list also stops.
- Some card links are obfuscated: CSS class starting with `ACr` + base64 of the URL path (strip every `ACr` substring, then b64decode, validate `/film/` or `/series/` prefix).
- Rating lives in the card's CSS class `rating-mdl nXX` (XX/5, halves) → Trakt rating = XX × 2 / 10.
- Movie detail pages carry duration/actors/director in `application/ld+json` (@type Movie). **Series JSON-LD lacks them** — fall back to the `meta-body-direction` / `meta-body-actor` divs (`parse_body_credits`); actor text may sit inside obfuscated spans but is still plain text.
- `/critiques/...` pages redirect (this profile has none) — there is genuinely no date data.

## Resolution sources (no key for IMDb)

- IMDb: public suggestion API `v3.sg.media-imdb.com/suggestion/x/{query}.json` (no key). Match requires exact year.
- TMDB: search by French title + year → `external_ids` → imdb_id; also used to validate existing mappings (`/find/{imdb_id}` + `/movie|tv/{id}?append_to_response=credits`).
- Wikidata: `wbsearchentities` (label fr) → claims P345 (imdb) + P577 (year), only for items nothing else resolves; accepted only if unique + exact year.
- Trakt API is off-limits (paid) — final coverage check happens at import time on Trakt's side.

## Output files (regenerated every run)

- `trakt-import.json` (the deliverable), `report.csv` (**semicolon** separator, UTF-8 BOM, `confiance` column), `review.csv` (items in doubt only, sorted by risk), `unresolved.json`.
- `overrides.json` maps `films-<allocine_id>` / `series-<allocine_id>` → `tt…`; human decisions go here and beat all caches.

## Conventions

- Delays are politeness + anti-429: Allociné 1.2 s, IMDb 0.3 s, TMDB 0.25 s (tunable CLI flags); retry with backoff on 429/5xx — a run killed mid-way is safely resumable.
- User-facing strings are French; keep it that way.
- Syntax check after edits: `.venv/bin/python -m py_compile allocine_to_trakt.py`.
- Expected end state after a full run: `VALIDATION : OK`, `Confiance : sûre <total> / à vérifier 0 / hors import 0`.
