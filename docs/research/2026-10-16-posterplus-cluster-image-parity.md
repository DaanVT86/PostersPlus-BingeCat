# PosterPlus PR1+2+3 — finale gesloten rendererbinding en imagepariteit

16 oktober 2026. Uitsluitend Core `integration/features-1-3` is gewijzigd. De
machineleesbare bewijsset staat in
[`2026-10-16-posterplus-cluster-image-parity.json`](2026-10-16-posterplus-cluster-image-parity.json).
Dit is nieuw validatiebewijs voor de hieronder genoemde eigen images, geen
deploybewijs of bronrecipe-/OCR-attestation. Er zijn geen bestaande operationele
attestations aangepast of overgenomen.

## Bron en geschiedenis

De geïntegreerde PR2/PR3 zijn vóór de builds op de originele PR1-head gerebased.
De Git-tree bleef exact `113b70f24e172f47b8626f439498ea198ef15d84`.

| Onderdeel | Commit |
| --- | --- |
| Legacy-baseline | `319cd93ac6610f86b733ce541a1ae965b26d8135` |
| Originele PR1-head | `2b5cb9ba362bc426490b4a9a34d7c2a4df930047` |
| Nieuwe PR2-top | `14eb1a1f79e1396cb37fd02fb729fbb7243f1abf` |
| Nieuwe PR3-top | `e84442b276824e0c57f834b95692a609c1cecc9b` |
| Ongebonden proof-build | `2ebcd13859cf3667658356ec10b08bf1f8498dcf` |
| Toevoegen bewezen binding | `058b09d6cbc38ffd80c4f5aa3aa3c3aa6da20ed3` |
| Definitieve geteste code/tests | `e37937d0c3c71738b8dd280899e77fa1182c98ab` |

De definitieve images bevatten de tree
`83ca905d57739870eb3a13f85f6eec55a9fcbc4e`. Het aansluitende bewijsdocumentcommit
wijzigt alleen deze Markdown/JSON-bestanden. De imagelabels blijven eerlijk op
`e37937d`; ze zijn niet naar het documentcommit verzet. Alle compositor-, policy-,
asset- en requirements-bestanden in de zes images zijn per SHA-256 vergeleken met
hun opgegeven Git-commit. Het JSON bevat die hashes.

## Dekking en binding

De bestaande genormaliseerde AST-closures blijven behouden; imports worden niet
automatisch als volledige modules gevolgd. Expliciete crossmodule-roots dekken:

- de hele `rating_badges.py`-AST, inclusief constants/helpers/imports, en alle negen
  daadwerkelijk gebruikte `static/rating_badges/*.png`;
- `render_spec.PORTRAIT_DEFAULTS`, `BADGE_PROVIDERS`, `portrait_options`,
  `badge_layout` en `weighted_rating_needed`, met hun lokale dependencyclosures;
- `main.RequestConfig`/`build_poster`, `v2_render._compose`, de notch/sash- en
  ratingsegmenthelpers, plus `age_badge.draw_tier_bar` en `_score_points`;
- de werkelijk gebruikte configdefaults, genre/canvas/quality/trendingwaarden,
  logo-opties en ratinggewichten, zowel als AST-input als effectieve runtimewaarde;
- de bestaande fonts/genreachtergronden/logos en vijf taalbestanden
  (`en`, `nl`, `de`, `pt`, `es`), inclusief `dateWindowLabels`.

Canonicalisatie, requirements, gebruikte ratings/facts, datumselectie en
contract/spec blijven in de aparte policyfingerprint. Die omvat onder meer
`_used_fact_fields`, `_used_ratings`, `_displayed_upcoming_date` via zijn closure,
`CanonicalRenderSpec`, `canonicalize_config`, `compile_requirements`, preset- en
contractdefinities, plus `i18n.normalize_locale`. De compositor bevat
`i18n.translate_sash` met zijn lokale datehelpers. De 70 assets staan expliciet in
het bewijsmateriaal.

