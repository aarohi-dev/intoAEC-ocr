"""
Domain Corrector — fuzzy correction of construction terminology using
rapidfuzz and curated construction vocabulary dictionaries.

Handles:
- OCR mis-spellings (Plastreing → Plastering)
- Unit normalisation (sqft → Sqft, cft → Cft)
- Known construction term correction
- All-caps normalisation
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Construction vocabulary dictionaries
# ─────────────────────────────────────────────────────────────────────────────

# Primary construction terms — used for fuzzy correction of descriptions
CONSTRUCTION_VOCABULARY: List[str] = [
    # Structural
    "Earth Work", "Earthwork", "Excavation", "Backfilling",
    "PCC", "RCC", "Plain Cement Concrete", "Reinforced Cement Concrete",
    "Footing", "Foundation", "Raft Foundation", "Pile Foundation",
    "Column", "Beam", "Lintel", "Sill", "Roof Slab", "Chajja",
    "Plinth Beam", "Tie Beam", "Staircase", "Landing",
    "Shuttering", "Formwork", "Centering", "De-shuttering",
    "TMT Steel", "Reinforcement Steel", "Binding Wire",
    "Cement", "Sand", "Aggregate", "Coarse Aggregate", "Fine Aggregate",
    "M Sand", "P Sand", "River Sand", "Quarry Dust",
    "Concrete Mix", "M15", "M20", "M25", "M30",
    # Masonry
    "Brick Work", "Brickwork", "Fly Ash Brick", "Clay Brick",
    "AAC Block", "Hollow Block", "Solid Block",
    "Mortar", "Pointing",
    # Finishing
    "Plastering", "Plaster", "External Plaster", "Internal Plaster",
    "Putty", "Primer", "Painting", "Emulsion Paint", "Enamel Paint",
    "Texture Coat", "Waterproofing", "Damp Proof Course", "DPC",
    # Flooring
    "Flooring", "Tile Work", "Ceramic Tile", "Vitrified Tile",
    "Granite", "Marble", "Kota Stone", "Anti-skid Tile",
    "Skirting", "Dado", "Chequered Tile",
    # Doors & Windows
    "UPVC Window", "Aluminium Window", "Wooden Window",
    "UPVC Door", "Aluminium Door", "Wooden Door",
    "Ventilator", "Grille", "Mosquito Net", "Sliding Window",
    "Casement Window", "French Door",
    "Door Frame", "Window Frame", "Architrave",
    # Electrical
    "Electrical Works", "Wiring", "Conduit", "PVC Conduit",
    "Switch Board", "Switchboard", "DB", "Distribution Board",
    "MCB", "ELCB", "RCCB", "Earth Leakage",
    "Point Wiring", "Fan Point", "Light Point",
    # Plumbing
    "Plumbing Works", "Sanitary", "Water Supply",
    "CPVC Pipe", "UPVC Pipe", "PVC Pipe", "GI Pipe",
    "Fitting", "Elbow", "Tee", "Reducer",
    "WC", "Water Closet", "Commode", "EWC",
    "Wash Basin", "Kitchen Sink", "Bath Tub",
    "Shower", "Tap", "Faucet", "Stop Cock", "Ball Valve",
    "Sump", "Overhead Tank", "Septic Tank",
    # External
    "Compound Wall", "Boundary Wall", "Parapet Wall",
    "Gate", "Fence", "Fencing",
    "Paving", "Driveway", "Pathway", "Ramp",
    # Miscellaneous
    "Scaffolding", "Labour", "Contractor", "Material",
    "Sundry", "Contingency", "Provisional Sum",
    "Lump Sum", "LS", "L/S",
]

# Unit vocabulary — canonical spellings
UNIT_VOCABULARY: Dict[str, str] = {
    # Volume
    "cft":    "Cft",
    "cubic feet": "Cft",
    "cu ft":  "Cft",
    "cu.ft":  "Cft",
    "cum":    "CuM",
    "m3":     "CuM",
    "cmt":    "CuM",
    # Area
    "sqft":   "Sqft",
    "sft":    "Sqft",
    "sq ft":  "Sqft",
    "sq.ft":  "Sqft",
    "sqmt":   "SqM",
    "sqm":    "SqM",
    "m2":     "SqM",
    "sq.m":   "SqM",
    # Length
    "rft":    "Rft",
    "rm":     "RM",
    "rmt":    "RM",
    "lm":     "LM",
    "m":      "M",
    # Count
    "no":     "No.",
    "nos":    "Nos.",
    "no.":    "No.",
    "nos.":   "Nos.",
    "unit":   "Unit",
    "units":  "Units",
    "set":    "Set",
    "sets":   "Sets",
    "pair":   "Pair",
    "pairs":  "Pairs",
    # Weight
    "kg":     "Kg",
    "kgs":    "Kg",
    "mt":     "MT",
    "ton":    "MT",
    "tons":   "MT",
    # Liquid
    "ltr":    "Ltr",
    "litre":  "Ltr",
    "litres": "Ltr",
    "l":      "Ltr",
    # Bags
    "bag":    "Bag",
    "bags":   "Bags",
    # Lump sum
    "ls":     "L/S",
    "l/s":    "L/S",
    "lump sum": "L/S",
    # Running
    "rm":     "RM",
}

# Common OCR mis-spellings → correct form
_OCR_CORRECTIONS: Dict[str, str] = {
    "plastreing": "Plastering",
    "plastring": "Plastering",
    "winodw": "Window",
    "windwo": "Window",
    "excahust": "Exhaust",
    "rsidential": "Residential",
    "residntial": "Residential",
    "founation": "Foundation",
    "foudnation": "Foundation",
    "concerte": "Concrete",
    "concreete": "Concrete",
    "cemnet": "Cement",
    "aggregte": "Aggregate",
    "brickwok": "Brickwork",
    "brikwork": "Brickwork",
    "shuterring": "Shuttering",
    "shutring": "Shuttering",
    "reinfrcement": "Reinforcement",
    "rienforcement": "Reinforcement",
    "steell": "Steel",
    "columnm": "Column",
    "beamm": "Beam",
    "footting": "Footing",
    "excavaion": "Excavation",
    "earthwrok": "Earthwork",
    "paintig": "Painting",
    "plumbig": "Plumbing",
    "eletrical": "Electrical",
    "elctrical": "Electrical",
    "waterproofng": "Waterproofing",
    "floring": "Flooring",
    "tiilework": "Tile Work",
    "tilework": "Tile Work",
    "granit": "Granite",
    "marbl": "Marble",
    "upvc": "UPVC",
    "pvc": "PVC",
    "cpvc": "CPVC",
    "rcc": "RCC",
    "pcc": "PCC",
    "dpc": "DPC",
    "tmt": "TMT",
}


# ─────────────────────────────────────────────────────────────────────────────
# DomainCorrector
# ─────────────────────────────────────────────────────────────────────────────

class DomainCorrector:
    """
    Corrects construction domain terminology using:
    1. Direct dictionary lookup for known OCR errors
    2. rapidfuzz fuzzy matching against the construction vocabulary
    3. Unit normalisation

    Args:
        fuzzy_threshold: Minimum similarity score (0-100) for fuzzy match acceptance.
                         Lower values = more aggressive correction. Default 85.
    """

    def __init__(self, fuzzy_threshold: int = 85):
        self.fuzzy_threshold = fuzzy_threshold
        self._vocab = CONSTRUCTION_VOCABULARY
        self._unit_map = UNIT_VOCABULARY
        self._corrections = _OCR_CORRECTIONS
        self._rapidfuzz_available = self._check_rapidfuzz()

    @staticmethod
    def _check_rapidfuzz() -> bool:
        try:
            import rapidfuzz  # noqa: F401
            return True
        except ImportError:
            logger.warning("rapidfuzz not installed — fuzzy correction disabled. pip install rapidfuzz")
            return False

    # ── word-level correction ────────────────────────────────────────────────

    def _correct_word(self, word: str) -> str:
        """Correct a single word using dictionary then fuzzy matching."""
        lc = word.lower().strip()

        # Direct lookup
        if lc in self._corrections:
            return self._corrections[lc]

        # Skip short words, numbers, and item numbers
        if len(lc) <= 3 or re.match(r"^\d[\d.,]*$", lc) or re.match(r"^\d+\.\d+", lc):
            return word

        if not self._rapidfuzz_available:
            return word

        try:
            from rapidfuzz import process, fuzz
            match = process.extractOne(
                word,
                self._vocab,
                scorer=fuzz.WRatio,
                score_cutoff=self.fuzzy_threshold,
            )
            if match:
                # Only return the correction if it meaningfully differs
                corrected = match[0]
                if corrected.lower() != lc:
                    logger.debug(f"Fuzzy corrected '{word}' → '{corrected}' (score={match[1]:.0f})")
                    return corrected
        except Exception as exc:
            logger.debug(f"rapidfuzz error: {exc}")

        return word

    def correct(self, text: str) -> str:
        """
        Correct a full description string.

        Applies:
        1. Known OCR error dictionary (phrase-level)
        2. Fuzzy matching per word
        """
        if not text or not text.strip():
            return text

        # Phrase-level corrections first
        lc_text = text.lower().strip()
        for error, correction in self._corrections.items():
            if error in lc_text:
                # Case-insensitive replacement
                pattern = re.compile(re.escape(error), re.IGNORECASE)
                text = pattern.sub(correction, text)

        # Word-level fuzzy correction
        words = text.split()
        corrected_words = []
        i = 0
        while i < len(words):
            # Try two-word phrases first
            if i + 1 < len(words):
                two_word = f"{words[i]} {words[i+1]}"
                lc_two = two_word.lower()
                if lc_two in self._corrections:
                    corrected_words.append(self._corrections[lc_two])
                    i += 2
                    continue
            corrected_words.append(self._correct_word(words[i]))
            i += 1

        return " ".join(corrected_words)

    # ── unit normalisation ───────────────────────────────────────────────────

    def normalize_unit(self, unit: str) -> str:
        """Normalise a unit string to canonical form."""
        if not unit:
            return unit
        lc = unit.lower().strip().rstrip(".")
        return self._unit_map.get(lc, unit)

    # ── batch correction ─────────────────────────────────────────────────────

    def correct_row(self, row) -> None:
        """In-place correction of a BOQRow's description and unit."""
        from boqparser.table.table_reconstructor import BOQRow
        if isinstance(row, BOQRow):
            row.description = self.correct(row.description)
            row.unit = self.normalize_unit(row.unit)

    def correct_all(self, rows: list) -> list:
        """Apply correction to all BOQRows. Returns the same list (mutated)."""
        for row in rows:
            self.correct_row(row)
        return rows
