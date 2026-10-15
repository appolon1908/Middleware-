# Dependency map

Arrows point from a PR to the PR (or candidate) whose merge it waits on.

## Carried by the candidate (#397)

#316 #317 #318 #321 #323 #325 #327 #330 #336 #347 #349 #350 #351 #352 #353 #357 #358
#359 #360 #361 #369 #377 #382 #383 #384 #386 #388 #389 #393 -> **#397**

Transitive supersessions, all resolved on #397:

- #327 -> #389 -> #397
- #347 -> #357 -> #397
- #349 -> #358 -> #397
- #350 -> #353 -> #397

## Waiting on another open PR

- #304 -> #397 (DB-TLS slice) and #312/#334 (PAS-146/179 evidence, Postman certification test)
- #337 -> #334 (+#312)
- #363 -> #334
- #343 -> #356 (+#370)
- #366 -> #397 (dispatcher recovery) and #356/#370 (production registration, D3)
- #344 -> #342
- #346 -> #341
- #348 -> #354

## MCR stack (absent from the candidate, held by the certify gate)

#319 -> #338 -> #339 -> #340 -> #341 -> #342 -> #345 -> #355, plus #354 (legacy-port refusal).
All of it is preserved on `preserve/pr-sweep-20260930-replay-wt` (`local/replay-wt`,
`99f34e9e`), except the #338/#345 Leads-journey backend, which is missing even there.

## Independent, open

- Unique work: #322 #328 #331 #333 #335 #368 #371 #379 #380 #381 #387
- Deferred by decision: #312 (D12, also carries PAS-44), #329 (D17), #334 (D12), #356 (D3), #370 (D15/D16), #376 (D15)
- Owner decision needed: #308 #311 #326

## Closed by recorded decision (branches kept)

#309, #390 (D6), #362 (D9)
