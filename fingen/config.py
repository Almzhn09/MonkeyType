"""Generator configuration (A03, CONTRACT §8).

``resolve_config(obj)`` validates a user config, fills defaults and makes every
seed-dependent choice explicit (timezone / currencies / rounding mode when they
are omitted or ``null``). The resolved config is what gets hashed
(``config_sha256``) and recorded in provenance, so the episode is fully
reproducible from it.

Config fields (all optional except ``seed``)::

    config_version      "1"
    schema_version      "1.0"
    seed                int >= 0
    split               "train" | "dev" | other [a-z0-9_-]+ (jury splits)
    size                {"invoices": 12..95, "background_entries": 0..700}
                        background_entries is a minimum: more background
                        entries are added if needed to reach 200+ ledger rows.
    period              {"start_month": "YYYY-MM", "months": 1..3}
    timezone            one of fingen.timeutil.TIMEZONES, or null (seeded)
    base_currency       one of fingen.money.CURRENCY_EXPONENTS, or null (seeded)
    foreign_currencies  list of 0..3 other currencies, or null (seeded, 1..2)
    rounding_mode       "half_up" | "half_even" | null (seeded)
    errors              {type: count} for the four CONTRACT §5 types
    combos              {"fx_partial_payment_boundary": n, "duplicate_plus_period": n}
                        (each combo yields two issues)
    distractors         {"split_payments", "refunds", "repeated_amounts",
                         "utc_boundary", "advances"}: counts
    transform           null or an object (reserved for paired episodes, A18)
"""

from __future__ import annotations

import copy
from datetime import timedelta
import hashlib
import json
import re

from . import SCHEMA_VERSION
from .money import CURRENCY_EXPONENTS, ROUNDING_MODES
from .rng import EpisodeRng
from .timeutil import TIMEZONES, period_bounds

CONFIG_VERSION = "1"
WINDOW_PAD_DAYS = 10
MAX_PAYMENT_TERMS_DAYS = 60

ERROR_TYPES = ("duplicate_invoice", "payment_allocation", "fx_conversion", "wrong_period")
COMBO_TYPES = ("fx_partial_payment_boundary", "duplicate_plus_period")
DISTRACTOR_TYPES = ("split_payments", "refunds", "repeated_amounts", "utc_boundary", "advances")

MAX_ISSUES = 12
MIN_DOCUMENTS, MAX_DOCUMENTS = 20, 200
MIN_LEDGER_ROWS, MAX_LEDGER_ROWS = 200, 2000
# A small margin keeps invoices and payments visible in the smallest ledgers.
BASELINE_MIN_ROWS = 210
INVOICE_RANGE = (12, 95)
BACKGROUND_RANGE = (0, 700)
MONTHS_RANGE = (1, 3)
MAX_FOREIGN = 3

DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "schema_version": SCHEMA_VERSION,
    "split": "train",
    "size": {"invoices": 40, "background_entries": 100},
    "period": {"start_month": "2026-03", "months": 1},
    "timezone": None,
    "base_currency": None,
    "foreign_currencies": None,
    "rounding_mode": None,
    "errors": {k: 0 for k in ERROR_TYPES},
    "combos": {k: 0 for k in COMBO_TYPES},
    "distractors": {"split_payments": 2, "refunds": 1, "repeated_amounts": 2,
                    "utc_boundary": 2, "advances": 1},
    "transform": None,
}

_TOP_KEYS = set(DEFAULTS) | {"seed"}
_SPLIT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ConfigError(ValueError):
    """Invalid generator configuration."""


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def config_sha256(resolved: dict) -> str:
    return hashlib.sha256(canonical_json(resolved)).hexdigest()


