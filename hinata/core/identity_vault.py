"""Secure local Identity Vault — stores your personal profiles for one-click form filling.

No cloud, no external API. Every profile lives in your local HINATA database
(optionally envelope-encrypted when security.encrypt_at_rest is enabled).

Features:
- Multiple named profiles (e.g. "personal", "work", "family")
- 50+ canonical fields + unlimited custom fields
- Alias-aware matching for intelligent web form autofill
- Default profile support
- Import/export helpers
- Secret-safe: refuses to store raw passwords/tokens as autofillable text
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from hinata.database.database import Database
from hinata.security.redaction import contains_secrets

# Canonical field definitions — the "strong" set that covers 99% of web forms.
CANONICAL_FIELDS: dict[str, dict[str, Any]] = {
    # Personal identity
    "first_name": {"label": "First Name", "aliases": ["fname", "firstname", "given_name", "given-name", "forename"], "group": "name"},
    "last_name": {"label": "Last Name", "aliases": ["lname", "lastname", "surname", "family_name", "family-name"], "group": "name"},
    "middle_name": {"label": "Middle Name", "aliases": ["middlename", "middleinitial"], "group": "name"},
    "full_name": {"label": "Full Name", "aliases": ["name", "fullname", "your_name", "yourname", "complete_name", "display_name"], "group": "name"},
    "title": {"label": "Title / Prefix", "aliases": ["prefix", "salutation", "honorific", "mr_ms"], "group": "name"},
    "suffix": {"label": "Suffix", "aliases": ["suffix_name", "generation"], "group": "name"},
    # Contact
    "email": {"label": "Email", "aliases": ["e_mail", "email_address", "emailaddress", "mail"], "group": "contact"},
    "phone": {"label": "Phone", "aliases": ["telephone", "tel", "mobile", "mobile_number", "mobile-number", "phone_number", "phone-number", "cell"], "group": "contact"},
    "phone_country_code": {"label": "Phone Country Code", "aliases": ["country_code", "dial_code", "phone_code"], "group": "contact"},
    "alternate_phone": {"label": "Alternate Phone", "aliases": ["alt_phone", "secondary_phone"], "group": "contact"},
    # Address
    "address_line1": {"label": "Street Address", "aliases": ["address", "street", "street_address", "street-address", "address1", "addr1", "line1"], "group": "address"},
    "address_line2": {"label": "Apt / Suite", "aliases": ["address2", "addr2", "line2", "apartment", "suite", "unit"], "group": "address"},
    "city": {"label": "City", "aliases": ["town", "locality", "city_name"], "group": "address"},
    "state": {"label": "State / Province", "aliases": ["province", "region", "state_province", "administrative_area"], "group": "address"},
    "zip": {"label": "ZIP / Postal Code", "aliases": ["zipcode", "zip_code", "postal", "postal_code", "postcode", "pincode", "pin_code"], "group": "address"},
    "country": {"label": "Country", "aliases": ["country_name", "nation"], "group": "address"},
    "country_code": {"label": "Country Code (2-letter)", "aliases": ["countrycode", "iso_country"], "group": "address"},
    # Personal details
    "date_of_birth": {"label": "Date of Birth", "aliases": ["dob", "birthdate", "bdate", "birthday", "birth_date"], "group": "personal"},
    "gender": {"label": "Gender", "aliases": ["sex", "gender_identity"], "group": "personal"},
    "nationality": {"label": "Nationality", "aliases": ["citizenship"], "group": "personal"},
    "marital_status": {"label": "Marital Status", "aliases": ["marital"], "group": "personal"},
    # Government IDs (stored but flagged sensitive — never logged)
    "pan": {"label": "PAN", "aliases": ["pan_number", "pan_no"], "group": "id"},
    "aadhaar": {"label": "Aadhaar", "aliases": ["aadhar", "uidai"], "group": "id"},
    "passport_number": {"label": "Passport Number", "aliases": ["passport", "passport_no"], "group": "id"},
    "driving_license": {"label": "Driving License", "aliases": ["dl_number", "license_number"], "group": "id"},
    "ssn": {"label": "SSN (last 4)", "aliases": ["social_security"], "group": "id"},
    "national_id": {"label": "National ID", "aliases": ["id_number", "gov_id"], "group": "id"},
    # Professional
    "company": {"label": "Company / Organization", "aliases": ["organization", "organisation", "employer", "company_name", "org"], "group": "work"},
    "job_title": {"label": "Job Title", "aliases": ["designation", "position", "role", "title_at_company", "occupation"], "group": "work"},
    "department": {"label": "Department", "aliases": ["dept", "division"], "group": "work"},
    "work_email": {"label": "Work Email", "aliases": ["business_email", "office_email"], "group": "work"},
    "work_phone": {"label": "Work Phone", "aliases": ["office_phone"], "group": "work"},
    "website": {"label": "Website / URL", "aliases": ["url", "homepage", "personal_website", "linkedin", "portfolio"], "group": "work"},
    "experience_years": {"label": "Years of Experience", "aliases": ["experience", "yoe"], "group": "work"},
    # Education
    "education": {"label": "Highest Education", "aliases": ["degree", "qualification"], "group": "education"},
    "university": {"label": "University / College", "aliases": ["college", "school", "institution"], "group": "education"},
    "graduation_year": {"label": "Graduation Year", "aliases": ["grad_year", "passing_year"], "group": "education"},
    # Preferences
    "language": {"label": "Preferred Language", "aliases": ["preferred_language", "lang"], "group": "pref"},
    "timezone": {"label": "Timezone", "aliases": ["tz"], "group": "pref"},
    # Payment (never auto-submit, only fill when explicitly confirmed)
    "card_holder": {"label": "Card Holder Name", "aliases": ["cardholder", "name_on_card"], "group": "payment"},
    "card_number": {"label": "Card Number", "aliases": ["cc_number", "pan_card"], "group": "payment"},
    "card_expiry": {"label": "Card Expiry (MM/YY)", "aliases": ["expiry", "exp_date", "card_exp"], "group": "payment"},
    "card_cvv": {"label": "CVV", "aliases": ["cvv2", "cvc", "security_code"], "group": "payment"},
}

# Build reverse alias index for fast matching
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical, meta in CANONICAL_FIELDS.items():
    _ALIAS_TO_CANONICAL[canonical] = canonical
    _ALIAS_TO_CANONICAL[canonical.replace("_", "")] = canonical
    _ALIAS_TO_CANONICAL[canonical.replace("_", "-")] = canonical
    for alias in meta.get("aliases", []):
        _ALIAS_TO_CANONICAL[alias.lower()] = canonical
        _ALIAS_TO_CANONICAL[alias.lower().replace("_", "").replace("-", "")] = canonical

#: Field-name fragments that always mean "a credential", never "autofill data".
#: Matched against the normalised key, so user_pin / OTP-code / myPasswd all hit.
SECRET_FIELD_NAME_TOKENS = frozenset(
    {
        "password", "passwd", "pwd", "secret", "token", "apikey", "api_key",
        "pin", "passcode", "otp", "cvv", "cvc", "securitycode", "security_code",
        "securityanswer", "security_answer", "mfa", "2fa", "totp", "recoverycode",
        "recovery_code", "privatekey", "private_key", "seedphrase", "seed_phrase",
    }
)
_SECRET_FIELD_SUBSTRINGS = ("password", "passwd", "secret", "apikey", "privatekey",
                            "passcode", "securitycode", "recoverycode", "seedphrase")


def _is_secret_field_name(key: str) -> bool:
    """True when a field name looks like a credential that must not be stored."""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if not normalized:
        return False
    if normalized in {t.replace("_", "") for t in SECRET_FIELD_NAME_TOKENS}:
        return True
    if any(fragment in normalized for fragment in _SECRET_FIELD_SUBSTRINGS):
        return True
    # Short, ambiguous tokens only match as a delimited word: "user_pin" and
    # "otp_code" are credentials, "pinned_post" and "spinning" are not.
    tokens = {t for t in re.split(r"[^a-z0-9]+", key.lower()) if t}
    return bool(tokens & {"pin", "otp", "cvv", "cvc", "totp", "mfa", "2fa", "pwd"})


SENSITIVE_FIELDS = {"pan", "aadhaar", "passport_number", "driving_license", "ssn", "national_id", "card_number", "card_cvv"}


def normalize_key(raw: str) -> str:
    """Normalize a raw HTML field name/id to a canonical lookup key."""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def canonicalize_field(raw: str) -> str | None:
    """Map any raw field identifier to canonical field name if known."""
    norm = normalize_key(raw)
    if norm in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[norm]
    # Try without separators
    compact = norm.replace("_", "")
    if compact in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[compact]
    return None


@dataclass(slots=True)
class IdentityProfile:
    profile_id: str
    name: str  # lowercase key, e.g. "personal"
    display_name: str
    fields: dict[str, str]
    is_default: bool
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "display_name": self.display_name,
            "fields": self.fields,
            "is_default": self.is_default,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class IdentityVault:
    """Local encrypted identity storage for autofill."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self.db.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS identity_profiles (
                    profile_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    fields_json TEXT NOT NULL DEFAULT '{}',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_identity_profiles_default ON identity_profiles(is_default);
                """
            )

    # ------------------------------------------------------------------ CRUD
    def save_profile(self, name: str, fields: dict[str, str], *, display_name: str = "", make_default: bool = False) -> IdentityProfile:
        if not name or not name.strip():
            raise ValueError("Profile name cannot be empty")
        key = name.strip().lower()[:64]
        if not re.fullmatch(r"[a-z0-9_\- ]+", key):
            raise ValueError("Profile name may contain letters, digits, space, underscore, hyphen")
        clean_fields: dict[str, str] = {}
        for k, v in fields.items():
            ck = k.strip().lower()[:80]
            if not ck:
                continue
            cv = str(v).strip()
            if not cv:
                continue
            if len(cv) > 2000:
                cv = cv[:2000]
            # Refuse to store raw secrets as autofill — guide to secure storage
            # We allow IDs like PAN/Aadhaar but flag them; still block generic secrets
            # Only block obvious tokens/passwords for canonical password fields
            # Blocklist by *normalised* name and by substring, so "user_pin",
            # "otp_code" and "Passwd" cannot slip past an exact-match check.
            if _is_secret_field_name(ck):
                raise ValueError(f"Refusing to store sensitive field '{ck}' as plain autofill data. Use a password manager.")
            if "\x00" in cv:
                raise ValueError("Field value contains null byte")
            # A secret can hide in an innocuous-looking field ("note": "sk-proj-...").
            # The module promises it refuses to store raw credentials, so scan the
            # value too, not just the field name. IDs such as PAN/Aadhaar/SSN are
            # deliberately still allowed (flagged sensitive, never logged).
            if contains_secrets(cv):
                raise ValueError(
                    f"Refusing to store a credential-like value in field '{ck}' "
                    "(detected key/token material). Use a password manager."
                )
            clean_fields[ck] = cv

        if not clean_fields:
            raise ValueError("At least one field value is required")

        # Auto-derive full_name if first+last provided
        if "first_name" in clean_fields and "last_name" in clean_fields and "full_name" not in clean_fields:
            clean_fields["full_name"] = f"{clean_fields['first_name']} {clean_fields['last_name']}".strip()
        if "full_name" in clean_fields and "first_name" not in clean_fields:
            parts = clean_fields["full_name"].split()
            if len(parts) >= 2:
                clean_fields.setdefault("first_name", parts[0])
                clean_fields.setdefault("last_name", parts[-1])

        fields_json = json.dumps(clean_fields, ensure_ascii=False)
        profile_id = uuid.uuid4().hex[:12]
        disp = display_name.strip() or key.title()

        # Upsert — if profile with same name exists, update it
        existing = self.db.fetch_one("SELECT profile_id FROM identity_profiles WHERE name = ?", (key,))
        if existing:
            profile_id = str(existing["profile_id"])
            self.db.execute(
                "UPDATE identity_profiles SET display_name = ?, fields_json = ?, updated_at = datetime('now') WHERE name = ?",
                (disp, fields_json, key),
            )
        else:
            self.db.execute(
                "INSERT INTO identity_profiles(profile_id, name, display_name, fields_json, is_default) VALUES(?, ?, ?, ?, ?)",
                (profile_id, key, disp, fields_json, 0),
            )

        if make_default:
            self.set_default(key)

        return self.get_profile(key)  # type: ignore

    def get_profile(self, name: str) -> IdentityProfile | None:
        row = self.db.fetch_one("SELECT * FROM identity_profiles WHERE name = ?", (name.strip().lower(),))
        if not row:
            return None
        try:
            fields = json.loads(row["fields_json"])
        except Exception:
            fields = {}
        return IdentityProfile(
            profile_id=str(row["profile_id"]),
            name=str(row["name"]),
            display_name=str(row["display_name"]),
            fields=dict(fields),
            is_default=bool(row["is_default"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_default(self) -> IdentityProfile | None:
        row = self.db.fetch_one("SELECT * FROM identity_profiles WHERE is_default = 1 ORDER BY updated_at DESC LIMIT 1")
        if row:
            try:
                fields = json.loads(row["fields_json"])
            except Exception:
                fields = {}
            return IdentityProfile(
                profile_id=str(row["profile_id"]),
                name=str(row["name"]),
                display_name=str(row["display_name"]),
                fields=dict(fields),
                is_default=True,
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        # Fallback to first profile if exists
        row = self.db.fetch_one("SELECT * FROM identity_profiles ORDER BY created_at ASC LIMIT 1")
        if row:
            try:
                fields = json.loads(row["fields_json"])
            except Exception:
                fields = {}
            return IdentityProfile(
                profile_id=str(row["profile_id"]),
                name=str(row["name"]),
                display_name=str(row["display_name"]),
                fields=dict(fields),
                is_default=False,
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        return None

    def list_profiles(self) -> list[IdentityProfile]:
        rows = self.db.fetch_all("SELECT * FROM identity_profiles ORDER BY is_default DESC, name ASC")
        out: list[IdentityProfile] = []
        for row in rows:
            try:
                fields = json.loads(row["fields_json"])
            except Exception:
                fields = {}
            out.append(IdentityProfile(
                profile_id=str(row["profile_id"]),
                name=str(row["name"]),
                display_name=str(row["display_name"]),
                fields=dict(fields),
                is_default=bool(row["is_default"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            ))
        return out

    def delete_profile(self, name: str) -> bool:
        cur = self.db.fetch_one("SELECT 1 FROM identity_profiles WHERE name = ?", (name.strip().lower(),))
        if not cur:
            return False
        self.db.execute("DELETE FROM identity_profiles WHERE name = ?", (name.strip().lower(),))
        return True

    def set_default(self, name: str) -> None:
        key = name.strip().lower()
        row = self.db.fetch_one("SELECT 1 FROM identity_profiles WHERE name = ?", (key,))
        if not row:
            raise ValueError(f"No profile named '{name}'")
        self.db.execute("UPDATE identity_profiles SET is_default = 0")
        self.db.execute("UPDATE identity_profiles SET is_default = 1, updated_at = datetime('now') WHERE name = ?", (key,))

    def update_field(self, profile_name: str, field_key: str, value: str) -> IdentityProfile:
        profile = self.get_profile(profile_name)
        if not profile:
            raise ValueError(f"No profile named '{profile_name}'")
        fk = field_key.strip().lower()[:80]
        if not fk:
            raise ValueError("Field key cannot be empty")
        if value.strip():
            profile.fields[fk] = value.strip()[:2000]
        else:
            profile.fields.pop(fk, None)
        self.save_profile(profile.name, profile.fields, display_name=profile.display_name, make_default=profile.is_default)
        return self.get_profile(profile.name)  # type: ignore

    # ------------------------------------------------------------------ helpers for autofill
    def resolve_value(self, field_key: str, profile: IdentityProfile | None = None) -> str | None:
        """Resolve a field from profile with alias and derived fallback."""
        prof = profile or self.get_default()
        if not prof:
            return None
        # Direct hit
        norm = normalize_key(field_key)
        if norm in prof.fields:
            return prof.fields[norm]
        # Alias via canonical
        canon = canonicalize_field(field_key)
        if canon and canon in prof.fields:
            return prof.fields[canon]
        # Try compact
        compact = norm.replace("_", "")
        for k, v in prof.fields.items():
            if normalize_key(k).replace("_", "") == compact:
                return v
        # Special derived cases
        if field_key.lower() in {"name", "fullname", "full_name", "your_name"}:
            if "full_name" in prof.fields:
                return prof.fields["full_name"]
            if "first_name" in prof.fields and "last_name" in prof.fields:
                return f"{prof.fields['first_name']} {prof.fields['last_name']}"
        return None

    def export_for_llm(self, profile_name: str | None = None) -> dict[str, Any]:
        """Redacted export for debugging — sensitive fields masked."""
        prof = self.get_profile(profile_name) if profile_name else self.get_default()
        if not prof:
            return {"error": "no profile"}
        redacted: dict[str, str] = {}
        for k, v in prof.fields.items():
            if k in SENSITIVE_FIELDS:
                # Show last 4 chars only
                redacted[k] = "***" + v[-4:] if len(v) > 4 else "***"
            else:
                redacted[k] = v
        return {"profile": prof.name, "fields": redacted, "is_default": prof.is_default}

    def fill_suggestions(self, html_fields: list[dict[str, Any]], profile_name: str | None = None) -> list[dict[str, Any]]:
        """Given parsed HTML fields, suggest values from profile."""
        prof = self.get_profile(profile_name) if profile_name else self.get_default()
        if not prof:
            return []
        suggestions = []
        for f in html_fields:
            # Try multiple signals for field identity
            signals = [
                f.get("name", ""),
                f.get("id", ""),
                f.get("placeholder", ""),
                f.get("label", ""),
                f.get("autocomplete", ""),
                f.get("aria_label", ""),
            ]
            best = None
            best_score = 0
            for sig in signals:
                if not sig:
                    continue
                canon = canonicalize_field(sig)
                if canon and canon in prof.fields:
                    # Score by signal priority: name/id > autocomplete > label > placeholder
                    priority = {"name": 5, "id": 5, "autocomplete": 4, "label": 3, "aria_label": 3, "placeholder": 2}
                    # Find which signal this is
                    for k, val in f.items():
                        if val == sig and k in priority:
                            score = priority[k]
                            if score > best_score:
                                best = canon
                                best_score = score
                # Also try direct alias match
                val = self.resolve_value(sig, prof)
                if val and not best:
                    best = sig
                    best_score = 1
            if best:
                val = self.resolve_value(best, prof)
                if val:
                    suggestions.append({"field": f, "canonical": best, "value": val, "confidence": best_score})
        return suggestions
