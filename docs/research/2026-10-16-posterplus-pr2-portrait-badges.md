# PosterPlus PR2: opt-in portraitopties en providerbadges

Datum: 16 oktober 2026. App-base `b0c071cf36c44d9987e729d4534f47a5c90bf552`; Core-base `319cd93ac6610f86b733ce541a1ae965b26d8135`. Eigen branches: app `feat/posterplus-portrait-provider-badges`, Core `feat/portrait-provider-badges`. Dit document hoort bij PR2; het goedgekeurde historische onderzoeksrapport blijft intact.

## Geleverd gedrag

De bestaande BingeCat-configurator en de Core-browserhandoff kunnen de nieuwe opties instellen, previewen en opslaan. Core tekent ze binnen de bestaande portraitlayouts. Nieuwe defaultwaarden en inactieve subopties verdwijnen uit canonieke JSON; reset naar defaults herstelt de oude config hash. Presetrefs en requirements-schema blijven gelijk. De bestaande opaque `canonical_config`-velden in DTO/integration_contract dragen de uitbreiding; nieuwe DTO- of databasevelden zijn niet nodig.

| Optie | Waarden | Default / activering |
| --- | --- | --- |
| `sash_badge_pos` | `center`, `left`, `right` | `center`; alleen zichtbare notch |
| `sash_badge_pad` | 0.5–1.5 | 1; alleen zichtbare notch; fontgrootte behouden |
| `sash_side` | `right`, `left` | `right`; alleen zichtbare diagonale sash |
| `meta_order` | CSV-permutatie van `genre,year,rating` | oude volgorde; alleen behouden als de volgorde van potentieel zichtbare slots verandert |
| `minimalist_separator` | `pip`, `bullet` | `pip`; alleen bij een zichtbare grens tussen genre en year |
| `minimalist_rating_separator` | `star`, `pip`, `bullet`, `bullet_star` | `star`; alleen bij een grens die rating bevat |
| `rating_badges` | geordende CSV van maximaal drie unieke providers | leeg; alleen Clean, gewone Minimalist-ratinglayouts en Bar met ratingtekst |
| `rating_badge_scale` | `native`, `normalized` | `native`; alleen met actieve badges |
| `rating_badge_style` | `color`, `mono` | `color`; alleen met actieve badges |

Side-notch gebruikt een gesloten afgeronde chip met gesloten toptrim: 4.5% zij-/bovenmarge, 82% nominale notchhoogte, 80% horizontale padding en 30% hoekradius. De centrale open-top-notch blijft de bestaande geometrie gebruiken. Links gedraaide sashtekst blijft leesbaar en wordt niet gespiegeld.

Badgeproviders: `imdb`, `tmdb`, `letterboxd`, `trakt`, `tomatoes`, `popcorn`, `metacritic`, `metacriticuser`, `rogerebert`. Animeproviders zijn niet toegelaten. Alleen bestaande lokale `score`-metrics met geldige freshness en bestaande minimumstemmenregel tellen mee. Nulscore is geldig, ook met providergewicht nul. Er worden geen providerjobs aangevraagd om een badge te vullen.

Native scoretekst wordt uitsluitend uit `normalized_score` 0–100 berekend: IMDb en Metacritic user /10, Letterboxd /5, Roger Ebert /4, Metacritic /100, TMDB/Trakt/Tomatoes/Popcorn als percentage. Normalized gebruikt de bestaande /100- of /10-keuze van de layout. De volledige rij schaalt naar de beschikbare breedte; fontmetingen verwijderen geen laatste badge.

De negen PNG-logoassets zijn vooraf verpakt, RGBA 128×128, met bron- en bestandsdigests in `static/rating_badges/manifest.json`. Geen dynamische logo-download of nieuwe runtime dependency. Simple Icons zijn CC0; de vaste upright Popcornmeter-markering is de gepinde Commons-versie; de generieke Ebert-thumb volgt de bestaande upstreamconventie (Apache-2.0). Licenties zijn meegeleverd. De vaste markeringen wisselen niet met de score.

`minimalist_append_mode=3` blijft Dual rating (Metacritic), evenals de bestaande Dual-layout en Bar `second_rating`. Badges en hun subopties zijn daar canoniek inactief. Geen nieuwe layoutmodus, vignette, frost-reference of OCR-aanpassing.

## Gebruikte ratings en fallback