Een gesloten approvalkey combineert de ruwe compositorfingerprint met de aparte
policyfingerprint. Daardoor kan een onbekende policywijziging de oude
bytegoedkeuring evenmin hergebruiken. De enige toegestane mapping is:

```text
compositor: 8c31f4a4689a4a1910db6b7b901da07c9684feef22ec1354702acdc9ce3f7091
policy:     e0857c57098953ff71207c7173957d03769fba4067cda156dcc08238e35fc5be
build:      a7ed1df863f30aab040b1ab6d005cf5918268fea5de6cf7ccb7b0652d099a9e5
renderer:   260504a9d0d0daa577848316c51518c5de78c4110642bac621cc92d3538f2b29
```

De oude PR1-key `4aea61b9…` is vervangen, niet als extra uitzondering behouden.
Iedere onbekende key krijgt zijn eigen rendererrevision. Geen env-override,
revisionfallback over verschillende renderers of stale-bypass toegevoegd.

De map was **leeg tijdens de proof-build**. Eerst slaagden de 108+20 cases op
beide architecturen en de vergelijking tussen architecturen. Pas daarna is de
exacte mapping gecommit. Het toevoegen van de mapping verandert geen gehashte
renderer/policy-input. De definitieve gebonden images zijn opnieuw strikt tegen
de baseline gecontroleerd, inclusief de uitgegeven renderer-ID.

## Echte images en runtime

Images zijn met de bestaande validation-overlay-Dockerfile gebouwd uit de
hergebruikte, geïnspecteerde PR1-drawingruntimes. Er is geen packagebuild of
encoder/font/pxscale-wijziging gedaan. De testcontainers hadden geen netwerk en
gebruikten de ingebakken `/app`-bron; alleen gegenereerde bewijsbestanden zijn
naar buiten geschreven. De brede test leest één apart gemounte statische
`compose.production.yaml`, omdat `.dockerignore` die uit images weglaat; er was
geen compositorbronmount.

| Run | Image-ID |
| --- | --- |
| x86 baseline | `sha256:c20087a0edb1bc72687ecaadb66b011ae8aa73356f161560b62d9f7c099a20fa` |
| x86 ongebonden proof | `sha256:25e20c4bf618d55e3992a103bd1017625dbb247d55f0b5360e0509f1efa63b40` |
| x86 definitief | `sha256:73682819f3d4e82e62ddf10f47dc92356efcfa3f14ed4685c43ec2b8c879e61f` |
| native ARM baseline | `sha256:3231d23d2db3b996d2c57491c27ef60d5dc673df70a275393eefb22c3ab749b3` |
| native ARM ongebonden proof | `sha256:ea762c322111808eb10d28d25ad6fa8caabccd4eebfd35b804cf71f861af47d9` |
| native ARM definitief | `sha256:348b4657a2b0b1c939a9a5ff8ce6bd4939382244714f71f2534492db1237f64d` |

De x86-runtimebasis is `sha256:712b97b32faea62d2c195a289951dc094dcbdf70d3e4051d8205ab8f683be82b`;
de ARM-basis is `sha256:520ce4993d5585eb01d66d36a251565f1f2397008a06bfcbb7ba26b553aecdab`.
AWS `aicat-verify` rapporteert native `aarch64`; Docker inspect rapporteert
`arm64`, zonder emulatie. Lokaal rapporteert het proces `x86_64` en Docker `amd64`.

Beide gebruiken Pillow **11.3.0**, numpy **2.4.6**, pycairo **1.29.1**, Cairo
**1.18.4**, FreeType **2.13.3**, WebP **1.5.0**. Python is x86 **3.11.14** en ARM
**3.11.17**. Alle aanvullende drawinglibraryversies en packageversies staan in
het JSON. De baselineimages reproduceren zelf exact renderer `260504a9…`.

## Checks en resultaat

De bestaande `tests/portrait_legacy_parity.py` levert **108** cases: alle elf
gepubliceerde presetversies, custom ratingmodes/appendkeuzes, patterned/solid
achtergrond, Engels/Nederlands. Confighash, requirementsprojectie, pixelhash,
WebP-hash en bytegrootte zijn in alle zes runs gelijk.

