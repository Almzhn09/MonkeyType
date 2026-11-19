"""Synthetic names (company, counterparties, accounts, line items).

Names are composed from invented syllables plus generic words so that they do
not denote real organisations.
"""

from __future__ import annotations

import random

_SYL_A = ("ka", "ra", "zan", "tel", "mir", "vor", "sa", "len", "dor", "bek", "nis", "tal",
          "qor", "ul", "ar", "ves", "lun", "tor", "ema", "zhy", "oro", "kel", "rin", "ast")
_SYL_B = ("an", "ex", "ia", "on", "ar", "is", "um", "et", "ora", "ix", "en", "ay", "ul", "ik")

_LEGAL = ("LLP", "Ltd", "JSC", "LLC", "GmbH", "S.A.S.", "Pte Ltd", "FZ-LLC", "K.K.", "PLC", "Co.")

INDUSTRIES: dict[str, dict] = {
    "logistics": {
        "words": ("Logistics", "Freight", "Cargo Lines", "Transit"),
        "items": ("Container haulage", "Customs brokerage", "Warehouse storage (pallet-week)",
                  "Last-mile delivery", "Freight insurance", "Cross-dock handling",
                  "Fuel surcharge", "Route planning service"),
        "expenses": ("Fuel", "Vehicle maintenance", "Warehouse rent", "Driver payroll", "Tolls"),
    },
    "software": {
        "words": ("Systems", "Analytics", "Cloud", "Software", "Data Works"),
        "items": ("Annual platform licence", "Implementation services (hour)", "Support plan",
                  "Data migration", "API usage block", "Training workshop", "Custom report",
                  "Security audit"),
        "expenses": ("Hosting", "Salaries", "Office rent", "Software subscriptions", "Marketing"),
    },
    "foods": {
        "words": ("Foods", "Agro", "Dairy", "Harvest", "Provisions"),
        "items": ("Wheat flour 50kg", "Sunflower oil 20L", "Dried apricots box", "Cheese wheel",
                  "Frozen berries crate", "Buckwheat 25kg", "Honey jar case", "Cold-chain delivery"),
        "expenses": ("Raw materials", "Packaging", "Electricity", "Cold storage", "Wages"),
    },
    "engineering": {
        "words": ("Engineering", "Industrial", "Machinery", "Technics"),
        "items": ("Pump overhaul", "Valve assembly", "Site inspection (day)", "Spare parts kit",
                  "Welding works (hour)", "Commissioning service", "Design drawings",
                  "Hydraulic hose set"),
        "expenses": ("Materials", "Subcontractors", "Equipment lease", "Wages", "Travel"),
    },
    "textiles": {
        "words": ("Textiles", "Apparel", "Weaving", "Fabrics"),
        "items": ("Cotton fabric roll", "Wool yarn bale", "Workwear set", "Embroidery service",
                  "Dyeing batch", "Felt panels", "Sewing (unit)", "Sample collection"),
        "expenses": ("Yarn", "Machine maintenance", "Rent", "Wages", "Shipping"),
    },
    "media": {
        "words": ("Media", "Studio", "Creative", "Broadcast"),
        "items": ("Video production (day)", "Campaign design", "Ad placement slot",
                  "Photo session", "Copywriting package", "Sound mastering", "Social media plan",
                  "Translation (page)"),
        "expenses": ("Equipment rent", "Freelancers", "Licences", "Office rent", "Travel"),
    },
}

_GENERIC_CUSTOMER = ("Trading", "Group", "Partners", "Holding", "Retail", "Distribution",
                     "Services", "Supply", "Ventures", "Construction", "Market", "Labs")

ACCOUNT_NAMES: dict[str, tuple[str, ...]] = {
    "ar": ("Accounts receivable", "Trade debtors", "Receivables - customers", "Customer accounts"),
    "revenue": ("Sales revenue", "Revenue from services", "Turnover", "Operating income"),
    "bank": ("Bank - current account", "Cash at bank", "Main operating account", "Settlement account"),
    "unapplied": ("Customer advances", "Unapplied receipts", "Prepayments received", "Advances from customers"),
}


def neologism(rng: random.Random) -> str:
    n = rng.choice((1, 2, 2))
    word = "".join(rng.choice(_SYL_A) for _ in range(n)) + rng.choice(_SYL_B)
    return word.capitalize()


def company_name(rng: random.Random, industry: str) -> str:
    words = INDUSTRIES[industry]["words"]
    return f"{neologism(rng)} {rng.choice(words)} {rng.choice(_LEGAL)}"


def customer_name(rng: random.Random) -> str:
    return f"{neologism(rng)} {rng.choice(_GENERIC_CUSTOMER)} {rng.choice(_LEGAL)}"