def _int(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be in [{lo}, {hi}], got {value}")
    return value


def _counts(value, name: str, keys: tuple[str, ...], hi: int) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise ConfigError(f"{name}: unknown keys {unknown}")
    return {k: _int(value.get(k, 0), f"{name}.{k}", 0, hi) for k in keys}


def required_invoice_slots(distractors, combos):
    """Invoices that distinct distractors and dedicated combinations need."""
    return (
        distractors["utc_boundary"] + 2 * distractors["repeated_amounts"]
        + distractors["split_payments"] + combos["fx_partial_payment_boundary"]
        + 2 * combos["duplicate_plus_period"]
    )


def resolve_config(obj: dict) -> dict:
    """Validate ``obj`` and return a fully specified, JSON-able config."""
    if not isinstance(obj, dict):
        raise ConfigError("config must be a JSON object")
    unknown = sorted(set(obj) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"unknown config keys {unknown}")
    if "seed" not in obj:
        raise ConfigError("seed is required")
    cfg = copy.deepcopy(DEFAULTS)
    for key, value in obj.items():
        if key in ("size", "period", "distractors", "errors", "combos") and isinstance(value, dict):
            merged = dict(cfg[key]) if key in ("size", "period") else {}
            merged.update(value)
            cfg[key] = merged if key in ("size", "period") else copy.deepcopy(value)
        else:
            cfg[key] = copy.deepcopy(value)

    if cfg["config_version"] != CONFIG_VERSION:
        raise ConfigError(f"config_version must be {CONFIG_VERSION!r}")
    if cfg["schema_version"] != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION!r}")
    seed = _int(cfg["seed"], "seed", 0, 2**63 - 1)
    if not isinstance(cfg["split"], str) or not _SPLIT_RE.fullmatch(cfg["split"]):
        raise ConfigError("split must match [a-z0-9][a-z0-9_-]*")

    size = cfg["size"]
    if not isinstance(size, dict) or sorted(size) != ["background_entries", "invoices"]:
        raise ConfigError("size must have exactly 'invoices' and 'background_entries'")
    n_inv = _int(size["invoices"], "size.invoices", *INVOICE_RANGE)
    _int(size["background_entries"], "size.background_entries", *BACKGROUND_RANGE)

    period = cfg["period"]
    if not isinstance(period, dict) or sorted(period) != ["months", "start_month"]:
        raise ConfigError("period must have exactly 'start_month' and 'months'")
    _int(period["months"], "period.months", *MONTHS_RANGE)
    try:
        start, end = period_bounds(period["start_month"], period["months"], "+00:00")
        # Reserve the extract padding plus space for adjacent dates and invoice payment terms.
        start - timedelta(days=WINDOW_PAD_DAYS + 15)
        end + timedelta(days=WINDOW_PAD_DAYS + MAX_PAYMENT_TERMS_DAYS)
    except (ValueError, TypeError, OverflowError):
        raise ConfigError("period.start_month must be YYYY-MM and leave room for adjacent dates and payment terms") from None

    errors = _counts(cfg["errors"], "errors", ERROR_TYPES, MAX_ISSUES)
    combos = _counts(cfg["combos"], "combos", COMBO_TYPES, MAX_ISSUES // 2)
    distractors = _counts(cfg["distractors"], "distractors", DISTRACTOR_TYPES, 10)
    cfg["errors"], cfg["combos"], cfg["distractors"] = errors, combos, distractors

    n_issues = sum(errors.values()) + 2 * sum(combos.values())
    if n_issues > MAX_ISSUES:
        raise ConfigError(f"total issues {n_issues} exceeds {MAX_ISSUES}")
    if n_issues > n_inv // 3:
        raise ConfigError(f"too many issues ({n_issues}) for {n_inv} invoices (max invoices//3)")
    if distractors["refunds"] > distractors["advances"]:
        raise ConfigError("distractors.refunds must not exceed distractors.advances")
    if distractors["split_payments"] > n_inv // 4 or distractors["repeated_amounts"] > n_inv // 4:
        raise ConfigError("split_payments / repeated_amounts must be <= invoices//4")
    if distractors["utc_boundary"] > 6:
        raise ConfigError("distractors.utc_boundary must be <= 6")
    required = required_invoice_slots(distractors, combos)
    if required > n_inv:
        raise ConfigError(f"distractors and combinations need {required} distinct invoices (got {n_inv})")
    # Leave a separate invoice or related receipt available for each standalone error.
    standalone = sum(errors.values())
    if required + standalone > n_inv:
        raise ConfigError(f"distractors, combinations and standalone errors need at least "
                          f"{required + standalone} invoices (got {n_inv})")
    # Upper bound on documents: every invoice paid once, split invoices paid
    # up to 3 times, plus standalone advances and refunds.
    docs_upper = 2 * n_inv + 2 * distractors["split_payments"] + distractors["advances"] \
        + distractors["refunds"] + 2 * combos["fx_partial_payment_boundary"]
    if docs_upper > MAX_DOCUMENTS:
        raise ConfigError(f"configuration may exceed {MAX_DOCUMENTS} documents ({docs_upper})")

    pick = EpisodeRng(seed).stream("config")
    # Draw all seeded choices unconditionally so explicit values don't shift others.
    tz_choice = pick.choice(sorted(TIMEZONES))
    base_choice = pick.choice(("KZT", "KZT", "KZT", "USD", "EUR", "GBP", "JPY", "CNY", "KWD"))
    n_foreign = pick.choice((1, 2, 2, 3))
    foreign_pool = list(sorted(CURRENCY_EXPONENTS))
    pick.shuffle(foreign_pool)
    rounding_choice = pick.choice(("half_up", "half_up", "half_even"))

    if cfg["timezone"] is None:
        cfg["timezone"] = tz_choice
    if not isinstance(cfg["timezone"], str) or cfg["timezone"] not in TIMEZONES:
        raise ConfigError(f"timezone must be one of {sorted(TIMEZONES)}")
    if cfg["base_currency"] is None:
        cfg["base_currency"] = base_choice
    base = cfg["base_currency"]
    if not isinstance(base, str) or base not in CURRENCY_EXPONENTS:
        raise ConfigError(f"base_currency must be one of {sorted(CURRENCY_EXPONENTS)}")
    if cfg["foreign_currencies"] is None:
        cfg["foreign_currencies"] = sorted([c for c in foreign_pool if c != base][:n_foreign])
    fc = cfg["foreign_currencies"]
    if not isinstance(fc, list) or len(fc) > MAX_FOREIGN:
        raise ConfigError(f"foreign_currencies must be a list of at most {MAX_FOREIGN}")
    if any(not isinstance(c, str) or c not in CURRENCY_EXPONENTS or c == base for c in fc) \
            or len(set(fc)) != len(fc):
        raise ConfigError("foreign_currencies must be distinct known currencies other than base")
    if (errors["fx_conversion"] or combos["fx_partial_payment_boundary"]) and not fc:
        raise ConfigError("fx_conversion errors need at least one foreign currency")
    if cfg["rounding_mode"] is None:
        cfg["rounding_mode"] = rounding_choice
    if not isinstance(cfg["rounding_mode"], str) or cfg["rounding_mode"] not in ROUNDING_MODES:
        raise ConfigError("rounding_mode must be 'half_up' or 'half_even'")
    if cfg["transform"] is not None and not isinstance(cfg["transform"], dict):
        raise ConfigError("transform must be null or an object")
    try:
        canonical_json(cfg)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config is not JSON-serialisable: {exc}") from None
    return cfg


def load_config_file(path: str) -> list[dict]:
    """Read a config file: a single config object, a list of configs, or
    ``{"episodes": [...]}``. Returns the raw (unresolved) config objects."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "episodes" in data:
        items = data["episodes"]
        if not isinstance(items, list):
            raise ConfigError("'episodes' must be a list")
    else:
        items = [data]
    if not items:
        raise ConfigError("config file contains no episodes")
    return items
