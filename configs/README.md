# Episode configurations

`train.json` and `dev.json` each hold 10 generator configs (CONTRACT section 8). Train seeds
are in 1000-1999, dev seeds in 2000-2999; no seed is shared. Per split: four single-type
episodes (duplicate_invoice, payment_allocation, fx_conversion, wrong_period), two clean
episodes (one small, one large), one `fx_partial_payment_boundary` combo, one
`duplicate_plus_period` combo and two mixed episodes (several types plus combos).
`examples/` holds small templates and is not part of the open set.

## Open episodes

| split | episode_id | seed | errors dup/alloc/fx/period | combos fx-partial/dup+period | invoices | documents | ledger rows | months | timezone | base |
|---|---|---|---|---|---|---|---|---|---|---|
| train | `EP-PN410491WY` | 1101 | 1/0/0/0 | 0/0 | 30 | 55 | 251 | 1 | Asia/Almaty | KZT |
| train | `EP-E6QBB6HMZX` | 1102 | 0/2/0/0 | 0/0 | 50 | 89 | 511 | 2 | Asia/Dubai | USD |
| train | `EP-TN0EFP12G5` | 1103 | 0/0/3/0 | 0/0 | 45 | 74 | 377 | 1 | Asia/Tokyo | JPY |
| train | `EP-RC4H3QHDHH` | 1104 | 0/0/0/2 | 0/0 | 60 | 107 | 669 | 3 | Etc/UTC | EUR |
| train | `EP-02233YWZ2Z` | 1105 | 0/0/0/0 | 0/0 | 12 | 22 | 210 | 1 | Asia/Almaty | KZT |
| train | `EP-EV3JAFBR0P` | 1106 | 0/0/0/0 | 0/0 | 95 | 164 | 1909 | 3 | Asia/Kolkata | GBP |
| train | `EP-9K6F80TV0W` | 1107 | 0/0/0/0 | 1/0 | 40 | 70 | 323 | 1 | Asia/Almaty | KZT |
| train | `EP-22ZXRRD8SC` | 1108 | 0/0/0/0 | 0/1 | 36 | 72 | 412 | 2 | America/Bogota | USD |
| train | `EP-TMKYZ0M7XE` | 1109 | 2/1/1/1 | 0/1 | 70 | 134 | 834 | 2 | Asia/Almaty | KZT |
| train | `EP-FT3SV52AV8` | 1110 | 2/2/1/2 | 1/1 | 95 | 165 | 1481 | 3 | Asia/Dubai | KWD |
| dev | `EP-K7C2PM5XZ2` | 2101 | 2/0/0/0 | 0/0 | 28 | 54 | 220 | 1 | Asia/Tokyo | JPY |
| dev | `EP-PJ4DZXZS5W` | 2102 | 0/1/0/0 | 0/0 | 55 | 93 | 587 | 1 | Etc/UTC | USD |
| dev | `EP-0CMTEHMK3Z` | 2103 | 0/0/2/0 | 0/0 | 48 | 89 | 487 | 2 | Asia/Almaty | KZT |
| dev | `EP-NBBTNWZGN0` | 2104 | 0/0/0/3 | 0/0 | 42 | 76 | 350 | 3 | America/Bogota | EUR |
| dev | `EP-QRS8HS6811` | 2105 | 0/0/0/0 | 0/0 | 14 | 24 | 211 | 1 | Asia/Kolkata | GBP |
| dev | `EP-D00HKJ2GD3` | 2106 | 0/0/0/0 | 0/0 | 90 | 158 | 1795 | 2 | Asia/Almaty | KZT |
| dev | `EP-R9Z69ZTEJF` | 2107 | 0/0/0/0 | 1/0 | 44 | 79 | 387 | 1 | Asia/Dubai | KWD |
| dev | `EP-9AMRPHA6M8` | 2108 | 0/0/0/0 | 0/1 | 38 | 77 | 402 | 3 | Asia/Tokyo | CNY |
| dev | `EP-S80216VQ25` | 2109 | 1/2/2/1 | 1/0 | 65 | 114 | 734 | 1 | Asia/Kolkata | USD |
| dev | `EP-EQXQDRD4DK` | 2110 | 1/1/3/1 | 1/1 | 95 | 166 | 1355 | 3 | Etc/UTC | KZT |

Error counts are listed as duplicate_invoice/payment_allocation/fx_conversion/wrong_period;
a combo yields two issues. Sizes come from `manifest.json` counts.

## Rebuild

```
python3.11 -m fingen build --configs configs/train.json configs/dev.json --out episodes --pairs
python3.11 -m fincheck verify --all episodes
```

The build is deterministic: the same configs reproduce `episodes/` byte for byte
(checked by `tests/test_episode_sets.py`). `--pairs` also writes permuted, ID-renamed copies
into `episodes/{public,private}/pairs/` (A18).

## Public and private parts

`episodes/public/` is what the agent may see. `episodes/private/` holds `truth.json` and
`provenance.json`: it must only be mounted for the grader, never for the agent.
