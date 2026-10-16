# PosterPlus PR1 — renderoptimalisaties met behouden legacy-identiteit

Datum: 16 oktober 2026. Core-basis `319cd93` (`origin/dev`), live referentie
`ac32e68`. De relevante compositorbestanden/assets/config verschillen niet tussen
deze twee basisrefs. Alleen de eigen Core-worktree is gewijzigd.

## Implementatie

- `main.py`: maximaal 256 titel-boundingboxes binnen één `build_poster()`-aanroep,
  keyed op tekst en het exacte fontobject. De drawcontext is vast voor die render.
  Wrapping en blokmeting delen dezelfde berekening; geen fontobject wordt tussen
  renders gedeeld. Fonts, fontkeuze, shaping en tekenposities blijven gelijk.
- `awards.py`: notchtekst gebruikt de al gemeten bbox opnieuw. De sashcaller blijft
  zelf meten. Frosted/black notchmaskers delen maximaal 32 immutable bytebuffers,
  keyed op breedte, hoogte en radius. Iedere render reconstrueert een eigen
  Pillow-image; posterpixels, tint, tekst en opacity zitten niet in deze cache.
  De bestaande Pillow/Cairo-tekenpaden en encoder blijven behouden.
- `v2_render.py`: de bestaande AST/assets/library-berekening blijft intact.
  `RENDERER_BUILD_FINGERPRINT` bewaart het ruwe resultaat. Alleen één expliciet
  bewezen key in `_RENDERER_COMPATIBILITY` behoudt de bestaande semantische ID.
  Iedere onbekende fingerprint krijgt zijn eigen ID, ook bij andere libraries.
  Geen env-override, cross-renderer fallback, receipt- of attestationwijziging.

De exacte binding voor deze PR1-build is:

```text
raw:      4aea61b945e646223209b3a79a40088c4eb49e7700a411a925c1f01c2ccfb869
semantic: 260504a9d0d0daa577848316c51518c5de78c4110642bac621cc92d3538f2b29
```

## Bewijs en checks

`2026-10-16-render-optimization-parity.json` bevat libraryversies, alle 20
fixture-identiteiten, vier uitvoeringen en individuele timings. De check hergebruikt
de bestaande frozen v2-endpointfixturehelpers. Beeldinputs zijn deterministisch
gegenereerde JPEG-patronen en een transparant PNG-logo; geen providers/OCR/netwerk
of productiegegevens. Fixtures dekken de drie huidige presets met/zonder logo,
Engels/Nederlands, lange titels, en custom frosted/black/silver/gold-notches met
verschillende geometrie, negatieve/positieve inset, ster en tekstkleur.

Op **x86_64 en native aarch64** zijn de WebP-bytes vóór/na exact vergeleken. Alle 20
contentdigests zijn daarnaast gelijk tussen beide architecturen. Canonieke JSON,
config-, requirements-, snapshot-hashes en de uitgegeven renderer-ID blijven gelijk.

- x86: `tests/test_v2_render_endpoint.py tests/test_v2_snapshot_hash_vectors.py`:
  **38 passed** in 17,93 s.
- x86: `tests/test_notch_open_top_border.py tests/test_fallback_logo_text.py`:
  **6 passed** in 2,05 s.
- Native ARM op de disposable AWS-verifier: dezelfde vier bestanden samen:
  **44 passed** in 14,86 s; geen skips.
- Negatieve identitychecks: gewijzigde cache-/teken-AST, RequestConfig,
  fontasset en Pillowversie vallen buiten de binding. Health-only wijzigingen
  behouden de bestaande ruwe identiteit. Maskchecks dekken alle drie keys,
  eviction, immutable opslag en onaangeraakte invoerbeelden.
- `git diff --check`: groen. App-broncode/schema/routes zijn niet gewijzigd;
  app-/PostgreSQL-/routechecks zijn daarom niet nodig voor deze PR1.

De containers gebruikten Pillow 11.3.0, numpy 2.4.6, FreeType 2.13.3, WebP 1.5.0,
Cairo 1.18.4 en pycairo 1.29.1. x86 Python 3.11.14; ARM Python 3.11.17. Hun
oude rendererfingerprint reproduceert exact de bestaande ID hierboven.
Dit is compositiecompatibiliteitsbewijs, geen nieuwe bronrecipe-/OCR-attestation.
Bestaande Pillow- en Starlette-deprecationwarnings zijn zichtbaar in de testlogs.

## Kleine benchmark

Twee CPU's, 1 GiB containerlimiet, netwerk uit; taal/fontinitialisatie vooraf.
20 fixtures, x86 drie herhalingen per toestand; ARM één per toestand als paritysmoke.
Koud wist vóór iedere render alleen de nieuwe process-maskcache; warm herhaalt
met gevulde cache. Titelmetingen blijven per render lokaal. Getal = mediaan van
de gemiddelden per fixture. Compositie sluit encoding uit; totaal bevat encoding.