Core `v2_render._used_ratings` en app `workers._used_ratings` projecteren dezelfde union van getoonde badges en werkelijk nog benodigde gewogen score. Bij een bruikbare badge vervalt een ongebruikte gewogen score uit snapshotidentiteit. Bar `rating_black`/`rating_frosted` houdt de gewogen fill als dependency. Ontbrekende, expired, verkeerde metric of te weinig stemmen bij alle geselecteerde badges herstelt de normale gewogen fallback en diens dependencies. Daardoor verandert de hash weer wanneer die fallbackscore verandert. Ongebruikte/expired badgegegevens veroorzaken geen rendertrigger.

De compositor filtert badge-inputs opnieuw op metric, stemmen en freshness, zodat gewogen fallbackdata geen afgewezen badge terug kan introduceren. Het bestaande freshnessbeleid voor werkelijk gebruikte gewogen ratings blijft intact. App refreshplanning vraagt alleen de oude gewogen dependencies op wanneer fallback/fill die nodig heeft, nooit extra badgeproviders.

Canonieke helpernamen in beide repos: `PORTRAIT_DEFAULTS`, `BADGE_PROVIDERS`, `portrait_options`, `badge_layout`, `weighted_rating_needed`. Diezelfde contractlogica staat in Core `render_spec.py` en app `utils/posterplus_v2/core_facts_contract.py`. Signed inputs moeten reeds canoniek zijn; browserinputs worden geprojecteerd en defaults/inactieve waarden worden verwijderd.

## Regressiebewijs

De twee repos delen een byte-identieke fixture `tests/fixtures/posterplus_portrait_badges.json`: 31 vectors, SHA-256 `0e3248f35679ec7f45b6c8832319a8876cc889639bacad12df78d43e3b45db4d`. Elke vector bevat raw/canonical config, config hash, requirements, snapshot, gebruikte visuele projectie en snapshot hash. Dekking omvat defaults/reset/inactieve controls, hidden genre en ongebruikte separators, alle ondersteunde layouts, Dual/second_rating, ontbrekend/expired/low-votes/wrong-metric fallback, nulscore/zero-weight, gewogen fill-union en unused-data nuldelta.

Definitieve gerichte checks:

- **Core AWS ARM: 159 passed**. `tests/test_portrait_provider_badges.py`, `tests/test_v2_render_spec.py`, `tests/test_v2_snapshot_hash_vectors.py`, `tests/test_v2_handoff.py`, `tests/test_custom_score_palette.py`, `tests/test_v2_render_endpoint.py`. Bestaande PR1-checkimage met Python 3.11/Pillow 11.3.0/NumPy 2.4.6/pycairo 1.29.1; disposable source-directory, Docker `--network none`. Dit omvat ook bestaande preset-pixelvergelijkingen en endpoint/freshnesschecks.
- **App AWS: 250 passed, 1 skipped**. `python3 scripts/aicat_aws_verify.py pytest -q -n 6 tests/test_posterplus_portrait_badges.py tests/test_postersplus_v2_snapshot_hash_vectors.py tests/test_postersplus_v2_workers.py tests/test_posterplus_v2_settings_api.py tests/test_posterplus_v2_handoff.py`. Definitieve snapshot-tree `26f7d96d9035fa62ca339278e22cd5650c6316f2` (voor documentatie/generated maps). De skip is de PostgreSQL-case, hieronder gedekt.
- **PostgreSQL AWS: 132 passed**. Eén batch `python3 scripts/aicat_aws_verify.py --postgres pytest -q tests/test_postersplus_v2_workers.py`; disposable PostgreSQL/Redis. Geen nieuwe DB-logica of transacties. De latere aanscherping raakt alleen optionele separatorprojectie; deze reeds groene batch is niet herhaald.
- **Configurator AWS Vitest: 2 passed**. `python3 scripts/aicat_aws_verify.py vitest frontend/src/features/settings/posterplus-configurator-dialog.test.tsx`. Echte shadcn-controls met previewnetwerk gemockt; geordende providerkeuze, nulgewicht, schaal/stijl, metadataorder, Dual-betekenis, separators en chipcontrols bereiken de save-payload.
- Lokaal: finale nieuwe appfile **48 passed**, finale nieuwe Core-file **62 passed**; lint van beide gewijzigde TSX-files groen. Route/template-integriteit `--strict` groen; context-/route-/dependency-maps geregenereerd; beide `git diff --check` groen.