De bestaande `scripts/check_render_optimizations.py` voegt **20** frozen
endpointcases toe: huidige presets, met/zonder logo, lange titels, en custom
notchstyles/geometrieën. Alle zes runs hebben gelijke canonical JSON,
config-/requirements-/snapshothashes, contentdigests en daadwerkelijke WebP-files.
De definitieve runs behouden ook de baseline-renderer-ID. Beide architecturen
zijn rechtstreeks onderling vergeleken. De eerder bewezen PR1-benchmark is niet
opnieuw als afzonderlijk onderzoek gedraaid.

- Native ARM, hele Core-suite in de definitieve image: **451 passed, 6 subtests
  passed**, 37,64 s, geen skips. De eerste brede run had 450 pass en één oudere
  identitytest die Git in de container vereiste en raw/semantic revision
  verwarde. Die test controleert nu een echte freshness-only bronmutatie tegen
  de raw fingerprint én approvalkey; na die testwijziging is de suite groen.
- x86, gerichte identity/freshness/combined-renderchecks in de definitieve image:
  **28 passed**, 12,12 s.
- Negatieve checks: badgehelpers/constants, portraitdefaults/providers/layout,
  config-/logo-defaults, qualityhelper, datumbeleid, alle negen PNGs, vijf
  taalassets, fontbytes en onbekende Pillowversie verlaten de binding.
- Een canonical-version- of ratingsselectiemutatie verandert de policy en
  approvalkey terwijl de compositorfingerprint gelijk blijft; apart uitgevoerd
  en in het JSON opgenomen. Health-/freshness-only wijzigingen behouden hun
  bestaande uitsluiting.
- De echte gecombineerde rendertest vergelijkt legacy, datum, datum+IMDb-badge,
  datum+IMDb-badge+**frosted left side-notch**. Alle vier WebPs verschillen;
  terugzetten van opt-ins herstelt de canonieke defaultconfig.
- Die test vond vóór de freeze een echte PR2-`NameError`: de side-notch riep
  `_frosted_tint_from_rgb` aan, die niet bestaat. De ene call gebruikt nu de al
  bestaande `_frosted_tint`. Daarna is de echte render groen. De defaultcenter-
  notch passeert de legacy-bytepariteit.
- `git diff --check`: groen. Bestaande Pillow/Starlette-deprecationwarnings zijn
  zichtbaar; geen runtimefout meer. Geen app-suite gedupliceerd.

De pariteitsrun registreert ook de bestaande kleine cold/warm-metingen, één
herhaling per toestand, mediaan over twintig fixtures. x86: 2 CPU/1 GiB; ARM:
4 CPU/2 GiB. Koud wist alleen de nieuwe maskcache; taal/fontsetup is opgewarmd.
Encoding zit uitsluitend in de totaalmeting. Dit is een ruismeting, geen nieuwe
prestatieclaim:

| Run | Compositie koud / warm | Totaal koud / warm |
| --- | --- | --- |
| x86 baseline | 409,28 / 403,59 ms | 652,10 / 637,51 ms |
| x86 definitief | 445,82 / 426,09 ms | 707,90 / 667,02 ms |
| ARM baseline | 416,91 / 422,56 ms | 642,18 / 648,37 ms |
| ARM definitief | 411,50 / 411,70 ms | 637,68 / 638,97 ms |

De x86-meting is hier trager; één sample op een gedeelde host is onvoldoende
voor een regressie- of winstclaim. De oorspronkelijke beperkte PR1-benchmark
blijft afzonderlijk beschikbaar. Er wordt geen upstreampercentage geclaimd.

## Reproduceerbare commando's

Gebruik op iedere architectuur dezelfde geïnspecteerde runtimebasis en een
schone `git archive` van de genoemde broncommit. Met `BASE`, `PROOF` en `FINAL`
als de zo gebouwde imagetags, en een schrijfbare evidence-directory:

