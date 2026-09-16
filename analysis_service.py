import json
import os
import re
import urllib.parse
import urllib.request


class RentalAnalysisService:
    """Generates a real, data-backed rental revenue analysis for a property.

    Uses the RealEstateAPI (realestateapi.com) v2 endpoints:
      - PropertyDetail: county/public-record data for a property (beds, baths, sqft,
        estimated value, median income, HUD fair-market rents, suggested rent).
      - PropertyAvm: lender-grade automated valuation with a confidence range.
    The Airbnb revenue estimate is built from this real data using clearly labelled
    industry-standard assumptions (STR nightly premium over long-term rent, occupancy).
    """

    BASE_URL = "https://api.realestateapi.com/v2"

    def __init__(self):
        self.api_key = os.getenv("REALESTATE_API_KEY", "")
        self.user_id = os.getenv("REALESTATE_USER_ID", "bizstack-hosts")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> list:
        return [
            ("Accept", "application/json"),
            ("Content-Type", "application/json"),
            ("x-api-key", self.api_key),
            ("x-user-id", self.user_id),
        ]

    def _post(self, path: str, payload: dict):
        req = urllib.request.Request(
            self.BASE_URL + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=dict(self._headers()),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _as_number(value, default=None):
        if value is None or value == "":
            return default
        try:
            return float(str(value).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def extract_zip(address: str) -> str:
        match = re.search(r"\b(\d{5})(?:-\d{4})?\b", address or "")
        return match.group(1) if match else ""

    def analyze(self, address: str) -> dict:
        """Run the full analysis. Returns a dict safe to render on the page."""
        result = {
            "ok": False,
            "input_address": address or "",
            "zip": self.extract_zip(address or ""),
            "data": {},
        }
        if not self.is_configured():
            result["error"] = "Rental data feed not configured yet."
            return result
        if not (address or "").strip():
            result["error"] = "Please provide a property address."
            return result

        try:
            detail = self._post("/PropertyDetail", {"address": address})
        except Exception as e:
            result["error"] = f"Property lookup failed: {e}"
            return result

        data = detail.get("data") or {}

        avm = None
        try:
            avm_resp = self._post("/PropertyAvm", {"address": address, "strict": False})
            avm = avm_resp.get("data") or {}
        except Exception as e:
            print(f"⚠️ AVM lookup skipped: {e}")

        props = data.get("propertyInfo") or {}
        demographics = data.get("demographics") or {}
        lot = data.get("lotInfo") or {}

        home_value = (
            self._as_number(avm.get("avm"))
            or self._as_number(data.get("estimatedValue"))
            or self._as_number(data.get("reapiAvm"))
        )
        value_min = self._as_number(avm.get("avmMin"))
        value_max = self._as_number(avm.get("avmMax"))

        beds = self._coerce_int(props.get("beds") or props.get("bedrooms"))
        baths = self._coerce_int(props.get("baths") or props.get("bathrooms"))
        sqft = self._coerce_int(props.get("buildingSquareFeet")
                                or props.get("livingArea")
                                or props.get("squareFeet"))
        year_built = self._coerce_int(props.get("yearBuilt") or data.get("yearBuilt"))
        property_type = props.get("propertyType") or data.get("propertyType") or props.get("propertyUse") or "Single Family Home"

        median_income = self._as_number(demographics.get("medianIncome") or data.get("medianIncome"))
        suggested_rent = self._as_number(demographics.get("suggestedRent"))
        fmr_year = demographics.get("fmrYear") or ""
        fmr = {
            "efficiency": self._as_number(demographics.get("fmrEfficiency")),
            "one_bedroom": self._as_number(demographics.get("fmrOneBedroom")),
            "two_bedroom": self._as_number(demographics.get("fmrTwoBedroom")),
            "three_bedroom": self._as_number(demographics.get("fmrThreeBedroom")),
            "four_bedroom": self._as_number(demographics.get("fmrFourBedroom")),
        }
        hud_area = demographics.get("hudAreaName") or data.get("hudAreaName") or ""

        if home_value is None:
            result["error"] = "Could not determine a home value for this address."
            return result

        estimate = self._build_airbnb_estimate(suggested_rent, fmr, home_value, beds)

        result["ok"] = True
        result["data"] = {
            "property": {
                "address": address,
                "property_type": property_type,
                "beds": beds,
                "baths": baths,
                "square_feet": sqft,
                "year_built": year_built,
                "estimated_value": home_value,
                "value_min": value_min,
                "value_max": value_max,
            },
            "area": {
                "zip": result["zip"],
                "median_income": median_income,
                "suggested_monthly_rent": suggested_rent,
                "hud_area": hud_area,
                "fair_market_rents": fmr,
                "fmr_year": fmr_year,
            },
            "estimate": estimate,
        }
        return result

    def _build_airbnb_estimate(self, suggested_rent, fmr, home_value, beds):
        monthly_rent = (
            suggested_rent
            or fmr.get("two_bedroom")
            or fmr.get("three_bedroom")
            or fmr.get("one_bedroom")
            or (home_value * 0.006)  # 0.6% monthly-yield proxy when no rent data
        )
        daily_long_term = (monthly_rent * 12) / 365.0

        # STR nightly rates typically run 25-50% above the equivalent long-term daily rate.
        str_premium = 1.35
        nightly_rate = daily_long_term * str_premium

        # Existing Airbnb, self-managed: national-average STR occupancy.
        occupancy_base = 0.55
        # With Broom Service co-hosting: dynamic pricing + automated ops typically add
        # ~15-20% occupancy (the site's stated uplift). Capped at a realistic ceiling.
        occupancy_boosted = min(occupancy_base * 1.18, 0.85)

        gross_base = nightly_rate * 365 * occupancy_base
        gross_boosted = nightly_rate * 365 * occupancy_boosted

        # Operating costs ~28% of revenue when self-managing (cleaning, supplies,
        # utilities, maintenance, insurance).
        opex_pct = 0.28
        non_cleaning_opex_pct = 0.18  # with Broom Service, cleaning is guest-funded

        cohost_pct = 0.12  # Broom Service Digital Co-Hosting fee (12% of gross)

        net_self = gross_base * (1 - opex_pct)
        net_bizstack = gross_boosted * (1 - non_cleaning_opex_pct - cohost_pct)
        extra_with_bizstack = net_bizstack - net_self

        return {
            "assumptions": {
                "nightly_rate": nightly_rate,
                "occupancy_base": occupancy_base,
                "occupancy_boosted": round(occupancy_boosted, 3),
                "str_premium": str_premium,
                "opex_percent": opex_pct,
                "non_cleaning_opex_percent": non_cleaning_opex_pct,
                "cohost_percent": cohost_pct,
            },
            "existing_airbnb": {
                "gross_annual": gross_base,
                "gross_monthly": gross_base / 12,
                "gross_annual_low": nightly_rate * 365 * 0.42,
                "gross_annual_high": nightly_rate * 365 * 0.70,
            },
            "with_bizstack": {
                "gross_annual": gross_boosted,
                "gross_monthly": gross_boosted / 12,
                "cohost_fee_annual": gross_boosted * cohost_pct,
            },
            "net": {
                "net_annual_self_managed": net_self,
                "net_monthly_self_managed": net_self / 12,
                "net_annual_cohosted": net_bizstack,
                "net_monthly_cohosted": net_bizstack / 12,
                "additional_annual_with_bizstack": extra_with_bizstack,
                "additional_monthly_with_bizstack": extra_with_bizstack / 12,
            },
            "monthly_rent_input": monthly_rent,
        }

    @staticmethod
    def _coerce_int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def map_embed_url(self, address: str) -> str:
        query = urllib.parse.quote(f"{address}")
        return f"https://www.google.com/maps?q={query}&z=13&output=embed"