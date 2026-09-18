#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export des notes Allociné (films + séries) d'un profil public vers un JSON importable sur Trakt.

Exemple :
    .venv/bin/python allocine_to_trakt.py --url "https://www.allocine.fr/membre-ZXXXXXXXXXXXXXXXXXXX/"

Sorties (dans --output-dir) :
  - trakt-import.json : liste au format Trakt
        [{"imdb_id": "tt1234567", "type": "movie",
          "watched_at": "2025-01-01T12:00:00Z",
          "rating": 8, "rated_at": "2025-01-01T12:00:00Z"}, ...]
  - report.csv        : récapitulatif, une ligne par item Allociné (avec niveau de confiance)
  - review.csv        : uniquement les items en doute, triés par risque
  - unresolved.json   : détail JSON des items sans mapping retenu
  - cache/            : profil + fiches + résolutions IMDb/TMDB + décisions de revue

Confiance : « sûre » = titre exact + année exacte (IMDb) ou confirmation TMDB croisée
ou override manuel ; « à vérifier » = correspondance plus faible → revue --review.
Revue interactive : --review (menus fléchés, décisions persistées).

Notes Allociné converties : /5 en demi-points × 2 → entier 1..10.
Aucune clé IMDb nécessaire (API publique de suggestion IMDb) ; TMDB optionnelle (.env).
"""

import argparse
import base64
import csv
import json
import re
import sys
import time
import unicodedata
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

BASE_URL = "https://www.allocine.fr"
IMDB_SUGGEST_URL = "https://v3.sg.media-imdb.com/suggestion/x/{}.json"
TMDB_BASE = "https://api.themoviedb.org/3"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MEMBER_RE = re.compile(r"membre-([A-Z0-9]+)")
CARD_SELECTOR = "div.card.entity-card-simple.userprofile-entity-card-simple"
TITLE_LINK_SELECTOR = ".meta-title.meta-title-link"
RATING_CLASS_RE = re.compile(r"n(\d{2})")
ALLOCINE_ID_RE = re.compile(r"c(?:film|serie)=(\d+)")
IMDB_ID_RE = re.compile(r"^tt\d{7,9}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

KINDS = {"films": "movie", "series": "show"}
MOVIE_QIDS = {"movie", "tvMovie"}
SHOW_QIDS = {"tvSeries", "tvMiniSeries"}


class TmdbAuthError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def get_questionary():
    try:
        import questionary
    except ImportError as exc:
        raise RuntimeError(
            "La dépendance questionary manque. Relancez ./install.sh puis ./export.sh."
        ) from exc
    return questionary


def normalize_title(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def load_json(path, default):
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def save_json(path, data, indent=1):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")


SAFE_STATUSES = ("ok", "override", "ok_cross")
REVIEW_ORDER = [
    "unresolved_conflict", "unresolved_mismatch", "unresolved_ambiguous",
    "unresolved_year_mismatch", "unresolved_no_match",
    "ok_year_near", "ok_no_year", "ok_year_adjusted",
    "ok_year_only", "ok_wikidata", "ok_tmdb",
]


def review_sort_key(it):
    return REVIEW_ORDER.index(it.status) if it.status in REVIEW_ORDER else 99


def needs_review(it, reviewed_ok):
    if it.cache_key in reviewed_ok or it.reviewed:
        return False
    if it.status.startswith("unresolved"):
        return True
    if it.status in ("ok_tmdb", "ok_wikidata"):
        return not it.cross_validated
    if it.status in SAFE_STATUSES:
        return False
    return it.tmdb_outcome != "confirm" and not it.cross_validated


def confidence_level(it, reviewed_ok):
    if it.status == "excluded":
        return "exclue"
    if it.status.startswith("unresolved") or it.status in ("imdb_error", "detail_error"):
        return "hors import"
    if it.status in SAFE_STATUSES or it.tmdb_outcome == "confirm" or it.cross_validated:
        return "sûre"
    if it.cache_key in reviewed_ok or it.reviewed:
        return "validée"
    return "à vérifier"


def tmdb_find_imdb(session, key, imdb_id, kind_slug, delay):
    """Retourne {id, title, year} du film/série TMDB correspondant à un ID IMDb (ou None)."""
    time.sleep(delay)
    path = f"/find/{urllib.parse.quote(imdb_id)}"
    bucket = "movie_results" if kind_slug == "films" else "tv_results"
    data = tmdb_get(session, key, path, {"external_source": "imdb_id", "language": "fr-FR"})
    results = data.get(bucket) or []
    if not results:
        return None
    r = results[0]
    ds = r.get("release_date") or r.get("first_air_date") or ""
    return {"id": r.get("id"), "title": r.get("title") or r.get("name") or "",
            "year": int(ds[:4]) if ds[:4].isdigit() else None}


def run_review(session, items, tmdb_key, cache_dir, overrides_path, delay_tmdb):
    """Revue interactive des items en doute, décisions persistées."""
    q = get_questionary()
    ok_path = cache_dir / "review-ok.json"
    reviewed_ok = load_json(ok_path, {})
    find_cache_path = cache_dir / "find.json"
    find_cache = load_json(find_cache_path, {})
    overrides = load_json(Path(overrides_path), {})

    def label_of(imdb_id, kind_slug):
        if not imdb_id:
            return None, None
        if not tmdb_key:
            return None, None
        if imdb_id in find_cache:
            d = find_cache[imdb_id] or {}
            return d.get("title"), d.get("year")
        try:
            r = tmdb_find_imdb(session, tmdb_key, imdb_id, kind_slug, delay_tmdb)
        except (TmdbAuthError, requests.RequestException):
            r = None
        find_cache[imdb_id] = r or {}
        save_json(find_cache_path, find_cache)
        return (r or {}).get("title"), (r or {}).get("year")

    def apply_mapping(it, new_id):
        it.imdb_id, it.status = new_id, "override"
        overrides[it.cache_key] = new_id
        save_json(Path(overrides_path), overrides, indent=2)
        it.reviewed = True
        reviewed_ok[it.cache_key] = True
        save_json(ok_path, reviewed_ok)
        return True

    def todo_sorted():
        todo = [it for it in items if needs_review(it, reviewed_ok)]
        todo.sort(key=review_sort_key)
        return todo

    todo = todo_sorted()
    log("\n===== REVUE INTERACTIVE =====")
    log(f"{len(todo)} item(s) en doute à examiner.")
    log("Utilisez les flèches puis Entrée pour choisir une action.")
    if not todo:
        log("Aucun item à valider. L'export est déjà prêt.")
        return

    idx = 0
    while idx < len(todo):
        it = todo[idx]
        pos = f"[{idx + 1}/{len(todo)}]"
        head = f"{pos} {it.kind.upper()} « {it.title} » ({it.year or 'année ?'}) — note {it.rating or '—'}/10 — statut : {it.status}"
        log("\n" + head)
        if it.imdb_id:
            cl, cy = (it.imdb_title, it.imdb_year) if it.imdb_title else label_of(it.imdb_id, it.kind_slug)
            log(f"  IMDb courant : {it.imdb_id}" + (f"  « {cl} » ({cy})" if cl else ""))
        else:
            initial = next(
                (c["imdb_id"] for c in (it.candidates or []) if c.get("type_imdb") == "imdb_suggestion"),
                None,
            )
            if initial and it.imdb_title:
                log(f"  IMDb initial : {initial}  « {it.imdb_title} » ({it.imdb_year})")
            else:
                log("  Aucun ID IMDb retenu.")
        for c in (it.candidates or [])[:4]:
            t = c.get("titre")
            if not t or t in ("mapping IMDb initial", "proposition TMDB"):
                cl, _ = label_of(c["imdb_id"], it.kind_slug)
                t = cl or t
            sig = c.get("signaux")
            log(f"    - {c['imdb_id']} « {t} » ({c.get('annee')}) [{c.get('type_imdb')}]"
                + (f" — {sig}" if sig else ""))
        if it.url_path:
            log(f"  Allociné : {it.allocine_url}")
        choices = []
        if it.imdb_id:
            choices.append(q.Choice("Valider le mapping actuel", value="validate"))
        initial = next(
            (c["imdb_id"] for c in (it.candidates or []) if c.get("type_imdb") == "imdb_suggestion"),
            None,
        )
        if initial:
            cl, cy = label_of(initial, it.kind_slug)
            label = cl or initial
            choices.append(q.Choice(f"Garder la proposition IMDb : {label} ({cy or '?'})", value="keep"))
        alt = next((c for c in (it.candidates or []) if c.get("type_imdb") == "tmdb"), None)
        if alt:
            cl, cy = label_of(alt["imdb_id"], it.kind_slug)
            label = cl or alt.get("titre") or alt["imdb_id"]
            choices.append(q.Choice(f"Prendre la proposition TMDB : {label} ({cy or alt.get('annee') or '?'})", value="tmdb"))
        choices.extend([
            q.Choice("Saisir un ID IMDb manuellement", value="manual"),
            q.Choice("Rechercher une correspondance avec TMDB", value="search"),
            q.Choice("Exclure cet item de l'import", value="exclude"),
            q.Choice("Passer cet item", value="skip"),
            q.Choice("Quitter la revue et sauvegarder", value="quit"),
        ])
        try:
            action = q.select("Que voulez-vous faire ?", choices=choices, instruction="↑↓ puis Entrée").ask()
        except (EOFError, KeyboardInterrupt):
            log("\n(fin de revue — sauvegarde)")
            break
        if action in (None, "quit"):
            log("Sauvegarde…")
            break
        if action == "skip":
            idx += 1
            continue
        if action == "validate":
            if not it.imdb_id:
                continue
            reviewed_ok[it.cache_key] = True
            it.reviewed = True
            save_json(ok_path, reviewed_ok)
            idx += 1
            continue
        if action == "exclude":
            it.imdb_id = None
            it.status = "excluded"
            it.reviewed = True
            reviewed_ok[it.cache_key] = {"action": "exclude"}
            save_json(ok_path, reviewed_ok)
            idx += 1
            continue
        if action == "keep":
            if not initial:
                log("  pas d'ID IMDb initial à conserver")
                continue
            apply_mapping(it, initial)
            log(f"  → retenu : {initial}")
            idx += 1
            continue
        if action == "tmdb":
            if not alt:
                log("  aucune proposition TMDB disponible — essayez 'v'")
                continue
            apply_mapping(it, alt["imdb_id"])
            log(f"  → retenu : {alt['imdb_id']}")
            idx += 1
            continue
        if action == "manual":
            new_id = q.text("ID IMDb (tt…) :").ask()
            if new_id is None:
                break
            new_id = new_id.strip().lower()
            if not IMDB_ID_RE.fullmatch(new_id):
                log("  format d'ID invalide")
                continue
            if tmdb_key:
                try:
                    r = tmdb_find_imdb(session, tmdb_key, new_id, it.kind_slug, delay_tmdb)
                except (TmdbAuthError, requests.RequestException):
                    r = None
                if r:
                    log(f"  ce pointe vers : « {r['title']} » ({r['year']})")
                else:
                    log("  introuvable dans TMDB")
                    decision = q.select(
                        "Appliquer malgré l'absence dans TMDB ?",
                        choices=[
                            q.Choice("Appliquer cet ID", value=True),
                            q.Choice("Annuler", value=False),
                        ],
                    ).ask()
                    if decision is not True:
                        continue
            apply_mapping(it, new_id)
            idx += 1
            continue
        if action == "search":
            if not tmdb_key:
                log("  nécessite une clé TMDB")
                continue
            try:
                new_id, st, ntitle, nyear = tmdb_resolve_item(session, tmdb_key, it, delay_tmdb)
            except (TmdbAuthError, requests.RequestException) as e:
                log(f"  erreur TMDB : {e}")
                continue
            if not new_id:
                log(f"  TMDB ne propose rien de concluant ({st})")
                continue
            log(f"  TMDB propose : {new_id} « {ntitle} » ({nyear})")
            if it.imdb_id == new_id:
                log("  (identique à l'ID courant — pas de changement)")
                continue
            decision = q.select(
                "Appliquer cette proposition ?",
                choices=[
                    q.Choice("Appliquer cette correspondance", value=True),
                    q.Choice("Retourner au menu", value=False),
                ],
            ).ask()
            if decision is True:
                apply_mapping(it, new_id)
                idx += 1
            continue
        log("  action inconnue")

    save_json(ok_path, reviewed_ok)


def decode_obfuscated_link(cls):
    """Décodage des liens obfusqués d'Allociné (classe CSS = base64 du chemin, séparateur 'ACr')."""
    candidates = [cls.replace("ACr", ""), cls]
    for cand in candidates:
        try:
            padded = cand + "=" * (-len(cand) % 4)
            path = base64.b64decode(padded, validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if path.startswith("/film/") or path.startswith("/series/"):
            return path
    return None


def load_env_file(path):
    env = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def store_tmdb_key(path, key):
    """Ajoute/remplace TMDB_API_KEY sans afficher ni écraser les autres variables."""
    lines = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    output = []
    for line in lines:
        if line.strip().startswith("TMDB_API_KEY="):
            output.append(f"TMDB_API_KEY={key}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"TMDB_API_KEY={key}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    path.chmod(0o600)


def run_wizard(args):
    """Collecte les paramètres courants sans exposer les options internes du script."""
    q = get_questionary()
    env_path = Path(__file__).resolve().parent / ".env"
    env = load_env_file(env_path)
    print("\n=== Export AlloCiné → Trakt ===", flush=True)
    print("Les données restent sur votre ordinateur. Le profil AlloCiné doit être public.\n", flush=True)

    url = args.url or ""
    while not MEMBER_RE.search(url):
        url = q.text("URL du profil AlloCiné :").ask()
        if url is None:
            raise EOFError
        url = url.strip()
        if url and not re.match(r"^https?://", url):
            url = "https://" + url
        if not MEMBER_RE.search(url):
            print("URL invalide : utilisez une URL de type https://www.allocine.fr/membre-Z.../", flush=True)
    args.url = url

    if args.date is None:
        raw_date = q.text("Date d'import (AAAA-MM-JJ, Entrée = aujourd'hui) :", default="").ask()
        if raw_date is None:
            raise EOFError
        raw_date = raw_date.strip()
        if raw_date:
            args.date = raw_date

    if args.kinds == "films,series":
        kinds = q.select(
            "Que voulez-vous exporter ?",
            choices=[
                q.Choice("Films et séries", value="films,series"),
                q.Choice("Films uniquement", value="films"),
                q.Choice("Séries uniquement", value="series"),
            ],
        ).ask()
        if kinds is None:
            raise EOFError
        args.kinds = kinds

    if not args.tmdb_key and not env.get("TMDB_API_KEY"):
        key = q.password("Clé TMDB facultative (laisser vide pour continuer sans) :").ask()
        if key is None:
            raise EOFError
        key = key.strip()
        if key:
            store_tmdb_key(env_path, key)
            args.tmdb_key = str(env_path)
            print("Clé TMDB enregistrée localement dans .env.", flush=True)
    elif env.get("TMDB_API_KEY"):
        print("Clé TMDB détectée dans .env.", flush=True)

    if not args.review:
        review = q.select(
            "Lancer la validation interactive après l'export ?",
            choices=[
                q.Choice("Oui, valider les items en doute", value=True),
                q.Choice("Non, générer uniquement l'export", value=False),
            ],
        ).ask()
        if review is None:
            raise EOFError
        args.review = review
    return args


def item_to_cache(it):
    fields = ("kind_slug", "allocine_id", "title", "url_path", "rating_xx", "rating")
    return {k: getattr(it, k) for k in fields}


@dataclass
class Item:
    kind_slug: str          # "films" | "series"
    allocine_id: str
    title: str              # titre VF tel qu'affiché sur le profil
    url_path: str           # chemin de la fiche Allociné
    rating_xx: int | None   # note brute 0..50
    rating: int | None      # note Trakt 1..10
    year: int | None = None
    original_title: str | None = None
    duration_min: int | None = None
    actors: list | None = None          # casting Allociné (JSON-LD)
    director: str | None = None
    imdb_id: str | None = None
    imdb_title: str | None = None
    imdb_year: int | None = None
    status: str = "pending"
    candidates: list = field(default_factory=list)
    tmdb_outcome: str | None = None   # confirm / mismatch / neutral / error
    cross_validated: bool = False
    reviewed: bool = False

    @property
    def kind(self):
        return KINDS[self.kind_slug]

    @property
    def cache_key(self):
        return f"{self.kind_slug}-{self.allocine_id}"

    @property
    def allocine_url(self):
        return BASE_URL + self.url_path

    def query_candidates(self):
        qs = []
        if self.original_title:
            qs.append(self.original_title)
        t = re.sub(r"\s*\((?:\d{4})\)\s*$", "", self.title).strip()
        qs.append(t)
        m = re.match(r"^(.*?)\s*:", t)
        if m and m.group(1).strip():
            qs.append(m.group(1).strip())
        m = re.match(r"^(.*?)\s*[-–]\s", t)
        if m and m.group(1).strip():
            qs.append(m.group(1).strip())
        out, seen = [], set()
        for q in qs:
            n = normalize_title(q)
            if q and n and n not in seen:
                seen.add(n)
                out.append(q)
        return out


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Accept": "text/html,application/json,*/*;q=0.8",
    })
    return s


def http_get(session, url, retries=3, base_delay=2.0, not_found_ok=False):
    """GET avec relances sur 429/5xx/erreurs réseau. Retourne None sur 404 si not_found_ok."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=(10, 30))
        except requests.RequestException as e:
            last = repr(e)
        else:
            if r.status_code == 404 and not_found_ok:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
            else:
                return r
        wait = base_delay * (2 ** attempt)
        log(f"    attente {wait:.0f}s ({last}) tentative {attempt + 1}/{retries + 1}")
        time.sleep(wait)
    raise RuntimeError(f"Échec HTTP après {retries + 1} tentatives : {url} ({last})")


def page_number_from_url(url):
    m = re.search(r"[?&]page=(\d+)", url or "")
    return int(m.group(1)) if m else None


def discover_total_pages(session, list_url):
    r = http_get(session, list_url + "?page=999")
    n = page_number_from_url(r.url)
    if n and n < 999:
        return n
    nums = [int(x) for x in re.findall(r"[?&]page=(\d+)", r.text)]
    return max(nums) if nums else None


def parse_cards(html):
    soup = BeautifulSoup(html, PARSER)
    cards = []
    for card in soup.select(CARD_SELECTOR):
        meta = card.select_one(TITLE_LINK_SELECTOR)
        title, path = None, None
        if meta is not None:
            title = (meta.get("title") or meta.get_text(strip=True) or "").strip()
            if meta.name == "a" and meta.get("href"):
                path = meta["href"]
            else:
                for cls in (meta.get("class") or []):
                    if cls.startswith("ACr"):
                        path = decode_obfuscated_link(cls)
                        if path:
                            break
        if path is None:
            a = card.find("a", href=re.compile(r"/(?:film|series)/"))
            if a is not None and a.get("href"):
                path = a["href"]
        if not title:
            img = card.find("img", class_="thumbnail-img")
            if img is not None and img.get("alt"):
                title = re.sub(r"^poster de\s+", "", img["alt"]).strip()
        if not path or not title:
            continue
        m = ALLOCINE_ID_RE.search(path)
        if not m:
            continue
        kind_slug = "films" if "/film/" in path else "series"
        xx = None
        rdiv = card.select_one(".rating-mdl")
        if rdiv is not None:
            rm = RATING_CLASS_RE.search(" ".join(rdiv.get("class") or []))
            if rm:
                xx = int(rm.group(1))
        cards.append((title, path, kind_slug, m.group(1), xx))
    return cards


def scrape_kind(session, member_id, kind_slug, max_pages):
    list_url = f"{BASE_URL}/membre-{member_id}/{kind_slug}/"
    total = discover_total_pages(session, list_url)
    if total is not None and total >= 999:
        total = None
    log(f"[{kind_slug}] {total or '?'} pages détectées")
    page, items, seen = 1, [], set()
    while True:
        if total is not None and page > total:
            break
        if max_pages and page > max_pages:
            break
        if page > 1000:
            log(f"[{kind_slug}] garde-fou 1000 pages atteint, arrêt")
            break
        r = http_get(session, f"{list_url}?page={page}")
        final_page = page_number_from_url(r.url)
        if final_page is not None and final_page < page:
            break
        cards = parse_cards(r.text)
        if not cards:
            break
        new = 0
        for title, path, card_kind, allocine_id, xx in cards:
            key = (card_kind, allocine_id)
            if key in seen:
                continue
            seen.add(key)
            items.append(Item(
                kind_slug=card_kind,
                allocine_id=allocine_id,
                title=title,
                url_path=path,
                rating_xx=xx,
                rating=(xx // 5) if xx else None,
            ))
            new += 1
        log(f"[{kind_slug}] page {page}" + (f"/{total}" if total else "") + f" — {len(items)} items ({new} nouveaux)")
        page += 1
    return items


def extract_year(title_text):
    if not title_text:
        return None
    patterns = (
        # "- Film documentaire 2018 - AlloCiné" : groupe de mots tolérant avant l'année
        r"[-–]\s*(?:[^\d\s-][^\d-]{0,25})?\s*(\d{4})\s*[-–]\s*AlloCiné",
        r"(?:Film\s+documentaire|Film|Documentaire|Série TV|Mini-série|Téléfilm)\s+(\d{4})",
    )
    for p in patterns:
        m = re.search(p, title_text, re.I)
        if m:
            return int(m.group(1))
    return None


def extract_original_title(html):
    m = re.search(r"Titre original\s*:?\s*</span>\s*<strong[^>]*>(.*?)</strong>", html, re.S)
    if not m:
        return None
    txt = re.sub(r"<[^>]+>", "", m.group(1))
    return txt.strip() or None


def enrich_item(session, item, cache_dir, delay):
    """Fiche Allociné (année, titre original, crédits) en un seul fetch, cache `detail/`."""
    cpath = cache_dir / "detail" / f"{item.cache_key}.json"
    d = load_json(cpath, {})
    if d.get("not_found"):
        return
    if not d:
        time.sleep(delay)
        r = http_get(session, item.allocine_url, not_found_ok=True)
        if r is None:
            item.status = "detail_error"
            save_json(cpath, {"not_found": True})
            return
        soup = BeautifulSoup(r.text, PARSER)
        title_tag = soup.title.string if soup.title and soup.title.string else ""
        minutes, actors, directors = extract_credits(r.text)
        d = {
            "year": extract_year(title_tag),
            "original_title": extract_original_title(r.text),
            "credits_checked": True,
            "duration_min": minutes,
            "actors": actors,
            "director": directors,
        }
        save_json(cpath, d)
    item.year = d.get("year")
    item.original_title = d.get("original_title")
    item.duration_min = d.get("duration_min")
    item.actors = d.get("actors")
    item.director = d.get("director")


def imdb_suggest(session, query):
    url = IMDB_SUGGEST_URL.format(urllib.parse.quote(query.strip().lower(), safe=""))
    r = session.get(url, timeout=(10, 20))
    r.raise_for_status()
    return r.json().get("d", [])


def resolve_item(session, item, imdb_cache, imdb_cache_path, delay_imdb, overrides):
    """Cascade : titre exact + année exacte → titre exact (année ±1) → année exacte unique."""
    if item.cache_key in overrides:
        item.imdb_id = overrides[item.cache_key].strip()
        item.status = "override"
        return
    cached = imdb_cache.get(item.cache_key)
    if cached is not None:
        item.imdb_id = cached.get("imdb_id")
        item.status = cached.get("status")
        item.imdb_title = cached.get("imdb_title")
        item.imdb_year = cached.get("imdb_year")
        return
    kind_qids = MOVIE_QIDS if item.kind_slug == "films" else SHOW_QIDS
    seen_queries, all_cands, exact_label, ambiguous = set(), {}, {}, False
    resolved, status, chosen = None, None, None
    year = item.year
    try:
        for q in item.query_candidates():
            nq = normalize_title(q)
            if nq in seen_queries:
                continue
            seen_queries.add(nq)
            data = imdb_suggest(session, q)
            time.sleep(delay_imdb)
            cands = [
                x for x in data
                if isinstance(x.get("id"), str)
                and x["id"].startswith("tt")
                and x.get("qid") in kind_qids
            ]
            for c in cands:
                all_cands.setdefault(c["id"], c)
            for c in cands:
                if normalize_title(c.get("l") or "") == nq:
                    exact_label.setdefault(c["id"], c)
            if year:
                ym = {c["id"]: c for c in exact_label.values() if c.get("y") == year}
                if len(ym) == 1:
                    chosen = next(iter(ym.values()))
                    resolved, status = chosen["id"], "ok"
                    break
                if len(ym) > 1:
                    ambiguous = True
    except (requests.RequestException, ValueError):
        item.status = "imdb_error"
        return

    if not resolved:
        # Étape 2 : titre exact (toutes requêtes), année manquante ou ±1
        close = [
            c for c in exact_label.values()
            if c.get("y") is None or (year and abs(c["y"] - year) <= 1)
        ]
        if len(close) == 1:
            chosen = close[0]
            if chosen.get("y") is None:
                resolved, status = chosen["id"], "ok_no_year"
            else:
                resolved, status = chosen["id"], "ok_year_adjusted"
        elif len(close) > 1:
            ambiguous = True

    if not resolved:
        # Étape 3 : un seul candidat (peu importe le label) avec l'année exacte
        if year:
            ym = {c["id"]: c for c in all_cands.values() if c.get("y") == year}
            if len(ym) == 1:
                chosen = next(iter(ym.values()))
                resolved, status = chosen["id"], "ok_year_only"
            elif len(ym) > 1:
                ambiguous = True

    if not resolved:
        # Étape 3b : un seul candidat (peu importe le label) à ±1 an (décalages festival/sortie FR)
        if year:
            near = {
                c["id"]: c
                for c in all_cands.values()
                if c.get("y") is not None and abs(c["y"] - year) <= 1
            }
            if len(near) == 1:
                chosen = next(iter(near.values()))
                resolved, status = chosen["id"], "ok_year_near"
            elif len(near) > 1:
                ambiguous = True

    if resolved:
        item.imdb_id, item.status = resolved, status
        if chosen is not None:
            item.imdb_title, item.imdb_year = chosen.get("l"), chosen.get("y")
    else:
        if ambiguous:
            item.status = "unresolved_ambiguous"
        elif exact_label:
            item.status = "unresolved_year_mismatch"
        else:
            item.status = "unresolved_no_match"
        item.candidates = [
            {"imdb_id": c["id"], "titre": c.get("l"), "annee": c.get("y"), "type_imdb": c.get("qid")}
            for c in list(all_cands.values())[:6]
        ]
    imdb_cache[item.cache_key] = {
        "imdb_id": item.imdb_id, "status": item.status,
        "imdb_title": item.imdb_title, "imdb_year": item.imdb_year,
    }
    save_json(imdb_cache_path, imdb_cache)


def tmdb_get(session, key, path, params):
    params = dict(params)
    params["api_key"] = key
    last = None
    for attempt in range(3):
        try:
            r = session.get(TMDB_BASE + path, params=params, timeout=(10, 20))
        except requests.RequestException as e:
            last = e
        else:
            if r.status_code == 401:
                raise TmdbAuthError("clé TMDB refusée (HTTP 401)")
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
            else:
                return r.json()
        time.sleep(1.0 * (attempt + 1))
    raise requests.RequestException(f"TMDB échec après 3 tentatives : {path} ({last})")


def _tmdb_search_fields(item):
    if item.kind_slug == "films":
        return "/search/movie", "title", "original_title", "release_date", "primary_release_year"
    return "/search/tv", "name", "original_name", "first_air_date", "first_air_date_year"


def tmdb_search(session, key, item, query, with_year, delay):
    time.sleep(delay)
    path, tkey, okey, dkey, ykey = _tmdb_search_fields(item)
    params = {"query": query, "language": "fr-FR", "include_adult": "false"}
    if with_year and item.year:
        params[ykey] = item.year
    results = []
    for r in tmdb_get(session, key, path, params).get("results", [])[:20]:
        ds = r.get(dkey) or ""
        results.append({
            "id": r.get("id"),
            "title": r.get(tkey) or "",
            "orig": r.get(okey) or "",
            "year": int(ds[:4]) if ds[:4].isdigit() else None,
        })
    return results


def tmdb_external_imdb(session, key, tmdb_id, kind_slug, delay):
    time.sleep(delay)
    path = f"/movie/{tmdb_id}/external_ids" if kind_slug == "films" else f"/tv/{tmdb_id}/external_ids"
    return (tmdb_get(session, key, path, {}) or {}).get("imdb_id")


def exact_title_matches(results, nq):
    return [
        r for r in results
        if normalize_title(r["title"]) == nq or normalize_title(r["orig"]) == nq
    ]


def _tmdb_pick(results, nq, year):
    exact = exact_title_matches(results, nq)
    if year:
        ym = [r for r in exact if r["year"] == year]
        if len(ym) == 1:
            return ym[0]
        near = [r for r in exact if r["year"] and abs(r["year"] - year) <= 1]
        if len(near) == 1:
            return near[0]
    if len(exact) == 1:
        return exact[0]
    return None


def tmdb_resolve_item(session, key, item, delay):
    """Fallback TMDB : titre FR/VO exact + année (±1) → imdb_id.
    Retourne (imdb_id|None, statut, titre_tmdb, annee_tmdb)."""
    for q in item.query_candidates():
        nq = normalize_title(q)
        cand = None
        if item.year:
            cand = _tmdb_pick(tmdb_search(session, key, item, q, True, delay), nq, item.year)
        if cand is None:
            cand = _tmdb_pick(tmdb_search(session, key, item, q, False, delay), nq, None)
        if cand is not None:
            imdb = tmdb_external_imdb(session, key, cand["id"], item.kind_slug, delay)
            if imdb and imdb.startswith("tt"):
                return imdb, "ok_tmdb", (cand["title"] or cand["orig"]), cand["year"]
            return None, "tmdb_no_imdb", None, None
    return None, "tmdb_no_match", None, None


def tmdb_check_item(session, key, item, delay):
    """Contre-vérification d'un mapping IMDb existant via TMDB.
    Retourne (statut, imdb_tmdb) : statut ∈ {'confirm', 'mismatch', 'neutral'}
    — seuls les matches exacts titre + année comptent ; rechute sans filtre d'année."""
    for q in item.query_candidates():
        nq = normalize_title(q)
        results = tmdb_search(session, key, item, q, bool(item.year), delay)
        exact = exact_title_matches(results, nq)
        ym = [r for r in exact if item.year and r["year"] == item.year]
        if len(ym) == 1:
            imdb = tmdb_external_imdb(session, key, ym[0]["id"], item.kind_slug, delay)
            if not imdb:
                return "neutral", None
            return ("confirm" if imdb == item.imdb_id else "mismatch"), imdb
        if len(ym) > 1:
            return "neutral", None
        if item.year and not exact:
            results = tmdb_search(session, key, item, q, False, delay)
            exact = exact_title_matches(results, nq)
            if len(exact) == 1:
                imdb = tmdb_external_imdb(session, key, exact[0]["id"], item.kind_slug, delay)
                if not imdb:
                    return "neutral", None
                return ("confirm" if imdb == item.imdb_id else "mismatch"), imdb
    return "neutral", None


# ------------------------------------------------------------------
# Croisement signaux Allociné (JSON-LD) ↔ candidats IMDb/TMDB
# ------------------------------------------------------------------

def person_norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def person_variants(name):
    n = person_norm(name)
    parts = n.split()
    out = {n} if n else set()
    if len(parts) > 1:
        out.add(parts[-1])
        # ordre des jetons inversé selon les sources (noms coréens/japonais :
        # "Ryu Jun-yeol" chez TMDB/IMDb vs "Jun-yeol Ryu" chez Allociné)
        out.add(" ".join(sorted(parts)))
    return out


def person_overlap(names_a, names_b):
    if not names_a or not names_b:
        return None
    a, b = set(), set()
    for x in names_a:
        a |= person_variants(x)
    for x in names_b:
        b |= person_variants(x)
    return len(a & b)


def split_imdb_actors(s):
    if not s:
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts or None


def parse_ld_credits(html):
    """(durée_min, acteurs, réalisateur) depuis le JSON-LD de la fiche Allociné."""
    for block in re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") not in ("Movie", "TVSeries"):
            continue
        minutes = None
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", data.get("duration") or "")
        if m:
            minutes = int(m.group(1) or 0) * 60 + int(m.group(2) or 0) or None
        actors = []
        actor_data = data.get("actor")
        if isinstance(actor_data, dict):
            actor_data = [actor_data]
        for a in (actor_data or [])[:8]:
            p = a.get("actor") if isinstance(a, dict) else a
            name = p.get("name") if isinstance(p, dict) else None
            if name:
                actors.append(name.strip())
        directors = []
        d = data.get("director")
        if isinstance(d, dict):
            d = [d]
        directors = [
            x.get("name").strip() for x in (d or [])
            if isinstance(x, dict) and x.get("name")
        ]
        return minutes, (actors or None), (directors or None)
    return None, None, None


def parse_body_credits(html):
    """Secours (séries) : créateur(s) et casting depuis les blocs meta-body."""
    soup = BeautifulSoup(html, PARSER)
    direction, actors = None, None
    for div in soup.select("div.meta-body-item.meta-body-direction"):
        names = [el.get_text(strip=True) for el in div.select("a.dark-grey-link, span.dark-grey-link")]
        if names:
            direction = names
            break
    for div in soup.select("div.meta-body-item.meta-body-actor"):
        names = [
            re.sub(r"\s*\(.*\)\s*$", "", el.get_text(strip=True)).strip()
            for el in div.select("a.dark-grey-link, span.dark-grey-link")
        ]
        names = [n for n in names if n]
        if names:
            actors = names[:8]
            break
    return direction, actors


def extract_credits(html):
    """(durée_min, acteurs, réalisateurs) — JSON-LD d'abord, blocs meta-body en secours."""
    minutes, actors, directors = parse_ld_credits(html)
    if not actors or not directors:
        d2, a2 = parse_body_credits(html)
        actors = actors or a2
        directors = directors or d2
    return minutes, actors, directors


def enrich_allocine_full(session, item, cache_dir, delay):
    """Crédits depuis `detail/` ; fetch de secours pour les caches antérieurs sans crédits."""
    cpath = cache_dir / "detail" / f"{item.cache_key}.json"
    d = load_json(cpath, {})
    if d.get("not_found"):
        return False
    if not d.get("credits_checked"):
        time.sleep(delay)
        r = http_get(session, item.allocine_url, not_found_ok=True)
        if r is None:
            return False
        minutes, actors, directors = extract_credits(r.text)
        d.update({"credits_checked": True, "duration_min": minutes, "actors": actors, "director": directors})
        save_json(cpath, d)
    item.duration_min = d.get("duration_min")
    item.actors = d.get("actors")
    item.director = d.get("director")
    return bool(item.duration_min or item.actors or item.director)


def imdb_candidates_meta(session, item, cache, cache_path, delay_imdb):
    """Candidats (tt) + casting via l'API suggestion IMDb. Cache imdbcands.json.
    Variante finale « titre + année » (ex. 'five 2016') et relaxation qid flaguée
    (un film documentaire AlloCiné peut être tvMiniSeries/tvSpecial chez IMDb)."""
    if item.cache_key in cache:
        return cache[item.cache_key]
    kind_qids = MOVIE_QIDS if item.kind_slug == "films" else SHOW_QIDS
    queries = item.query_candidates()
    if item.year:
        t0 = re.sub(r"\s*\(\d{4}\)\s*$", "", item.title).strip()
        qy = f"{t0} {item.year}"
        if normalize_title(qy) not in {normalize_title(q) for q in queries}:
            queries.append(qy)
    out, seen_ids, seen_q = [], set(), set()
    try:
        for q in queries:
            nq = normalize_title(q)
            if nq in seen_q:
                continue
            seen_q.add(nq)
            data = imdb_suggest(session, q)
            time.sleep(delay_imdb)
            for x in data:
                if not (isinstance(x.get("id"), str) and x["id"].startswith("tt")):
                    continue
                if x["id"] in seen_ids:
                    continue
                seen_ids.add(x["id"])
                qid = x.get("qid")
                relaxe = qid not in kind_qids
                if relaxe and not (item.kind_slug == "films" and qid in ("tvSeries", "tvMiniSeries", "tvSpecial", "tvMovie")):
                    continue
                out.append({
                    "imdb_id": x["id"], "titre": x.get("l"), "annee": x.get("y"),
                    "type_imdb": qid, "acteurs_imdb": split_imdb_actors(x.get("s")),
                    "qid_relaxe": relaxe,
                })
    except requests.RequestException:
        return []
    cache[item.cache_key] = out
    save_json(cache_path, cache)
    return out


def tmdb_meta_for_imdb(session, key, imdb_id, kind_slug, delay, cache, cache_path):
    """Runtime/cast/réalisateur TMDB pour un ID IMDb (via /find). Cache tmdbmeta.json."""
    if imdb_id in cache:
        return cache[imdb_id]
    find = tmdb_find_imdb(session, key, imdb_id, kind_slug, delay)
    if not find:
        cache[imdb_id] = None
        save_json(cache_path, cache)
        return None
    time.sleep(delay)
    path = f"/movie/{find['id']}" if kind_slug == "films" else f"/tv/{find['id']}"
    d = tmdb_get(session, key, path, {"language": "fr-FR", "append_to_response": "credits"})
    runtime = None
    if kind_slug == "films":
        runtime = d.get("runtime")
    else:
        ert = sorted(d.get("episode_run_time") or [])
        if ert:
            runtime = ert[len(ert) // 2]
    actors = [
        m.get("name") for m in ((d.get("credits") or {}).get("cast") or [])[:10] if m.get("name")
    ]
    director = None
    for c in (d.get("credits") or {}).get("crew") or []:
        if c.get("job") == "Director" and c.get("name"):
            director = c["name"]
            break
    if director is None and kind_slug != "films":
        cb = [x.get("name") for x in (d.get("created_by") or []) if x.get("name")]
        director = ", ".join(cb[:2]) or None
    meta = {
        "tmdb_id": find.get("id"), "titre": find.get("title"), "annee": find.get("year"),
        "acteurs": actors or None, "runtime": runtime, "realisateur": director,
    }
    cache[imdb_id] = meta
    save_json(cache_path, cache)
    return meta


def score_candidate(it, cand):
    """cand: {titre, annee, acteurs, runtime, realisateur} → (score, nb_forts, notes, conflits_durs, conflits_mous)."""
    s, strong, notes = 0, 0, []
    hard, soft = [], []
    if cand.get("annee") is not None and it.year is not None:
        dy = abs(int(cand["annee"]) - it.year)
        if dy == 0:
            s += 1
        elif dy == 1:
            notes.append("année ±1")
        elif dy > 1:
            if it.kind_slug == "films":
                notes.append(f"année Δ{dy} (ressortie ?)")
            else:
                hard.append(f"année Δ{dy}")
    if it.duration_min and cand.get("runtime") and it.kind_slug == "films":
        delta = abs(it.duration_min - int(cand["runtime"]))
        if delta <= 2:
            s += 2; strong += 1
            notes.append(f"durée {it.duration_min}↔{cand['runtime']} min")
        elif delta <= 5:
            s += 1
            notes.append(f"durée ≈ (Δ{delta} min)")
        elif delta > 10:
            soft.append(f"durée Δ{delta} min")
    ov = person_overlap(it.actors, cand.get("acteurs"))
    if ov is not None:
        if ov >= 2:
            s += 2; strong += 1
            notes.append(f"{ov} acteurs communs")
        elif ov == 1:
            s += 1
            notes.append("1 acteur commun")
        elif ov == 0 and len(it.actors or []) >= 3 and len(cand.get("acteurs") or []) >= 3:
            hard.append("casting disjoint")
    if it.director and cand.get("realisateur"):
        dirs_a = it.director if isinstance(it.director, list) else [it.director]
        variants_a = set()
        for dname in dirs_a:
            variants_a |= person_variants(dname)
        if variants_a & person_variants(cand["realisateur"]):
            s += 2; strong += 1
            notes.append("réalisateur identique")
    return s, strong, notes, hard, soft


def cross_verify_pass(session, items, cache_dir, delay, delay_imdb, tmdb_key, delay_tmdb):
    """Vérifie/arbitre via durée+casting+réalisateur : les items en doute ET l'audit des
    items « sûre » jamais audités (décisions persistées, rejouées à chaque run)."""
    cpath = cache_dir / "cross.json"
    cross = load_json(cpath, {})
    tmdbmeta_path = cache_dir / "tmdbmeta.json"
    tmdbmeta = load_json(tmdbmeta_path, {})
    imdbcands_path = cache_dir / "imdbcands.json"
    imdbcands = load_json(imdbcands_path, {})
    reviewed_ok = load_json(cache_dir / "review-ok.json", {})
    targets = []
    for it in items:
        if it.status == "override":
            continue
        if needs_review(it, reviewed_ok):
            targets.append(it)
        elif it.imdb_id and not it.cross_validated:
            targets.append(it)   # audit : item « sûre » jamais vérifié par le moteur
    used_ids = {(x.kind, x.imdb_id) for x in items if x.imdb_id}
    n_audit = len(targets)
    log(f"\nCroisement signaux Allociné : {n_audit} item(s) à vérifier (revue + audit)")
    actions = Counter()

    def replay(it):
        entry = cross.get(it.cache_key)
        if not entry or it.status == "override":
            return
        act = entry.get("action")
        if act == "replace":
            it.imdb_id, it.status = entry.get("imdb_id"), "ok_cross"
        elif act == "downgrade":
            it.imdb_id = None
            it.status = "unresolved_conflict"
            it.candidates = entry.get("cands") or []
        elif act == "upgrade":
            it.cross_validated = True
        actions["replay"] += 1

    for it in targets:
        replay(it)
    if actions["replay"]:
        log(f"  décisions précédentes rejouées : {actions['replay']}")

    for i, it in enumerate(targets, 1):
        if it.cache_key in cross and it.status != "override":
            continue  # décision déjà prise et rejouée
        has_credits = enrich_allocine_full(session, it, cache_dir, delay)
        if not has_credits or not (it.duration_min or it.actors or it.director):
            cross[it.cache_key] = {"action": "skip"}
            save_json(cpath, cross)
            actions["skip"] += 1
            if i % 25 == 0 or i == len(targets):
                log(f"  [croisement {i}/{len(targets)}] {dict(actions)}")
            continue
        # Pool de candidats
        pool = {}
        if it.imdb_id:
            pool[it.imdb_id] = {"imdb_id": it.imdb_id, "titre": it.imdb_title, "annee": it.imdb_year,
                                "type_imdb": it.status}
        for c in (it.candidates or []):
            pool.setdefault(c.get("imdb_id"), c)
        imdb_cands = imdb_candidates_meta(session, it, imdbcands, imdbcands_path, delay_imdb)
        for c in imdb_cands:
            pool.setdefault(c["imdb_id"], c)
        scored = []
        chosen_entry = None
        tmdb_failed = False
        for cid, c in pool.items():
            imdb_actors = (c.get("acteurs_imdb") if "acteurs_imdb" in c else None)
            meta = None
            if tmdb_key:
                try:
                    meta = tmdb_meta_for_imdb(session, tmdb_key, cid, it.kind_slug, delay_tmdb,
                                              tmdbmeta, tmdbmeta_path)
                except (TmdbAuthError, requests.RequestException) as e:
                    log(f"  TMDB indisponible ({e}) — décision sur {it.title!r} reportée au prochain run")
                    tmdb_failed = True
                    break
            cand = {
                "titre": c.get("titre") or (meta or {}).get("titre"),
                "annee": c.get("annee") if c.get("annee") is not None else (meta or {}).get("annee"),
                "acteurs": imdb_actors or (meta or {}).get("acteurs"),
                "runtime": (meta or {}).get("runtime"),
                "realisateur": (meta or {}).get("realisateur"),
            }
            s, strong, notes, hard, soft = score_candidate(it, cand)
            display = "; ".join(notes + [f"⚠ {c}" for c in soft] + [f"✗ {c}" for c in hard])
            entry = {
                "imdb_id": cid, "titre": cand.get("titre"), "annee": cand.get("annee"),
                "type_imdb": c.get("type_imdb") or "imdb",
                "signaux": display,
                "score": s, "strong": strong, "conflicts": hard, "soft": soft,
            }
            scored.append(entry)
            if cid == it.imdb_id:
                chosen_entry = entry
        if tmdb_failed or not scored:
            continue
        scored.sort(key=lambda x: -x["score"])
        best = scored[0]

        if it.imdb_id:
            # vérification du mapping courant
            if chosen_entry is None:
                continue
            better = (best["imdb_id"] != chosen_entry["imdb_id"]
                      and best["strong"] >= 2
                      and best["score"] - chosen_entry["score"] >= 3
                      and not best["conflicts"]
                      and (it.kind, best["imdb_id"]) not in used_ids)
            chosen_disqualified = bool(
                (chosen_entry["conflicts"] or chosen_entry.get("soft"))
                and not chosen_entry["strong"]
            )
            if chosen_disqualified and better:
                # le mapping courant est contredit et un candidat nettement meilleur existe
                it.imdb_id, it.status = best["imdb_id"], "ok_cross"
                used_ids.add((it.kind, best["imdb_id"]))
                cross[it.cache_key] = {
                    "action": "replace", "imdb_id": best["imdb_id"],
                    "evidence": f"{best.get('signaux')} ; remplaçait {chosen_entry['imdb_id']}",
                }
                actions["replace"] += 1
            elif chosen_entry["conflicts"] and not chosen_entry["strong"]:
                it.candidates = it.candidates + [chosen_entry] + ([best] if best["imdb_id"] != it.imdb_id else [])
                it.imdb_id = None
                it.status = "unresolved_conflict"
                cross[it.cache_key] = {
                    "action": "downgrade", "cands": it.candidates,
                    "scores": [{k: e[k] for k in ("imdb_id", "titre", "score", "strong", "signaux")} for e in scored[:4]],
                }
                actions["downgrade"] += 1
            elif chosen_entry["strong"] >= 1 and not chosen_entry["conflicts"]:
                it.cross_validated = True
                cross[it.cache_key] = {
                    "action": "upgrade",
                    "evidence": chosen_entry.get("signaux"),
                }
                actions["upgrade"] += 1
            else:
                cross[it.cache_key] = {"action": "keep"}
                actions["keep"] += 1
        else:
            # arbitrage entre candidats (divergences/ambiguïtés) — la marge se calcule
            # contre les concurrents PLAUSIBLES uniquement (même année ±1) :
            # un candidat d'une autre année (Δ>1) n'est pas le film cherché.
            credible = [
                e for e in scored[1:]
                if e.get("annee") is None or it.year is None or abs(int(e["annee"]) - it.year) <= 1
            ]
            second = credible[0] if credible else None
            if (best["strong"] >= 2
                    and best["score"] - (second["score"] if second else 0) >= 4
                    and (second is None or second["strong"] == 0)
                    and not best["conflicts"]
                    and (it.kind, best["imdb_id"]) not in used_ids):
                it.imdb_id, it.status = best["imdb_id"], "ok_cross"
                used_ids.add((it.kind, best["imdb_id"]))
                cross[it.cache_key] = {
                    "action": "replace", "imdb_id": best["imdb_id"],
                    "evidence": best.get("signaux"),
                }
                actions["replace"] += 1
            else:
                it.candidates = scored[:4]
                cross[it.cache_key] = {
                    "action": "no-decision",
                    "scores": [{k: e[k] for k in ("imdb_id", "titre", "score", "strong", "signaux")} for e in scored[:4]],
                }
                actions["no-decision"] += 1
        if i % 25 == 0 or i == len(targets):
            log(f"  [croisement {i}/{len(targets)}] {dict(actions)}")
            save_json(cpath, cross)  # persistance incrémentale : un run tué est reprenable

    save_json(cpath, cross)
    log(f"  croisement terminé : {dict(actions)}")


WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def wikidata_search_films(session, query, delay, limit=10):
    time.sleep(delay)
    r = session.get(WIKIDATA_API, params={
        "action": "wbsearchentities", "search": query, "language": "fr",
        "format": "json", "type": "item", "limit": str(limit),
    }, timeout=(10, 20))
    r.raise_for_status()
    return [x["id"] for x in r.json().get("search", [])[:limit]]


def wikidata_claims(session, qids, delay):
    time.sleep(delay)
    r = session.get(WIKIDATA_API, params={
        "action": "wbgetentities", "ids": "|".join(qids), "props": "claims", "format": "json",
    }, timeout=(10, 20))
    r.raise_for_status()
    out = {}
    for qid, ent in (r.json().get("entities") or {}).items():
        claims = ent.get("claims") or {}
        imdb, year = None, None
        for sn in claims.get("P345") or []:
            v = ((sn.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if isinstance(v, str) and v.startswith("tt"):
                imdb = v
                break
        for sn in claims.get("P577") or []:
            t = (((sn.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("time")
            if t:
                y = re.search(r"(\d{4})", t)
                if y:
                    year = int(y.group(1))
                    break
        if imdb:
            out[qid] = {"imdb_id": imdb, "annee": year}
    return out


def wikidata_pass(session, items, cache_dir, delay):
    """Dernier recours sans clé : Wikidata P345, accepté si candidat unique + année exacte."""
    cpath = cache_dir / "wikidata.json"
    cache = load_json(cpath, {})
    targets = [it for it in items if not it.imdb_id and it.status.startswith("unresolved")]
    if not targets:
        return
    log(f"Wikidata : {len(targets)} item(s) encore sans mapping")
    resolved = 0
    for i, it in enumerate(targets, 1):
        cached = cache.get(it.cache_key)
        if cached is not None:
            if cached.get("imdb_id"):
                it.imdb_id, it.status = cached["imdb_id"], "ok_wikidata"
                resolved += 1
            continue
        found = None
        try:
            for q in it.query_candidates()[:2]:
                qids = wikidata_search_films(session, q, delay)
                if not qids:
                    continue
                claims = wikidata_claims(session, qids, delay)
                exact = [c for c in claims.values() if it.year and c.get("annee") == it.year]
                if len(exact) == 1:
                    found = exact[0]
                    break
        except requests.RequestException:
            continue
        if found:
            it.imdb_id, it.status = found["imdb_id"], "ok_wikidata"
            resolved += 1
            log(f"  + Wikidata : {it.title} ({it.year}) → {found['imdb_id']}")
        cache[it.cache_key] = {"imdb_id": found["imdb_id"] if found else None}
        save_json(cpath, cache)
    log(f"  Wikidata : {resolved} résolu(s)")


def build_trakt_entries(items, ts):
    entries = []
    seen_index = {}
    for it in items:
        if not it.imdb_id or it.status.startswith("unresolved"):
            continue
        e = {"imdb_id": it.imdb_id, "type": it.kind, "watched_at": ts}
        if it.rating:
            e["rating"] = it.rating
            e["rated_at"] = ts
        key = (it.kind, it.imdb_id)
        if key in seen_index:
            if entries[seen_index[key]] == e:
                continue  # doublon strictement identique : inutile pour Trakt
        else:
            seen_index[key] = len(entries)
        entries.append(e)
    return entries


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        w.writerows(rows)


def write_report(items, path, reviewed_ok):
    rows = []
    for it in items:
        note_ac = f"{it.rating_xx / 10:.1f}".replace(".", ",") if it.rating_xx is not None else ""
        rows.append([
            it.kind, it.allocine_id, it.title, it.original_title or "", it.year or "",
            note_ac, it.rating or "", it.imdb_id or "",
            it.imdb_title or "", it.imdb_year or "",
            confidence_level(it, reviewed_ok), it.status, it.allocine_url,
        ])
    write_csv(path, [
        "type", "allocine_id", "titre", "titre_original", "annee",
        "note_allocine_sur5", "note_trakt_sur10", "imdb_id",
        "titre_imdb", "annee_imdb", "confiance", "statut", "url_allocine",
    ], rows)


def write_review_list(items, path, reviewed_ok):
    """CSV des items à valider manuellement, triés par niveau de risque."""
    todo = [it for it in items if needs_review(it, reviewed_ok)]
    todo.sort(key=review_sort_key)
    rows = []
    for it in todo:
        alts = " | ".join(
            f"{c['imdb_id']} ({c.get('titre')}, {c.get('annee')}) [{c.get('type_imdb')}]"
            + (f" {{ {c['signaux']} }}" if c.get("signaux") else "")
            for c in (it.candidates or [])[:4]
        )
        rows.append([
            it.kind, it.title, it.year or "", it.rating or "", it.imdb_id or "",
            it.imdb_title or "", it.imdb_year or "", it.status, alts, it.allocine_url,
        ])
    write_csv(path, [
        "type", "titre_allocine", "annee", "note_trakt", "imdb_id",
        "titre_imdb", "annee_imdb", "statut", "alternatives", "url_allocine",
    ], rows)


def write_unresolved(items, path):
    data = []
    for it in items:
        if it.status.startswith("unresolved") or it.status in ("detail_error", "excluded"):
            data.append({
                "type": it.kind,
                "allocine_id": it.allocine_id,
                "titre": it.title,
                "titre_original": it.original_title,
                "annee": it.year,
                "note_trakt": it.rating,
                "url_allocine": it.allocine_url,
                "raison": it.status,
                "imdb_actuel": it.imdb_id if it.status == "unresolved_mismatch" else None,
                "candidats_imdb": it.candidates,
            })
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_entries(entries):
    errors = []
    seen = set()
    for e in entries:
        iid = e.get("imdb_id", "")
        if not IMDB_ID_RE.fullmatch(iid):
            errors.append(f"imdb_id invalide : {iid!r}")
        if e.get("type") not in ("movie", "show"):
            errors.append(f"type invalide : {e.get('type')!r}")
        for k in ("watched_at", "rated_at"):
            v = e.get(k)
            if v is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", v):
                errors.append(f"{k} invalide : {v!r}")
        r = e.get("rating")
        if r is not None and not (isinstance(r, int) and 1 <= r <= 10):
            errors.append(f"rating invalide : {r!r}")
        key = (e.get("type"), iid)
        if key in seen:
            errors.append(f"doublon : {key}")
        seen.add(key)
    return errors


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Export des notes Allociné d'un profil public vers un JSON importable sur Trakt.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--url", default=None, help="URL du profil Allociné (absente : assistant interactif)")
    p.add_argument("--date", default=None, help="date fixe watched_at/rated_at, AAAA-MM-JJ (défaut : aujourd'hui UTC)")
    p.add_argument("--delay", type=float, default=1.2, help="délai entre requêtes fiches Allociné (s)")
    p.add_argument("--delay-imdb", type=float, default=0.3, help="délai entre requêtes IMDb (s)")
    p.add_argument("--output-dir", default=".", help="dossier des sorties")
    p.add_argument("--kinds", default="films,series", help="listes à exporter : films,series")
    p.add_argument("--max-pages", type=int, default=0, help="debug : limiter le nombre de pages par type (0 = tout)")
    p.add_argument("--limit", type=int, default=0, help="debug : limiter le nombre d'items traités (0 = tout ; export partiel possible)")
    p.add_argument("--overrides", default=None, help="JSON de mappings manuels (créé automatiquement s'il manque)")
    p.add_argument("--tmdb-key", default=None, help="clé API TMDB (v3) : chaîne, chemin de fichier, ou TMDB_API_KEY dans .env")
    p.add_argument("--delay-tmdb", type=float, default=0.25, help="délai entre requêtes TMDB (s)")
    p.add_argument("--no-tmdb-check", action="store_true", help="désactiver la contre-vérification TMDB (fallback seul)")
    p.add_argument("--refresh-scrape", action="store_true", help="re-scraper le profil au lieu d'utiliser le cache des items")
    p.add_argument("--wizard", action="store_true", help="assistant interactif de premier lancement")
    p.add_argument("--review", action="store_true", help="revue interactive des items en doute (décisions persistées)")
    p.add_argument("--retry-unresolved", action="store_true", help="retenter les résolutions IMDb en échec")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.wizard or not args.url:
        if not sys.stdin.isatty():
            log("Erreur : aucune URL fournie. Utilisez --url en mode non interactif, ou lancez le programme dans un terminal.")
            return 2
        args = run_wizard(args)
    member_m = MEMBER_RE.search(args.url or "")
    if not member_m:
        log("Erreur : URL de profil Allociné invalide (attendu .../membre-ZXXXX.../)")
        return 1
    member_id = member_m.group(1)

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not DATE_RE.fullmatch(date_str):
        log(f"Erreur : date invalide {date_str!r} (attendu AAAA-MM-JJ)")
        return 1
    ts = f"{date_str}T12:00:00Z"

    requested = [k.strip() for k in args.kinds.split(",") if k.strip()]
    ignored = [k for k in requested if k not in KINDS]
    if ignored:
        log(f"Attention : --kinds ignoré pour {', '.join(ignored)} (valeurs possibles : films, series)")
    kinds = [k for k in requested if k in KINDS]
    if not kinds:
        log(f"Erreur : --kinds invalide {args.kinds!r}")
        return 1

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "cache"
    (cache_dir / "detail").mkdir(parents=True, exist_ok=True)
    imdb_cache_path = cache_dir / "imdb.json"

    overrides_path = Path(args.overrides) if args.overrides else out_dir / "overrides.json"
    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    overrides = load_json(overrides_path, {})
    if not overrides_path.is_file():
        save_json(overrides_path, overrides, indent=2)
        log(f"Fichier de mappings créé : {overrides_path}")

    imdb_cache = load_json(imdb_cache_path, {})
    if args.retry_unresolved:
        tmdb_path, cross_path = cache_dir / "tmdb.json", cache_dir / "cross.json"
        tmdb_cache, cross = load_json(tmdb_path, {}), load_json(cross_path, {})
        stale = {k for k, v in imdb_cache.items() if str(v.get("status", "")).startswith("unresolved")}
        stale |= {k for k, v in tmdb_cache.items() if v.get("check") == "mismatch"}
        stale |= {k for k, v in cross.items() if v.get("action") == "downgrade"}
        n_imdb = sum(1 for k in stale if imdb_cache.pop(k, None) is not None)
        n_tmdb = sum(1 for k in stale if tmdb_cache.pop(k, None) is not None)
        n_cross = sum(1 for k in stale if cross.pop(k, None) is not None)
        for path, data, n in ((imdb_cache_path, imdb_cache, n_imdb),
                              (tmdb_path, tmdb_cache, n_tmdb),
                              (cross_path, cross, n_cross)):
            if n:
                save_json(path, data)
        if stale:
            log(f"--retry-unresolved : {n_imdb} clé(s) imdb.json, {n_tmdb} tmdb.json, {n_cross} cross.json purgée(s)")
        else:
            log("--retry-unresolved : aucune clé en échec dans les caches")

    session = make_session()

    ipath = cache_dir / f"items-{member_id}.json"
    items = None
    if ipath.is_file() and not args.refresh_scrape:
        try:
            loaded = [Item(**d) for d in json.loads(ipath.read_text(encoding="utf-8"))]
            items = [it for it in loaded if it.kind_slug in kinds]
            log(f"Profil en cache ({ipath.name}) : {len(items)} items — utilisez --refresh-scrape pour re-scraper")
        except (json.JSONDecodeError, TypeError):
            items = None
    if items is None:
        items = []
        for kind_slug in kinds:
            items.extend(scrape_kind(session, member_id, kind_slug, args.max_pages))
        log(f"Total items scrapés : {len(items)}")
        ipath.write_text(
            json.dumps([item_to_cache(it) for it in items], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    if args.limit:
        items = items[: args.limit]
        log(f"Attention : --limit={args.limit} → run partiel sur {len(items)} item(s) ; le cache des items reste complet.")

    review_decisions = load_json(cache_dir / "review-ok.json", {})
    for it in items:
        decision = review_decisions.get(it.cache_key)
        if isinstance(decision, dict) and decision.get("action") == "exclude":
            it.imdb_id = None
            it.status = "excluded"
            it.reviewed = True

    n = len(items)
    log(f"\nPhase 1/5 — fiches Allociné et résolution IMDb de {n} item(s) (cache réutilisé quand disponible)")
    for i, it in enumerate(items, 1):
        if it.status == "excluded":
            continue
        try:
            enrich_item(session, it, cache_dir, args.delay)
        except (requests.RequestException, RuntimeError):
            it.status = "detail_error"
        resolve_item(session, it, imdb_cache, imdb_cache_path, args.delay_imdb, overrides)
        if i % 25 == 0 or i == n:
            log(f"[{i}/{n}] traités — statuts : {dict(Counter(x.status for x in items))}")

    # ---- TMDB : fallback sur les non résolus + contre-vérification ----
    log("\nPhase 2/5 — vérifications TMDB")
    env = load_env_file(Path(__file__).resolve().parent / ".env")
    tmdb_key = args.tmdb_key or env.get("TMDB_API_KEY")
    if tmdb_key:
        kpath = Path(tmdb_key)
        tmdb_key = kpath.read_text(encoding="utf-8").strip() if kpath.is_file() else tmdb_key
        if not tmdb_key:
            log("TMDB : clé vide — passes TMDB ignorées")
    if tmdb_key:
        try:
            tmdb_get(session, tmdb_key, "/configuration", {})
        except (TmdbAuthError, requests.RequestException) as e:
            log(f"TMDB indisponible ({e}) — passes TMDB ignorées")
            tmdb_key = None
    if tmdb_key:
        tmdb_cache_path = cache_dir / "tmdb.json"
        tmdb_cache = {}
        if tmdb_cache_path.is_file():
            tmdb_cache = json.loads(tmdb_cache_path.read_text(encoding="utf-8"))

        to_resolve = [
            it for it in items
            if not it.imdb_id and (
                it.status.startswith("unresolved") or it.status in ("imdb_error", "detail_error")
            )
        ]
        log(f"\nTMDB fallback : {len(to_resolve)} item(s) non résolu(s) par IMDb")
        for i, it in enumerate(to_resolve, 1):
            cached = tmdb_cache.get(it.cache_key)
            if cached is not None:
                if cached.get("imdb_id"):
                    it.imdb_id, it.status = cached["imdb_id"], cached.get("status", "ok_tmdb")
                    it.imdb_title = cached.get("imdb_title")
                    it.imdb_year = cached.get("imdb_year")
                continue
            try:
                imdb, st, ttitle, tyear = tmdb_resolve_item(session, tmdb_key, it, args.delay_tmdb)
            except (TmdbAuthError, requests.RequestException):
                it.status = "tmdb_error"
                continue
            if imdb:
                it.imdb_id, it.status = imdb, st
                it.imdb_title, it.imdb_year = ttitle, tyear
            tmdb_cache[it.cache_key] = {
                "imdb_id": imdb, "status": it.status if imdb else None,
                "imdb_title": ttitle, "imdb_year": tyear,
            }
            save_json(tmdb_cache_path, tmdb_cache)
            if i % 10 == 0 or i == len(to_resolve):
                log(f"  [tmdb-fallback {i}/{len(to_resolve)}] {it.title} → {it.status}")

        if not args.no_tmdb_check:
            to_check = [
                it for it in items
                if it.imdb_id and it.status not in ("override", "ok_tmdb")
                and not it.status.startswith("unresolved")
            ]
            log(f"TMDB contre-vérification : {len(to_check)} item(s)")
            confirmed = mismatched = 0
            for i, it in enumerate(to_check, 1):
                ck = tmdb_cache.get(it.cache_key) or {}
                outcome, tmdb_imdb = ck.get("check"), ck.get("tmdb_imdb")
                if outcome is None or (outcome == "mismatch" and not tmdb_imdb):
                    try:
                        outcome, tmdb_imdb = tmdb_check_item(session, tmdb_key, it, args.delay_tmdb)
                    except (TmdbAuthError, requests.RequestException):
                        outcome, tmdb_imdb = "error", None
                    if outcome != "error":
                        tmdb_cache[it.cache_key] = {"check": outcome, "tmdb_imdb": tmdb_imdb}
                        save_json(tmdb_cache_path, tmdb_cache)
                it.tmdb_outcome = outcome
                if outcome == "mismatch":
                    mismatched += 1
                    cands = [{"imdb_id": it.imdb_id, "titre": "mapping IMDb initial", "annee": it.year,
                              "type_imdb": "imdb_suggestion"}]
                    if tmdb_imdb:
                        cands.append({"imdb_id": tmdb_imdb, "titre": "proposition TMDB", "annee": it.year,
                                      "type_imdb": "tmdb"})
                    it.candidates = it.candidates + cands
                    it.imdb_id = None
                    it.status = "unresolved_mismatch"
                elif outcome == "confirm":
                    confirmed += 1
                if i % 50 == 0 or i == len(to_check):
                    log(f"  [tmdb-check {i}/{len(to_check)}] confirmés={confirmed} divergences={mismatched}")
            log(f"TMDB : confirmés={confirmed}, divergences={mismatched} (à revoir ci-dessous)")

    # ---- Croisement signaux Allociné (JSON-LD) vs candidats, puis Wikidata ----
    log("\nPhase 3/5 — recherche de derniers mappings via Wikidata")
    wikidata_pass(session, items, cache_dir, args.delay_tmdb)
    log("\nPhase 4/5 — audit durée, casting et réalisateur")
    cross_verify_pass(session, items, cache_dir, args.delay, args.delay_imdb, tmdb_key, args.delay_tmdb)

    reviewed_ok = load_json(cache_dir / "review-ok.json", {})

    def write_outputs():
        entries = build_trakt_entries(items, ts)
        errors = validate_entries(entries)
        trakt_path = out_dir / "trakt-import.json"
        trakt_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        write_report(items, out_dir / "report.csv", reviewed_ok)
        write_unresolved(items, out_dir / "unresolved.json")
        write_review_list(items, out_dir / "review.csv", reviewed_ok)
        return entries, errors

    log("\nPhase 5/5 — génération des fichiers")
    entries, errors = write_outputs()

    if args.review:
        run_review(session, items, tmdb_key, cache_dir, overrides_path, args.delay_tmdb)
        reviewed_ok = load_json(cache_dir / "review-ok.json", {})
        entries, errors = write_outputs()

    log("\n===== RÉSUMÉ =====")
    by_kind = Counter(it.kind for it in items)
    by_status = Counter(it.status for it in items)
    by_conf = Counter(confidence_level(it, reviewed_ok) for it in items)
    log(f"Items Allociné : {len(items)} ({dict(by_kind)})")
    log(f"Statuts : {dict(by_status)}")
    log(f"Confiance : {dict(by_conf)}")
    log(f"Entrées trakt-import.json : {len(entries)}")
    if errors:
        log(f"VALIDATION : {len(errors)} erreur(s)")
        for e in errors[:10]:
            log(f"  - {e}")
        log("N'importez pas le fichier tant que la validation n'est pas OK.")
    else:
        log("VALIDATION : OK (IDs, dates, notes, unicité)")
    n_review = sum(1 for it in items if needs_review(it, reviewed_ok))
    if n_review:
        log(f"\nÀ valider : {n_review} item(s) en doute → review.csv, ou revue interactive :")
        log(f"  .venv/bin/python allocine_to_trakt.py --url \"{args.url}\" --overrides \"{overrides_path}\" --review")
    elif not errors:
        log("\nEXPORT PRÊT : importez trakt-import.json dans Trakt → Réglages → Importer.")
    log(f"\nFichiers : {out_dir / 'trakt-import.json'}, {out_dir / 'report.csv'}, "
        f"{out_dir / 'unresolved.json'}, {out_dir / 'review.csv'}")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterruption reçue. Les caches permettent de reprendre avec la même commande.", flush=True)
        sys.exit(130)
    except EOFError:
        print("\nAssistant interrompu. Relancez ./export.sh pour recommencer.", flush=True)
        sys.exit(130)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"\nErreur réseau ou d'exécution : {exc}", flush=True)
        print("Relancez la même commande ; les étapes déjà terminées sont conservées dans cache/.", flush=True)
        sys.exit(1)