```bash
# Build vanuit de uitgepakte bron; de runtimebasis vooraf met docker inspect verifiëren.
docker build --network none -f deploy/oracle/dockerfile.runtime-overlay \
  --build-arg VERIFIED_BASE_IMAGE=posterplus-pr1-check:latest \
  --build-arg SOURCE_REVISION=e37937d0c3c71738b8dd280899e77fa1182c98ab \
  -t "$FINAL" .

# Run het bestaande harness via stdin: alle imports komen uit de image.
docker run --rm -i --network none --cpus 2 --memory 1g -w /app \
  -e PR2_PARITY_OUTPUT=/evidence/portrait.json -v "$OUT":/evidence \
  "$FINAL" python - < tests/portrait_legacy_parity.py

docker run --rm -i --network none --cpus 2 --memory 1g -w /app \
  -v "$EVIDENCE":/evidence "$FINAL" python - \
  --source /app --output /evidence/final/fixtures \
  --baseline /evidence/baseline/fixtures --iterations 1 \
  < scripts/check_render_optimizations.py

# In de ongebonden proof-fase: dezelfde endpointcheck met --unbound-proof.
# Alleen renderer-metadata mag dan verschillen; bytes en alle andere hashes niet.
cmp "$EVIDENCE/baseline/portrait.json" "$EVIDENCE/final/portrait.json"

# Native AWS ARM, één hele Core-suite op definitieve code.
docker run --rm --network none --cpus 4 --memory 3g -w /app \
  -v "$SOURCE/compose.production.yaml":/app/compose.production.yaml:ro \
  "$FINAL" python -m pytest -q -p no:cacheprovider tests

# Kleine lokale x86-set.
docker run --rm --network none --cpus 2 --memory 1g -w /app "$FINAL" \
  python -m pytest -q -p no:cacheprovider \
  tests/test_renderer_cluster_identity.py \
  tests/test_source_registration.py::test_renderer_revision_excludes_freshness_only_changes \
  tests/test_v2_local_dates.py::test_combined_date_badges_and_side_notch_change_only_opted_pixels
```

De baseline krijgt de bron uit `319cd93`; de actuele harnessbestanden worden via
stdin uitgevoerd omdat ze daar nog niet aanwezig zijn. De proof-build gebruikt
`2ebcd13` en een lege binding. Eerst bytes/hashvelden vergelijken (de optionele
`--unbound-proof`-check doet dat rechtstreeks), daarna exact de hierboven
bewezen key binden en de final-build strikt controleren. Het JSON registreert de
werkelijke inspect-ID's, labels, runtimeversies, fixturevectors en logdigests.

## Integratie en grenzen

Root draagt de finale commits over naar de laatste stacked Core-PR #8. Er zijn
hier geen PRbranches aangepast, nieuwe PRs geopend of images gedeployd. Alleen
Core-bron/test/evidence is gewijzigd. Geen Oracle-/productie-/provider-/queueactie.

De **gecombineerde** PR1+2+3-fingerprint is nu vernieuwd en op beide echte
architecturen gevalideerd; dit vervangt de voorlopige PR1-binding. Root moet na
restacken controleren dat de expliciete inputmanifest/hash en effectieve
runtime nog exact matchen. Iedere latere compositor/policy/asset/config/library-
wijziging vereist opnieuw een gesloten bewijs/binding; de huidige mapping mag
niet worden uitgebreid om een onbekende build alsnog goed te keuren.

Deze images gebruiken de pinned drawingruntime voor offline compositorchecks.
Bronverwerving, OCR/faces en een toekomstige volledige deploymentimage zijn niet
hiermee geattesteerd. Bij een latere rollout horen nieuwe image-eigen checks en
operationele attestations voor de werkelijk te gebruiken images; bestaande
attestations niet hergebruiken. Als hun drawinginputs verschillen, valt de build
terecht buiten deze mapping.

Omdat legacy-rendereridentiteit, canonical config, requirements, snapshot en
WebP-bytes behouden zijn, is **geen catalogusprewarm of initial/renderwave nodig**
voor deze cluster. Er is nul extra prewarmcode toegevoegd. Nieuwe custom opt-ins
gebruiken de bestaande demandflow; de drie fixed/en-presets behouden hun output.
