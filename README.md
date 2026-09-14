# allocine-to-trakt

Exporte les notes de films et séries d'un profil public AlloCiné vers un JSON importable sur Trakt — en local, sans clé API payante.

- Lit les onglets *films notés* / *séries notées* d'un profil public AlloCiné (titre, note /5, fiche)
- Convertit les notes (0,5–5 → entier 1–10), résout chaque item vers son **ID IMDb** (sans clé, via l'API publique de suggestion IMDb), et recoupe chaque mapping avec TMDB + la durée/le casting/le réalisateur de la fiche AlloCiné
- Génère `trakt-import.json` prêt à importer sur [trakt.tv](https://trakt.tv) (Réglages → Importer), plus des fichiers de contrôle (`report.csv`, `review.csv`, `unresolved.json`)
- Tout est caché : le premier run est le seul long, ensuite chaque run rejoue depuis le cache

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Utilisation

```bash
.venv/bin/python allocine_to_trakt.py --url "https://www.allocine.fr/membre-ZXXXXXXXXXXXXXXXXXXX/" --overrides overrides.json
```

Le profil doit être **public** (URL récupérable sur mon.allocine.fr → Profil).

### Revue interactive

```bash
.venv/bin/python allocine_to_trakt.py --url "..." --overrides overrides.json --review
```

Présente uniquement les items en doute, du plus risqué au moins risqué, avec les candidats alternatifs et leurs signaux (durée, acteurs communs, réalisateur) :

| Action | Effet |
|---|---|
| `Entrée` | valider l'ID affiché |
| `k` | garder l'ID IMDb initial (divergences IMDb/TMDB) |
| `t` | prendre la proposition TMDB |
| `i ttXXXXXXX` | ID IMDb manuel (vérifié via TMDB avant application) |
| `v` | chercher une correspondance via TMDB à la demande |
| `e` | exclure définitivement (validé sans mapping) |
| `s` / `q` | passer / sauvegarder et quitter (reprise possible) |

Les décisions sont persistées (`cache/review-ok.json`, remplacements dans `overrides.json`) : les items validés ne se représentent plus.

### Options principales

| Option | Défaut | Rôle |
|---|---|---|
| `--date AAAA-MM-JJ` | aujourd'hui (12:00 UTC) | date fixe `watched_at`/`rated_at` — AlloCiné ne stocke aucune date de note |
| `--refresh-scrape` | — | re-scraper le profil (sinon `cache/items-*.json` est réutilisé : nécessaire si vous avez noté des films depuis) |
| `--delay` / `--delay-imdb` / `--delay-tmdb` | 1,2 / 0,3 / 0,25 s | délais anti-429 (relances automatiques avec backoff) |
| `--kinds films,series` | les deux | listes à exporter |
| `--overrides` | — | fichier de mappings manuels, prioritaires sur tout |

La clé TMDB (optionnelle mais recommandée) va dans `.env` :

```
TMDB_API_KEY=votre_clé
```

Aucune autre clé n'est nécessaire (IMDb et Wikidata sont publics).

## Fichiers générés (à chaque run)

| Fichier | Contenu |
|---|---|
| `trakt-import.json` | le livrable : `[{"imdb_id", "type": "movie"\|"show", "watched_at", "rating", "rated_at"}, …]` |
| `report.csv` | récapitulatif complet (une ligne par item, séparateur `;`, UTF-8 BOM, colonne `confiance`) |
| `review.csv` | uniquement les items en doute, triés par risque |
| `unresolved.json` | détail des items sans mapping retenu (candidats, raison) |

Le cache vit dans `cache/` (profil, fiches, résolutions IMDb/TMDB, décisions d'arbitrage) — supprimer un fichier de cache change les décisions, voir `AGENTS.md`.

## Modèle de confiance

| Niveau | Signification |
|---|---|
| **sûre** | titre exact + année exacte sur IMDb, **ou** confirmation croisée indépendante (durée ±2 min, ≥2 acteurs communs ou même réalisateur), ou validation manuelle |
| **à vérifier** | correspondance plus faible (année seule, année ±1, Wikidata seul…) → à trancher via `--review` |
| **hors import** | divergences non départagées ou titres absents d'IMDb — exclus du JSON d'import |

Politique volontairement conservatrice : jamais d'application automatique sur un seul signal faible ; les conflits détectés (ex. casting disjoint, année incompatible pour les séries) rétrogradent vers la revue. La marge d'arbitrage ne compte que les **concurrents plausibles** (même année ±1).

## Audit complet

La passe de croisement ne vérifie pas seulement les items en doute : elle audite **tous** les items « sûre » (durée, casting, réalisateur de la fiche AlloCiné vs métadonnées IMDb/TMDB du mapping retenu). Une décision d'arbitrage est prise uniquement avec ≥2 signaux forts concordants ; un mapping contredit (durée à 40 min, casting disjoint) est rétrogradé en revue. Les décisions sont persistées dans `cache/cross.json` (sauvegarde incrémentale, runs reprenables) — le premier audit coûte ~1 h, ensuite tout est rejoué en secondes.

## Résultats

- Profil AlloCiné complet scrapé (films et séries)
- Toutes les entrées importables sont validées (IDs uniques, dates ISO, notes 1–10)
- Confiance après audit croisé complet de tous les items
- Des mappings erronés sont corrigés automatiquement grâce au croisement durée + casting + réalisateur
- Les entrées strictement identiques sont dédoublonnées
- Noms asiatiques gérés (ordre coréen/japonais inversé entre sources) ; variantes de requête « titre + année » et types IMDb élargis pour les documentaires

## Limites connues

- Les séries sont importées au niveau « show » (AlloCiné ne note pas les épisodes)
- Les notes sans date : toutes les dates valent `--date` (pas de date de note sur AlloCiné)
- Les items absents d'IMDb restent hors import (`unresolved.json`), l'import Trakt exigeant un `imdb_id`
- Le DOM d'AlloCiné peut changer : `--review` et les caches facilitent l'ajustement des sélecteurs

## Développeurs

Voir `AGENTS.md` pour les règles internes (sémantique du cache, règles d'arbitrage, quirks de scraping).
