"""
Pricing RAG Helper — matches item descriptions against a standard Schedule of Rates (SOR)
using rapidfuzz to suggest rates and confidence scores.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

try:
    from rapidfuzz import process, fuzz
except ImportError:
    process = None
    fuzz = None

logger = logging.getLogger(__name__)

SOR_FILE = Path("standard_schedule_of_rates.json")

# Default standard rates database if none is present
DEFAULT_SOR = [
    {"description": "Earth work excavation in all kinds of soil for foundation trenches", "unit": "Cft", "rate": 12.00},
    {"description": "F-Earth work excavation in hard soil", "unit": "Cft", "rate": 15.50},
    {"description": "Plain Cement Concrete (PCC) 1:4:8 in foundation and plinth", "unit": "Cft", "rate": 190.00},
    {"description": "Reinforced Cement Concrete (RCC) 1:1.5:3 for columns, beams, slabs", "unit": "Cft", "rate": 420.00},
    {"description": "Brick work in clay bricks CM 1:6 in foundation and plinth", "unit": "Cft", "rate": 220.00},
    {"description": "Brick work in CM 1:5 in superstructure 9 inch wall", "unit": "Cft", "rate": 260.00},
    {"description": "Half brick work in CM 1:4 in superstructure 4.5 inch partition wall", "unit": "Sft", "rate": 45.00},
    {"description": "Plastering 12mm thick in CM 1:4 with smooth finish to walls", "unit": "Sft", "rate": 18.00},
    {"description": "Ceiling plastering 6mm thick in CM 1:3", "unit": "Sft", "rate": 16.50},
    {"description": "Providing and laying vitrified tiles flooring 600x600mm", "unit": "Sft", "rate": 95.00},
    {"description": "Providing and fixing steel reinforcement TMT bars FE-500", "unit": "Kg", "rate": 68.00},
    {"description": "Centering and shuttering including strutting, propping for slabs", "unit": "Sft", "rate": 35.00},
    {"description": "Painting walls with two coats of plastic emulsion paint over wall care putty", "unit": "Sft", "rate": 24.00},
    {"description": "Providing and fixing UPVC sliding windows with mosquito mesh", "unit": "Sft", "rate": 450.00},
    {"description": "Providing and fixing teak wood door frame 120x75mm", "unit": "Rft", "rate": 950.00},
]

class PricingRAG:
    """Retrieval-Augmented pricing recommendations based on standard rates."""

    def __init__(self, sor_file: Path = SOR_FILE):
        self.sor_file = sor_file
        self.rates = self._load_sor()

    def _load_sor(self) -> List[Dict[str, Any]]:
        if not self.sor_file.exists():
            try:
                with open(self.sor_file, "w") as f:
                    json.dump(DEFAULT_SOR, f, indent=2)
                return DEFAULT_SOR
            except Exception as exc:
                logger.error(f"Failed to write default SOR file: {exc}")
                return DEFAULT_SOR
        
        try:
            with open(self.sor_file, "r") as f:
                return json.load(f)
        except Exception as exc:
            logger.error(f"Failed to read SOR file: {exc}")
            return DEFAULT_SOR

    def recommend_rates(self, query_description: str, limit: int = 3) -> List[Dict[str, Any]]:
        """
        Find top matches in SOR for the given description.
        Returns a list of dicts with matching item info and match_confidence score.
        """
        if not query_description or not self.rates:
            return []

        # If rapidfuzz is not available, fall back to simple substring match
        if process is None or fuzz is None:
            matches = []
            q_lower = query_description.lower()
            for item in self.rates:
                if q_lower in item["description"].lower() or item["description"].lower() in q_lower:
                    matches.append({**item, "match_confidence": 75.0})
            return matches[:limit]

        descriptions = [r["description"] for r in self.rates]
        
        # Perform fuzzy search
        results = process.extract(
            query_description,
            descriptions,
            scorer=fuzz.token_sort_ratio,
            limit=limit
        )

        matches = []
        for match_text, score, index in results:
            if score > 30:  # Threshold to filter out completely irrelevant matches
                item = self.rates[index]
                matches.append({
                    "description": item["description"],
                    "unit": item["unit"],
                    "rate": item["rate"],
                    "match_confidence": round(score, 1)
                })
        
        return matches
