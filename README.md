# allocine-to-trakt

Exporte les notes de films et séries d'un profil public AlloCiné vers un JSON importable sur Trakt — en local, sans clé API payante.

- Lit les onglets *films notés* / *séries notées* d'un profil public AlloCiné (titre, note /5, fiche)
- Convertit les notes (0,5–5 → entier 1–10), résout chaque item vers son **ID IMDb** (sans clé, via l'API publique de suggestion IMDb), et recoupe chaque mapping avec TMDB + la durée/le casting/le réalisateur de la fiche AlloCiné
- Génère `trakt-import.json` prêt à importer sur [trakt.tv](https://trakt.tv) (Réglages → Importer), plus des fichiers de contrôle (`report.csv`, `review.csv`, `unresolved.json`)
- Tout est caché : seul le premier audit est long (durée variable selon la taille du profil et la vitesse des services) ; les runs suivants rejouent depuis le cache

## Démarrage rapide

```bash
./install.sh
./export.sh
```

Le premier lancement ouvre un assistant qui demande l'URL du profil, la date fixe, la clé TMDB facultative et propose de lancer la revue interactive. Le profil Allociné doit être **public**.

Les lancements suivants réutilisent automatiquement les caches.

## Utilisation avancée

```bash
.venv/bin/python allocine_to_trakt.py --url "https://www.allocine.fr/membre-ZXXXXXXXXXXXXXXXXXXX/"
```

`overrides.json` est créé automatiquement s'il n'existe pas. Pour lancer explicitement l'assistant :

```bash
./export.sh --wizard
```

### Revue interactive

```bash
./export.sh --review
```

Présente uniquement les items en doute, du plus risqué au moins risqué, avec les candidats alternatifs et leurs signaux (durée, acteurs communs, réalisateur). Utilisez les flèches haut/bas puis `Entrée` :

| Option du menu | Effet |
|---|---|
| Valider le mapping actuel | conserver l'ID affiché |
| Garder la proposition IMDb | choisir l'alternative IMDb affichée |
| Prendre la proposition TMDB | choisir l'alternative TMDB affichée |
| Saisir un ID IMDb manuellement | saisir `ttXXXXXXX`, vérifié via TMDB |
| Rechercher une correspondance avec TMDB | lancer une recherche supplémentaire |
| Exclure cet item de l'import | retirer réellement l'item du JSON |
| Passer / Quitter | reprendre plus tard ou sauvegarder |

Les décisions sont persistées (`cache/review-ok.json`, remplacements dans `overrides.json`) : les items validés ne se représentent plus.

### Options principales

| Option | Défaut | Rôle |
|---|---|---|
| `--date AAAA-MM-JJ` | aujourd'hui (12:00 UTC) | date fixe `watched_at`/`rated_at` — AlloCiné ne stocke aucune date de note |
| `--wizard` | — | assistant de premier lancement |
| `--refresh-scrape` | — | re-scraper le profil (sinon `cache/items-*.json` est réutilisé : nécessaire si vous avez noté des films depuis) |
| `--delay` / `--delay-imdb` / `--delay-tmdb` | 1,2 / 0,3 / 0,25 s | délais anti-429 (relances automatiques avec backoff) |
| `--kinds films,series` | les deux | listes à exporter |
| `--overrides` | — | fichier de mappings manuels, prioritaires sur tout |
| `--tmdb-key` | `.env` | clé API TMDB (chaîne, chemin de fichier, ou `TMDB_API_KEY`) |
| `--output-dir` | `.` | dossier des sorties et du cache |
| `--no-tmdb-check` | — | désactiver la contre-vérification TMDB (fallback seul) |
| `--retry-unresolved` | — | purger les échecs des caches (`imdb.json`, `tmdb.json`, `cross.json`, y compris divergences TMDB et conflits d'arbitrage) pour les retenter |
| `--max-pages` / `--limit` | 0 | outils de debug : limiter les pages/l'items traités (un `--limit` produit un export partiel) |

La clé TMDB est facultative mais améliore le matching. Copiez `.env.example` vers `.env` et renseignez-la, ou laissez l'assistant vous la demander :

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

Le cache vit dans `cache/` (profil, fiches, résolutions IMDb/TMDB, décisions d'arbitrage). Les décisions sont sauvegardées progressivement : une interruption permet de reprendre. Supprimer un fichier de cache change les décisions, voir `AGENTS.md`. Après des échecs réseau, `--retry-unresolved` purge les clés en échec d'`imdb.json`, `tmdb.json` et `cross.json`.

## Modèle de confiance

| Niveau | Signification |
|---|---|
| **sûre** | titre exact + année exacte sur IMDb, **ou** confirmation croisée indépendante (durée ±2 min, ≥2 acteurs communs ou même réalisateur), ou validation manuelle |
| **validée** | mapping conservé après validation humaine en revue (`--review`, décision dans `cache/review-ok.json`) |
| **exclue** | item retiré volontairement de l'import lors de la revue |
| **à vérifier** | correspondance plus faible (année seule, année ±1, Wikidata seul…) → à trancher via `--review` |
| **hors import** | divergences non départagées ou titres absents d'IMDb — exclus du JSON d'import |

Politique volontairement conservatrice : jamais d'application automatique sur un seul signal faible ; les conflits détectés (ex. casting disjoint, année incompatible pour les séries) rétrogradent vers la revue. La marge d'arbitrage ne compte que les **concurrents plausibles** (même année ±1).

## Audit complet

La passe de croisement ne vérifie pas seulement les items en doute : elle audite **tous** les items « sûre » (durée, casting, réalisateur de la fiche AlloCiné vs métadonnées IMDb/TMDB du mapping retenu). Une décision d'arbitrage est prise uniquement avec ≥2 signaux forts concordants ; un mapping contredit (casting disjoint, incohérences de métadonnées) est rétrogradé en revue. Les entrées strictement identiques sont dédoublonnées et les noms asiatiques sont gérés malgré l'ordre coréen/japonais inversé entre sources. Les décisions sont persistées dans `cache/cross.json` (sauvegarde incrémentale, runs reprenables) — le premier audit est le plus long, ensuite tout est rejoué depuis le cache.

## Limites connues

- Les séries sont importées au niveau « show » (AlloCiné ne note pas les épisodes)
- Les notes sans date : toutes les dates valent `--date` (pas de date de note sur AlloCiné)
- Les items absents d'IMDb restent hors import (`unresolved.json`), l'import Trakt exigeant un `imdb_id`
- Le DOM d'AlloCiné peut changer : en cas de casse, adaptez les sélecteurs puis relancez avec `--refresh-scrape` + purge de `cache/detail/` (les caches stockent du parsé, pas le HTML)
- Le premier audit complet est long (durée variable selon la taille du profil et la vitesse des services) ; les runs suivants sont rapides grâce au cache

## Avertissement légal

- Cet outil est destiné à un **usage strictement personnel** : n'exportez que **votre propre profil** AlloCiné, jamais celui d'un tiers (les notes et le pseudo d'un profil sont des données personnelles).
- Les [CGU d'AlloCiné](https://www.allocine.fr/service/conditions.html) (version du 16/05/2025) interdisent l'extraction et l'usage des données du site hors de sa consultation : « extraire, de manière substantielle ou non, et/ou utiliser en dehors de la consultation du Site, une quelconque donnée du Site » (art. 7.5), et « collecter ou stocker des données en vue de créer une base de données » (art. 8.1). **L'export automatisé décrit ici est donc en contradiction avec ces CGU** ; il en va de même des requêtes automatisées en volume (art. 8.1).
- Conséquence possible : suspension ou clôture du compte AlloCiné, sans préavis (art. 9). L'utilisation de ce script se fait **sous votre seule responsabilité**.
- La voie recommandée est de demander une autorisation écrite à AlloCiné (art. 7.5) : téléphone `+33 811 69 41 42` ou [formulaire de contact](https://www.allocine.fr/service/contact/).
- Ce projet n'est ni affilié, ni approuvé, ni sponsorisé par AlloCiné/Webedia, Trakt, TMDB ou IMDb. « AlloCiné », « Trakt », « TMDB » et « IMDb » sont les marques de leurs propriétaires respectifs.

## Licence

MIT — voir [`LICENSE`](LICENSE).

## Développeurs

Voir `AGENTS.md` pour les règles internes (sémantique du cache, règles d'arbitrage, quirks de scraping).