| Architectuur / meting | Oud koud | Nieuw koud | Oud warm | Nieuw warm |
| --- | ---: | ---: | ---: | ---: |
| x86 compositie | 439,91 ms | 439,19 ms | 443,45 ms | 417,49 ms |
| x86 totaal | 669,94 ms | 662,80 ms | 677,19 ms | 664,12 ms |
| ARM compositie | 409,57 ms | 417,04 ms | 422,51 ms | 409,19 ms |
| ARM totaal | 634,16 ms | 640,81 ms | 648,26 ms | 634,11 ms |

Dit kleine sample ondersteunt een bescheiden warme verbetering; de koude
ARM-meting is iets trager. Hostbelasting en encoding begrenzen de interpretatie.
Geen upstreampercentage, capaciteitsschatting of productielatentieclaim.

## Reproduceren

Vanuit de finale PR1 Core-checkout, op iedere architectuur:

```bash
docker build -f scripts/render-optimization-check.Dockerfile -t posterplus-pr1-check .
mkdir -p /tmp/pp-pr1-baseline /tmp/pp-pr1-evidence
git archive 319cd93 | tar -x -C /tmp/pp-pr1-baseline
docker run --rm --network none --cpus 2 --memory 1g \
  -v "$PWD":/candidate:ro -v /tmp/pp-pr1-baseline:/baseline:ro \
  -v "$PWD/badges":/app/badges:ro -v /tmp/pp-pr1-evidence:/evidence \
  posterplus-pr1-check python /candidate/scripts/check_render_optimizations.py \
  --source /baseline --output /evidence/baseline --iterations 3
docker run --rm --network none --cpus 2 --memory 1g \
  -v "$PWD":/validation:ro -v "$PWD/badges":/app/badges:ro \
  -v /tmp/pp-pr1-evidence:/evidence \
  posterplus-pr1-check python scripts/check_render_optimizations.py \
  --output /evidence/candidate --baseline /evidence/baseline --iterations 3
```

Gebruik voor exacte reproductie de libraryversies uit het JSON-bewijs. Het
Dockerfile is uitsluitend een testomgeving. Andere renderer-libraryversies
leveren bewust een andere raw key op en vallen buiten de binding.

## Overdracht voor finale clusterintegratie

Root integreert in `/root/postersplus-bingecatwork/integration-features-1-3` en
`/root/.paseo/worktrees/19s30g4j/integration-posterplus-features-1-3`.
Deze PR1-key is **geen toestemming voor de uiteindelijke PR1+2+3-build**.
Na stacken moet root:

1. De code-root-/assetmanifestdekking controleren: PR2 `RequestConfig`,
   `build_poster`, notch/sash, ratingbarsegment, nieuwe `rating_badges.py` en alle
   negen packaged `static/rating_badges/*.png`; PR3 `v2_render`, `discovery`,
   `i18n` en de vijf ondersteunde taalbestanden. Cross-module helpers moeten
   expliciete roots hebben: een import-AST alleen hasht hun body niet.
2. Canonieke config/spec, requirements en contract/snapshotprojecties mee
   verifiëren. `render_spec.py` en `integration_contract.py` horen niet automatisch
   bij de bestaande compositorfingerprint; hun compatibilitybewijs vraagt eigen
   oude vectors en nieuwe opt-in checks. Maak die scheiding niet ruimer via een
   rendereroverride.
3. Op de exacte finale runtimebuild oude presets en representatieve oude
   customconfiguraties byte-/hashvergelijking tegen `319cd93` laten slagen op
   x86 én ARM. Nieuwe opties krijgen hun eigen config-/snapshot-hashes.
4. Pas daarna de exacte nieuwe fingerprint als bindingkey vastleggen en de
   negatieve identitychecks opnieuw draaien. Onbekende keys behouden hun eigen
   revision. De ruwe fingerprint blijft inspecteerbaar via
   `RENDERER_BUILD_FINGERPRINT`; uitgaande metadata gebruikt `RENDERER_REVISION`.
5. De geautoriseerde finale app-/Core-clusterchecks uitvoeren. Dit PR1-bewijs
   vervangt geen integratiebewijs of operationele runtime-attestation.

Het aanvullende prewarmonderzoek van 16 oktober bevestigt: **nul extra
prewarmcode**. Oracle staging verwerkt uitsluitend de drie fixed/en-presets;
nieuwe custom opt-ins volgen de bestaande demandflow. De renderchecks hierboven
gebruiken geïsoleerde testcontainers. Voor een latere rollout blijven nieuwe echte
ARM/x86-imagepariteit en verse runtime-evidence vereist; een oude attestation mag
niet als bewijs voor een nieuwe image worden hergebruikt.

Voor deze zuivere optimalisatie is **geen prewarm, brede invalidatie of rendergolf
nodig**: bestaande legacy-identiteiten en bytes blijven behouden. Geen merge,
deployments, productie-/Oracle-SSH, providers of queuewijzigingen uitgevoerd.