**Legacy byteparity: 108/108 exact gelijk.** Dezelfde offline harness `tests/portrait_legacy_parity.py` is onafhankelijk gedraaid op een clean `git archive` van Core-base en op de finale PR2-bronnen, in exact dezelfde gepinde x86-image `sha256:712b97b32faea62d2c195a289951dc094dcbdf70d3e4051d8205ab8f683be82b`. De 11 bestaande presetversies plus 16 customgevallen dekken alle oude layout-/appendmodi, diagonale sash, en/nl, gradient- en solid-artwork. Config hash, requirements, RGBA-pixels, lossless WebP-digest en bytegrootte zijn per record gelijk. Bewijs met alle records en finale bron-/assetdigests: Core `docs/research/2026-10-16-pr2-legacy-parity.json`. De bestaande gedeelde snapshotvectors zijn daarnaast in beide AWS-batches groen.

Reproduceren (beide bronbomen in dezelfde gepinde runtime; voer de kandidaat-harness ook tegen baseline-imports uit):

```bash
PYTHONPATH=/baseline PR2_PARITY_OUTPUT=/evidence/baseline.json \
  python /candidate/tests/portrait_legacy_parity.py
PYTHONPATH=/candidate PR2_PARITY_OUTPUT=/evidence/candidate.json \
  python /candidate/tests/portrait_legacy_parity.py
cmp /evidence/baseline.json /evidence/candidate.json
```

Run daarbij vanuit de respectieve baseline-/candidate-werkdirectory zodat taalbestanden dezelfde bronboom gebruiken. De harness heeft geen netwerk nodig.

## Finale integratie en fingerprintinputs voor root

PR2 wijzigt geen rendereridentity-, revision- of bindingcode. De exacte gecombineerde PR1/2/3-build moet root nog volledig op AWS verifiëren en pas daarna compatibel binden. PR1 Core #7 en PR3 Core #8/app #1443 zijn clusterdependencies; de datumvelden `release_status_dates`/`airing_dates` blijven PR3-eigendom.

Neem in de finale compositorfingerprint de bestaande gewijzigde `main.py`, `awards.py`, `ratings.py`, `v2_render._compose` én de **hele nieuwe `rating_badges.py`** op. Neem de **negen `static/rating_badges/*.png`** op in de assetfingerprint: de bestaande scan van `static/logos` dekt deze nieuwe directory niet. `render_spec.badge_layout` wordt ook door de compositor gebruikt. De canonical/selection-policyfingerprint moet de nieuwe constants/helpers in `render_spec.py` en de aangepaste `_used_ratings` meenemen. Bron- en PNG-digests staan volledig in het paritybewijs; zij zijn inputinventaris, geen bindingattestation. Geen blanket override, attestations of globale renderrevision gebruiken om features te activeren.

App/Core moeten samen het optionele contract ondersteunen voordat nieuwe customconfiguraties gebruikt worden. Bestaande presetrefs, config-/snapshotvectors en bytes blijven gelijk. Root doet de finale volledige clustersuite en exacte x86/ARM-buildparity; PR2 herhaalt die suite niet.

## Prewarmgrenzen en resterende checks

Geen nieuwe prewarmcode, custom Oracle-queue of catalogusbackfill. De bestaande Oracle-lane bezit uitsluitend drie fixed/en-presets. Nieuwe custom portrait-/badge-opt-ins gebruiken de bestaande preview-, demand- en coldfill-route. Legacy hashes plus bytes behouden betekent nul nieuwe catalogusprewarm voor deze featuredeploy. Popular-first is geen algemene bestaande steady-feature: initial gebruikt popularity, steady gebruikt ID-volgorde, demand gebruikt recency. Bron: de read-only verkenning `/tmp/posterplus-feature-prewarm-explore-20261016.md`.

De brede frontendtypecheck heeft drie bestaande fouten buiten de gewijzigde configurator: `settings-app.tsx:306` (TS2322 en TS7006) en `simkl-legacy.test.ts:49` (TS2345). De gerichte lint- en Vitestchecks zijn groen. Geen browser-E2E tegen productie uitgevoerd. Geen deploy, productie-/Oracle-actie, providerfetch, queuewijziging, brede invalidatie of backfill uitgevoerd; alleen dedicated AWS-verificatieruns en offline bron-/assetvoorbereiding.
