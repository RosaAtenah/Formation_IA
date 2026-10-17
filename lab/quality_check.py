#!/usr/bin/env python3
"""
quality_check.py - PrediCare TPA (P1): data quality & privacy checks, 2 modes.

  --mode raw    BEFORE clean.py.  Profiles the SOURCE CSV files and changes
                nothing. It measures what is wrong (nulls, formats, duplicates,
                out-of-range values, orphan keys, contract rules) and proposes
                a decision for each problem: REJECT, DROP, FIX, KEEP or REVIEW.
                The team validates these decisions, then clean.py is frozen.
                Needs only pandas (no pyarrow).

  --mode clean  AFTER clean.py (default).  Re-reads the finished Parquet files
                and checks they respect data contract v0.1. It does not import
                clean.py, so a bug in the cleaning logic cannot hide itself.

Raw mode (report = counts and CSV line numbers, never personal values)
----------------------------------------------------------------------
 1. Source files, columns versus contract, volumes versus contract.
 2. Per column: spaces, placeholder text ('NaN', 'None'...), unparsable
    values, missing required values, id formats, forbidden values, ranges.
    Per table: exact duplicate rows, duplicated primary keys.
 3. Foreign keys (orphans), invoices without lines.
 4. Contract section 9.2 rules.
 5. Fraud signals that must be KEPT (repeated numero_facture, re-used
    ordonnance_id, prices above tarif_max).
 6. Privacy: personal columns / personal-looking values outside identites_pii.
 7. Estimated rejects per table (row-level checks only) versus the allowed
    ratio.
Outputs: <report>.json and <report>.md (decision table for the team).

Clean mode (BLOQUANT = exit code 1, AVERT = warning, INFO = report only)
-----------------------------------------------------------------------
 1. Files, columns and Arrow types match the contract; no NULL in required
    columns; primary keys unique; row counts versus contract volumes.
 2. _manifest.json is consistent: rows_in - exact duplicates = rows_out +
    rejected; Parquet and rejects files really contain these counts;
    optional SHA-256 re-check of the source CSVs (--src / --labels).
 3. Identifier formats; foreign keys (0 orphan); contract section 9.2 rules.
 4. Anti over-cleaning: the injected fraud signals are still there.
 5. Privacy: no personal column or value outside identites_*, leak columns
    absent from factures_features, rejects of identites_pii hold no PII,
    secure/ folder ignored by Git, no plaintext identity left over.
 6. identites_chiffrees (if present): structure, 1:1 with assures, Fernet
    token shape. It never decrypts anything and never needs IDENTITY_KEY.
 7. With --raw-report: the cleaned CSVs are the profiled ones, and the
    profile's estimated rejects are compared with what clean.py rejected.
Output: <out>/_quality_report.json.

Exit codes: 0 = OK, 1 = blocking failure (or any warning with --strict).
In raw mode a data problem is a finding (warning), not a failure: only a
missing required file/column, or a code present in both referentials (clean.py
would abort), is blocking.

Usage
-----
python quality_check.py --mode raw --src dataset [--labels labels] \
        [--report data/quality/profile_raw.json] [--today 2026-10-14]
python clean.py --src dataset --out data/clean --rejets data/rejets \
        --secure data/secure [--labels labels]
python quality_check.py --mode clean --out data/clean --rejets data/rejets \
        --secure data/secure [--src dataset] [--labels labels] \
        [--raw-report data/quality/profile_raw.json] [--strict]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

try:  # only needed by --mode clean (Parquet files)
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # raw mode works without it
    pa = pq = None

# --------------------------------------------------------------------------- #
# Contract (re-declared on purpose: independent from clean.py)
# --------------------------------------------------------------------------- #
LEAK = {"montant_pris_en_charge", "reste_a_charge", "statut", "motif_rejet",
        "date_traitement"}
PII_COLS = {"nom", "prenoms", "date_naissance", "telephone", "email",
            "adresse", "numero_cin"}
ID_RX = {
    "assure_id": r"ASS-[0-9A-F]{10}", "assure_principal_id": r"ASS-[0-9A-F]{10}",
    "contrat_id": r"CTR-\d{6}", "prestataire_id": r"PRE-\d{5}",
    "prescripteur_id": r"PRE-\d{5}", "facture_id": r"FAC-\d{8}",
    "ordonnance_id": r"ORD-\d{7}", "ligne_id": r"FAC-\d{8}-L\d{2}",
    "souscripteur_id": r"SOU-(\d{4}|IND)",
}
TEL_RX = r"0\d{2} \d{2} \d{3} \d{2}"
EXTRA_RX = {  # format rules beyond identifiers, by (table, column)
    ("prestataires", "telephone"): TEL_RX,
    ("prestataires", "nif"): r"\d{10}",
    ("prestataires", "jours_ouverture"): r"[1-7]{1,7}",
    ("prestataires", "numero_onm"): r"ONM-\d{5}",
    ("identites_pii", "telephone"): TEL_RX,
    ("identites_pii", "email"): r"[^@\s]+@[^@\s]+\.[^@\s]+",
    ("identites_pii", "numero_cin"): r"\d{12}",
}
PII_VALUE_RX = {
    "email": r"[^@\s]+@[^@\s]+\.[^@\s]+",
    "phone": r"\b0\d{2} \d{2} \d{3} \d{2}\b",
    "cin_12_digits": r"\b\d{12}\b",
}
SCAN_TABLES = ["assures", "contrats", "ordonnances", "ordonnance_lignes",
               "factures", "factures_features", "lignes_facture",
               "labels_fraude", "labels_prestataires", "garanties",
               "referentiel_actes", "referentiel_medicaments", "ref_codes"]

FORMULES = ["ESSENTIEL", "CONFORT", "PREMIUM"]
CATEGORIES = ["CONSULTATION", "PHARMACIE", "LABORATOIRE", "IMAGERIE",
              "DENTAIRE", "KINESITHERAPIE", "MATERNITE", "OPTIQUE",
              "HOSPITALISATION"]
MOTIFS_REJET = ["CONTROLE_MEDICAL", "PIECE_MANQUANTE_ATTESTATION",
                "ORDONNANCE_PERIMEE", "DELAI_DE_CARENCE",
                "HORS_PERIODE_COUVERTURE", "PLAFOND_ATTEINT",
                "ENTENTE_PREALABLE_ABSENTE", "PRODUIT_NON_REMBOURSABLE",
                "FACTURE_INVALIDE"]
FRAUD_TYPES = ["SURFACTURATION", "QUANTITE_EXCESSIVE", "UPCODING",
               "LIGNES_NON_PRESCRITES", "SUBSTITUTION_PRODUIT",
               "ORDONNANCE_EXPIREE", "SOINS_JOUR_FERME", "DOUBLON",
               "FRACTIONNEMENT", "SURCONSOMMATION", "ACTE_FICTIF",
               "INCOHERENCE_PROFIL", "PRESTATAIRE_RADIE", "HORS_COUVERTURE"]
INVALID_CODES = ["MONTANT_NEGATIF", "CODE_ACTE_INCONNU", "DATE_FUTURE",
                 "DATE_IMPOSSIBLE", "CHAMP_OBLIGATOIRE_MANQUANT",
                 "TOTAL_INCOHERENT", "PRESTATAIRE_INCONNU", "ASSURE_INCONNU",
                 "DEVISE_INVALIDE", "FACTURE_SANS_LIGNE"]
ENUMS = {  # allowed values, by (table, column)
    ("referentiel_actes", "sexe_requis"): ["F", "M"],
    ("garanties", "formule"): FORMULES,
    ("garanties", "categorie"): CATEGORIES,
    ("prestataires", "statut"): ["ACTIF", "RADIE"],
    ("prestataires", "niveau_tarif"): ["RAISONNABLE", "ELEVE", "TRES_ELEVE"],
    ("contrats", "formule"): FORMULES,
    ("contrats", "statut"): ["ACTIF", "RESILIE"],
    ("assures", "sexe"): ["M", "F"],
    ("assures", "rang_beneficiaire"): ["ASSURE_PRINCIPAL", "CONJOINT",
                                       "ENFANT"],
    ("identites_pii", "sexe"): ["M", "F"],
    ("ordonnances", "type_ordonnance"): ["MEDICAMENTS", "EXAMENS", "KINE",
                                         "OPTIQUE"],
    ("lignes_facture", "type_ligne"): ["ACTE", "MEDICAMENT"],
    ("factures", "type_facture"): CATEGORIES,
    ("factures", "canal_reception"): ["PAPIER_SCAN", "EMAIL",
                                      "PORTAIL_PRESTATAIRE"],
    ("factures", "mode_reglement"): ["TIERS_PAYANT", "REMBOURSEMENT_ASSURE"],
    ("factures", "statut"): ["PAYEE", "REJETEE", "EN_COURS"],
    ("factures", "motif_rejet"): MOTIFS_REJET,
    ("labels_fraude", "type_fraude"): FRAUD_TYPES,
    ("labels_fraude", "motif_invalidite"): INVALID_CODES,
}
PLACEHOLDERS = {"nan", "none", "null", "n/a", "#n/a", "na"}
TRUE_VALUES = {"1", "true", "t", "yes", "y", "oui", "o"}
FALSE_VALUES = {"0", "false", "f", "no", "n", "non"}
KIND_LABEL = {"str": "text", "arr": "text", "i16": "SMALLINT",
              "i64": "BIGINT", "bool": "BOOLEAN", "date": "DATE (AAAA-MM-JJ)",
              "time": "TIME (HH:MM)"}


def ranges(today: dt.date) -> dict:
    """(table, column) -> (min, max), same bounds as the cleaning step."""
    return {
        ("referentiel_actes", "tarif_min"): (0, None),
        ("referentiel_actes", "age_min"): (0, 120),
        ("referentiel_actes", "age_max"): (0, 120),
        ("referentiel_medicaments", "prix_reference"): (0, None),
        ("garanties", "taux_remboursement"): (0, 100),
        ("garanties", "plafond_annuel_categorie"): (0, None),
        ("garanties", "franchise_par_facture"): (0, None),
        ("garanties", "delai_carence_jours"): (0, None),
        ("garanties", "plafond_annuel_global"): (0, None),
        ("garanties", "prime_annuelle_par_beneficiaire"): (0, None),
        ("prestataires", "heure_ouverture"): (0, 24),
        ("prestataires", "heure_fermeture"): (0, 24),
        ("prestataires", "note_qualite"): (1, 5),
        ("grille_tarifaire", "tarif_pratique"): (0, None),
        ("contrats", "nb_beneficiaires"): (1, None),
        ("contrats", "prime_annuelle_totale"): (0, None),
        ("assures", "annee_naissance"): (1900, today.year),
        ("ordonnances", "validite_jours"): (1, None),
        ("ordonnance_lignes", "num_ligne"): (1, None),
        ("ordonnance_lignes", "quantite"): (1, None),
        ("lignes_facture", "num_ligne"): (1, None),
        ("lignes_facture", "quantite"): (1, None),
        ("lignes_facture", "prix_unitaire"): (0, None),
        ("lignes_facture", "montant"): (0, None),
        ("factures", "montant_total"): (0, None),
        ("factures", "montant_eligible"): (0, None),
        ("factures", "montant_pris_en_charge"): (0, None),
        ("factures", "reste_a_charge"): (0, None),
    }


def _kind_ok():
    return {
        "str": lambda t: pa.types.is_string(t) or pa.types.is_large_string(t),
        "i16": pa.types.is_int16, "i64": pa.types.is_int64,
        "bool": pa.types.is_boolean, "date": pa.types.is_date32,
        "time": pa.types.is_time32,
        "arr": lambda t: pa.types.is_list(t)
        and pa.types.is_string(t.value_type),
    }


def S(txt: str) -> tuple[dict, set]:
    """'a:str b:i64?' -> ({'a': 'str', 'b': 'i64'}, {'b'})  (? = nullable)."""
    cols, nullable = {}, set()
    for tok in txt.split():
        name, kind = tok.split(":")
        if kind.endswith("?"):
            kind = kind[:-1]
            nullable.add(name)
        cols[name] = kind
    return cols, nullable


def tbl(folder, pk, spec, rows=None, optional=False):
    cols, nullable = S(spec)
    return dict(dir=folder, pk=pk, cols=cols, nullable=nullable, rows=rows,
                optional=optional)


FACT = ("facture_id:str numero_facture:str prestataire_id:str assure_id:str "
        "contrat_id:str ordonnance_id:str? type_facture:str date_soin:date "
        "heure_soin:time date_facture:date date_reception:date "
        "canal_reception:str mode_reglement:str attestation_pec_id:str? "
        "entente_prealable_id:str? devise:str nb_lignes:i16 montant_total:i64 "
        "montant_eligible:i64 montant_pris_en_charge:i64 reste_a_charge:i64 "
        "statut:str motif_rejet:str? date_traitement:date?")
FACT_FEAT = " ".join(t for t in FACT.split() if t.split(":")[0] not in LEAK)

TABLES = {
    "referentiel_actes": tbl("out", ["code_acte"],
        "code_acte:str libelle:str categorie:str specialite:str? "
        "tarif_min:i64 tarif_reference:i64 tarif_max:i64 "
        "ordonnance_requise:bool entente_prealable:bool sexe_requis:str? "
        "age_min:i16 age_max:i16", 58),
    "referentiel_medicaments": tbl("out", ["code_medicament"],
        "code_medicament:str libelle:str prix_reference:i64 remboursable:bool "
        "classe:str chronique:bool", 28),
    "ref_codes": tbl("out", ["code"],
        "code:str type_ligne:str libelle:str tarif_min:i64? "
        "tarif_reference:i64 tarif_max:i64?", 86),
    "garanties": tbl("out", ["formule", "categorie"],
        "formule:str categorie:str taux_remboursement:i16 "
        "plafond_annuel_categorie:i64 franchise_par_facture:i64 "
        "delai_carence_jours:i16 plafond_annuel_global:i64 "
        "prime_annuelle_par_beneficiaire:i64 attestation_requise:bool "
        "actes_exclus:arr? pieces_justificatives:str", 27),
    "prestataires": tbl("out", ["prestataire_id"],
        "prestataire_id:str nom:str type_prestataire:str specialite:str? "
        "ville:str quartier:str adresse:str telephone:str numero_onm:str? "
        "nif:str horaires:str jours_ouverture:str heure_ouverture:i16 "
        "heure_fermeture:i16 niveau_tarif:str note_qualite:i16 "
        "conventionne:bool date_conventionnement:date "
        "date_fin_convention:date? statut:str", 251),
    "grille_tarifaire": tbl("out", ["prestataire_id", "code"],
        "prestataire_id:str code:str tarif_pratique:i64", 3242),
    "contrats": tbl("out", ["contrat_id"],
        "contrat_id:str numero_police:str souscripteur_id:str "
        "souscripteur_nom:str secteur:str formule:str date_effet_police:date "
        "date_effet:date date_fin:date? statut:str nb_beneficiaires:i16 "
        "prime_annuelle_totale:i64", 1500),
    "assures": tbl("out", ["assure_id"],
        "assure_id:str contrat_id:str rang_beneficiaire:str "
        "assure_principal_id:str sexe:str annee_naissance:i16 ville:str "
        "date_adhesion:date date_radiation:date?", 4459),
    "ordonnances": tbl("out", ["ordonnance_id"],
        "ordonnance_id:str assure_id:str prescripteur_id:str "
        "date_prescription:date validite_jours:i16 type_ordonnance:str "
        "diagnostic:str? renseignement_clinique:str?", 47804),
    "ordonnance_lignes": tbl("out", ["ordonnance_id", "num_ligne"],
        "ordonnance_id:str num_ligne:i16 code:str libelle:str quantite:i16 "
        "posologie:str?", 82175),
    "factures": tbl("out", ["facture_id"], FACT, 84593),
    "factures_features": tbl("out", ["facture_id"], FACT_FEAT, 84593),
    "lignes_facture": tbl("out", ["ligne_id"],
        "ligne_id:str facture_id:str num_ligne:i16 code:str type_ligne:str "
        "libelle:str quantite:i16 prix_unitaire:i64 montant:i64", 123270),
    "labels_fraude": tbl("out", ["facture_id"],
        "facture_id:str est_fraude:bool type_fraude:str? detail_fraude:str? "
        "est_valide:bool motif_invalidite:str?", optional=True),
    "labels_prestataires": tbl("out", ["prestataire_id"],
        "prestataire_id:str est_suspect:bool", optional=True),
    # personal data, only in <secure>/ ; sexe/ville are dropped by clean.py
    "identites_pii": tbl("secure", ["assure_id"],
        "assure_id:str nom:str prenoms:str date_naissance:date "
        "telephone:str? email:str? adresse:str numero_cin:str?", 4459,
        optional=True),
}
# columns present in the SOURCE csv but dropped by clean.py
RAW_EXTRA = {"identites_pii": {"sexe": "str", "ville": "str"}}
NOT_A_SOURCE = ("ref_codes", "factures_features")  # built by clean.py

FK = [
    ("grille_tarifaire", "prestataire_id", "prestataires", "prestataire_id"),
    ("grille_tarifaire", "code", "ref_codes", "code"),
    ("contrats", "formule", "garanties", "formule"),
    ("assures", "contrat_id", "contrats", "contrat_id"),
    ("assures", "assure_principal_id", "assures", "assure_id"),
    ("ordonnances", "assure_id", "assures", "assure_id"),
    ("ordonnances", "prescripteur_id", "prestataires", "prestataire_id"),
    ("ordonnance_lignes", "ordonnance_id", "ordonnances", "ordonnance_id"),
    ("ordonnance_lignes", "code", "ref_codes", "code"),
    ("factures", "prestataire_id", "prestataires", "prestataire_id"),
    ("factures", "assure_id", "assures", "assure_id"),
    ("factures", "contrat_id", "contrats", "contrat_id"),
    ("factures", "ordonnance_id", "ordonnances", "ordonnance_id"),
    ("factures", "type_facture", "garanties", "categorie"),
    ("lignes_facture", "facture_id", "factures", "facture_id"),
    ("lignes_facture", "code", "ref_codes", "code"),
    ("factures_features", "facture_id", "factures", "facture_id"),
    ("labels_fraude", "facture_id", "factures", "facture_id"),
    ("labels_prestataires", "prestataire_id", "prestataires",
     "prestataire_id"),
    ("identites_pii", "assure_id", "assures", "assure_id"),
]

# Proposed decisions (raw mode). They describe what clean.py does today.
D_REJECT = "REJECT (row removed, written to rejets/)"
D_DROP = "DROP (technical duplicate, first row kept)"
D_FIX = "FIX (clean.py strips them)"
D_PLACEHOLDER = ("REVIEW (clean.py keeps them as text; "
                 "decide whether they mean NULL)")
D_KEEP = "KEEP (fraud signal: never fix, never dedupe)"
D_SOFT = "REVIEW (clean.py does not reject on this)"
D_PII = "REVIEW (personal data must live only in identites_chiffrees)"


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self, verbose=True):
        self.results: list[dict] = []
        self.stats: dict = {}
        self.findings: list[dict] = []
        self.ok_count = 0          # passed raw-mode checks (not stored)
        self.verbose = verbose

    def section(self, title):
        print(f"\n== {title}")

    def add(self, sev, name, ok, detail=""):
        self.results.append(dict(severity=sev, check=name, ok=bool(ok),
                                 detail=detail))
        if ok and not self.verbose:
            return
        tag = "OK  " if ok else ("FAIL" if sev == "BLOQUANT" else "WARN")
        print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))

    def info(self, text):
        self.results.append(dict(severity="INFO", check=text, ok=True,
                                 detail=""))
        print(f"  [INFO] {text}")

    def finding(self, table, problem, n, decision, rows=None):
        """Raw mode: a data problem in the sources + the proposed decision."""
        n = int(n)
        if n == 0:
            self.ok_count += 1
            if self.verbose:
                print(f"  [OK  ] {table}: {problem}")
            return
        self.add("AVERT", f"{table}: {problem}", False,
                 f"{n} rows -> {decision}")
        self.findings.append(dict(table=table, problem=problem, rows=n,
                                  decision=decision,
                                  example_csv_lines=rows or []))

    def signal(self, table, problem, n):
        """Raw mode: a fraud signal that must be present and kept."""
        n = int(n)
        if n:
            self.info(f"{table}: {problem}: {n} -> {D_KEEP}")
            self.findings.append(dict(table=table, problem=problem, rows=n,
                                      decision=D_KEEP, example_csv_lines=[]))
        else:
            self.add("AVERT", f"{table}: {problem}", False,
                     "none found in the source")

    def failed(self, sev):
        return [r for r in self.results if r["severity"] == sev
                and not r["ok"]]

    def total_checks(self):
        return sum(1 for r in self.results if r["severity"] != "INFO") \
            + self.ok_count


def D(s):
    return pd.to_datetime(s, errors="coerce")


def samples(mask, k=5):
    """CSV line numbers (header = line 1) of the first offending rows."""
    m = pd.Series(mask).fillna(False).astype(bool)
    return [int(i) + 2 for i in m[m].index[:k]]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pii_value_hits(df, str_cols) -> dict:
    """Count personal-looking values (email, phone, 12-digit CIN)."""
    hits = {}
    for col in str_cols:
        s = df[col].astype("string")
        for label, rx in PII_VALUE_RX.items():
            n = int(s.str.contains(rx, regex=True, na=False).sum())
            if n:
                hits[f"{col}:{label}"] = n
    return hits


# =========================================================================== #
# RAW MODE
# =========================================================================== #
def parse_col(s: pd.Series, kind: str):
    """(typed series, mask of values present but not parsable)."""
    s = s.astype("string").str.strip()
    s = s.mask((s == "").fillna(False))
    present = s.notna()
    no_bad = pd.Series(False, index=s.index)
    if kind in ("str", "arr"):
        return s, no_bad
    if kind in ("i16", "i64"):
        num = pd.to_numeric(s, errors="coerce")
        limit = 32767 if kind == "i16" else 2 ** 53
        ok = (num.notna() & (num % 1 == 0) & (num.abs() <= limit))
        ok = ok.fillna(False).astype(bool)
        return num.where(ok).astype("Int64"), present & ~ok
    if kind == "bool":
        low = s.str.lower()
        is_t, is_f = low.isin(TRUE_VALUES), low.isin(FALSE_VALUES)
        val = pd.Series(pd.NA, index=s.index, dtype="boolean")
        val[is_t] = True
        val[is_f] = False
        return val, present & ~(is_t | is_f)
    if kind == "date":
        fmt = s.str.fullmatch(r"\d{4}-\d{2}-\d{2}").fillna(False).astype(bool)
        d = pd.to_datetime(s.where(fmt), format="%Y-%m-%d", errors="coerce")
        return d, present & d.isna()
    if kind == "time":
        ok = s.str.fullmatch(r"([01]\d|2[0-3]):[0-5]\d").fillna(False)
        ok = ok.astype(bool)
        return s.where(ok), present & ~ok
    raise ValueError(kind)


def raw_table(name, t, path, rep, today, rejects, stats):
    """Profile one source CSV. Returns (stripped text frame, typed frame)."""
    sens = name == "identites_pii"
    raw = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""],
                      encoding="utf-8-sig")
    cols = dict(t["cols"])
    cols.update(RAW_EXTRA.get(name, {}))
    missing = [c for c in cols if c not in raw.columns]
    extra = [c for c in raw.columns if c not in cols]
    rep.add("BLOQUANT", f"{name}: all contract columns present", not missing,
            f"missing={missing} (clean.py would abort)" if missing else "")
    if extra:
        rep.add("AVERT", f"{name}: no unexpected column", False,
                f"extra={extra} (clean.py ignores them)")
        leaked = PII_COLS & set(extra)
        if name != "prestataires":
            rep.finding(name, f"personal column(s) {sorted(leaked)} in a "
                        "non-identity file", len(raw) if leaked else 0,
                        D_PII)
    if missing:
        return None
    n_in = len(raw)
    raw = raw[list(cols)]
    if t["rows"] is not None:
        rep.add("AVERT", f"{name}: row count = contract volume",
                n_in == t["rows"],
                f"{n_in} rows, contract says {t['rows']}"
                if n_in != t["rows"] else "")

    exact = raw.duplicated(keep="first")
    rep.finding(name, "exact duplicate rows", exact.sum(), D_DROP,
                samples(exact))
    raw = raw[~exact]

    st = pd.DataFrame(index=raw.index)   # stripped text
    ty = pd.DataFrame(index=raw.index)   # typed
    rej = pd.Series(False, index=raw.index)
    colstats = {}
    for col, kind in cols.items():
        o = raw[col]
        ws = o.notna() & (o != o.str.strip())
        rep.finding(name, f"{col}: leading/trailing spaces", ws.sum(),
                    D_FIX, samples(ws))
        typed, bad = parse_col(o, kind)
        s = o.astype("string").str.strip()
        s = s.mask((s == "").fillna(False))
        st[col], ty[col] = s, typed
        ph = s.str.lower().isin(PLACEHOLDERS).fillna(False).astype(bool)
        rep.finding(name, f"{col}: placeholder text ('NaN', 'None', "
                    "'null'...)", ph.sum(), D_PLACEHOLDER, samples(ph))
        rep.finding(name, f"{col}: not parsable as {KIND_LABEL[kind]}",
                    bad.sum(), D_REJECT, samples(bad))
        rej |= bad
        if col not in t["nullable"]:
            miss = typed.isna() & ~bad
            rep.finding(name, f"{col}: required value missing", miss.sum(),
                        D_REJECT, samples(miss))
            rej |= miss
        rx = EXTRA_RX.get((name, col)) or ID_RX.get(col)
        if rx:
            ok = st[col].str.fullmatch(rx).fillna(False).astype(bool)
            fmt = st[col].notna() & ~ok
            rep.finding(name, f"{col}: wrong format", fmt.sum(), D_REJECT,
                        samples(fmt))
            rej |= fmt
        allowed = ENUMS.get((name, col))
        if allowed is not None:
            forb = st[col].notna() & ~st[col].isin(allowed)
            rep.finding(name, f"{col}: value outside {allowed[:3]}...",
                        forb.sum(), D_REJECT, samples(forb))
            rej |= forb
        lo, hi = ranges(today).get((name, col), (None, None))
        if lo is not None or hi is not None:
            out = pd.Series(False, index=raw.index)
            if lo is not None:
                out |= (ty[col] < lo).fillna(False).astype(bool)
            if hi is not None:
                out |= (ty[col] > hi).fillna(False).astype(bool)
            rep.finding(name, f"{col}: outside [{lo}, {hi}]", out.sum(),
                        D_REJECT, samples(out))
            rej |= out
        info = dict(null_rate=round(float(typed.isna().mean()), 4)
                    if len(raw) else 0.0,
                    distinct=int(s.nunique(dropna=True)))
        if not sens and typed.notna().any():
            if kind in ("i16", "i64"):
                info.update(min=int(typed.min()), max=int(typed.max()))
            elif kind == "date":
                info.update(min=typed.min().date().isoformat(),
                            max=typed.max().date().isoformat())
        colstats[col] = info

    pk = t["pk"]
    dup = st.duplicated(pk, keep="first") & st[pk].notna().all(axis=1)
    rep.finding(name, f"primary key {pk} duplicated", dup.sum(),
                D_REJECT + ", first kept", samples(dup))
    rej |= dup
    rejects[name] = rej
    stats[name] = dict(rows_in=n_in, exact_duplicates=int(exact.sum()),
                       columns=colstats)
    return st, ty


def build_ref_codes_raw(T):
    a, m = T.get("referentiel_actes"), T.get("referentiel_medicaments")
    if a is None or m is None:
        return None
    acts = pd.DataFrame({"code": a.code_acte, "type_ligne": "ACTE",
                         "libelle": a.libelle, "tarif_min": a.tarif_min,
                         "tarif_reference": a.tarif_reference,
                         "tarif_max": a.tarif_max})
    meds = pd.DataFrame({"code": m.code_medicament, "type_ligne": "MEDICAMENT",
                         "libelle": m.libelle,
                         "tarif_min": pd.array([pd.NA] * len(m),
                                               dtype="Int64"),
                         "tarif_reference": m.prix_reference,
                         "tarif_max": pd.array([pd.NA] * len(m),
                                               dtype="Int64")})
    ref = pd.concat([acts, meds], ignore_index=True)
    return ref.dropna(subset=["code"]).drop_duplicates("code")


def run_raw(args, rep, today):
    src = Path(args.src)
    labels = Path(args.labels) if args.labels else None
    SS, TT, rejects, stats, inputs = {}, {}, {}, {}, {}

    rep.section("1. Source files, columns, volumes, per-column profile")
    for name, t in TABLES.items():
        if name in NOT_A_SOURCE:
            continue
        is_label = name.startswith("labels_")
        base = labels if is_label else src
        path = base / f"{name}.csv" if base else None
        if path is None or not path.exists():
            if is_label:
                rep.info(f"{name}: not provided (labels are optional)")
            else:
                rep.add("BLOQUANT", f"{name}: source file present", False,
                        str(path))
            continue
        inputs[path.name] = sha256_file(path)
        res = raw_table(name, t, path, rep, today, rejects, stats)
        if res is not None:
            SS[name], TT[name] = res

    both = None
    if "referentiel_actes" in SS and "referentiel_medicaments" in SS:
        both = (set(SS["referentiel_actes"].code_acte.dropna())
                & set(SS["referentiel_medicaments"].code_medicament.dropna()))
        rep.add("BLOQUANT", "no code in both referentials (clean.py aborts)",
                not both, f"{sorted(both)[:5]}" if both else "")
    ref = build_ref_codes_raw(TT)
    ref_keys = set(ref.code) if ref is not None else None

    rep.section("2. Foreign keys")
    for child, col, parent, pcol in FK:
        if child in NOT_A_SOURCE or child not in SS:
            continue
        if parent == "ref_codes":
            keys = ref_keys
        elif parent in SS:
            keys = set(SS[parent][pcol].dropna())
        else:
            keys = None
        if keys is None:
            continue
        mask = SS[child][col].notna() & ~SS[child][col].isin(keys)
        rep.finding(child, f"{col} not found in {parent}.{pcol}", mask.sum(),
                    D_REJECT + " (orphan key)", samples(mask))
        rejects[child] |= mask
    if "factures" in SS and "lignes_facture" in SS:
        with_lines = set(SS["lignes_facture"].facture_id.dropna())
        mask = ~SS["factures"].facture_id.isin(with_lines)
        rep.finding("factures", "invoice without any line (FACTURE_SANS_LIGNE)",
                    mask.sum(), D_REJECT, samples(mask))
        rejects["factures"] |= mask
    rep.info("clean.py also rejects children of rejected parents (cascade); "
             "the manifest after cleaning shows the real totals")

    # typed frames, one row per primary key, for the contract rules
    T = {}
    for name, df in TT.items():
        T[name] = df.drop_duplicates(subset=TABLES[name]["pk"])
    if ref is not None:
        T["ref_codes"] = ref
    # check_rules(T, today, rep, raw=True)

    # **Check rules and integrate rule-based rejects**
    rule_masks = check_rules(T, today, rep, raw=True)  # Get rule-based masks
    for table, mask in rule_masks.items():
        rejects[table] |= mask  # Combine rule-based rejects with existing rejects


    rep.section("4. Fraud signals that must be kept")
    if "factures" in SS:
        f = SS["factures"]
        rep.signal("factures", "numero_facture repeated (DOUBLON / "
                   "FRACTIONNEMENT)", f.numero_facture.dropna().duplicated()
                   .sum())
        rep.signal("factures", "ordonnance_id re-used (SURCONSOMMATION)",
                   f.ordonnance_id.dropna().duplicated().sum())
    if "lignes_facture" in T and ref is not None:
        l = T["lignes_facture"]
        tmax = l.code.map(ref.set_index("code").tarif_max)
        rep.signal("lignes_facture", "price above tarif_max (SURFACTURATION)",
                   (tmax.notna() & (l.prix_unitaire > tmax)).sum())

    rep.section("5. Privacy")
    for name, df in SS.items():
        if name in ("identites_pii", "prestataires"):
            continue
        strcols = [c for c, k in TABLES[name]["cols"].items() if k == "str"]
        hits = pii_value_hits(df, strcols)
        rep.finding(name, f"personal-looking values in text columns {hits}",
                    sum(hits.values()), D_PII)

    rep.section("6. Estimated rejects (row-level checks only)")
    est = {}
    for name, rej in rejects.items():
        n = int(rej.sum())
        n_rows = len(rej)
        est[name] = n
        ratio = n / n_rows if n_rows else 0
        rep.add("AVERT", f"{name}: estimated reject ratio <= "
                f"{args.max_reject_ratio:.0%}", ratio <= args.max_reject_ratio,
                f"{n}/{n_rows} = {ratio:.2%}")
    return dict(inputs=inputs, tables=stats, estimated_rejects=est)


def write_markdown(path: Path, rep: Report, state: dict, today: str):
    L = ["# Raw data profile (before clean.py)", "",
         f"Reference date: {today}. Counts and CSV line numbers only, no "
         "personal values.", "",
         "## Tables", "",
         "| Table | Rows read | Exact duplicates | Estimated rejects |",
         "|---|---:|---:|---:|"]
    for name, s in state["tables"].items():
        L.append(f"| `{name}` | {s['rows_in']} | {s['exact_duplicates']} | "
                 f"{state['estimated_rejects'].get(name, 0)} |")
    L += ["", "## Findings and proposed decisions", "",
          "| Table | Finding | Rows | Example CSV lines | Proposed decision "
          "| Team decision |", "|---|---|---:|---|---|---|"]
    for f in sorted(rep.findings, key=lambda x: (x["table"], x["decision"],
                                                 x["problem"])):
        ex = ", ".join(map(str, f["example_csv_lines"])) or "-"
        L.append(f"| `{f['table']}` | {f['problem']} | {f['rows']} | {ex} | "
                 f"{f['decision']} | |")
    if not rep.findings:
        L.append("| - | no finding | 0 | - | - | |")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# =========================================================================== #
# CLEAN MODE (Parquet files written by clean.py)
# =========================================================================== #
def load_tables(out: Path, secure: Path, rep: Report) -> dict:
    rep.section("1. Files, columns, types, NULLs, primary keys, volumes")
    kind_ok = _kind_ok()
    data: dict[str, pd.DataFrame] = {}
    for name, t in TABLES.items():
        path = (secure if t["dir"] == "secure" else out) / f"{name}.parquet"
        if not path.exists():
            if t["optional"]:
                rep.info(f"{name}: file absent (optional)")
            else:
                rep.add("BLOQUANT", f"{name}: file present", False, str(path))
            continue
        table = pq.read_table(path)
        df = table.to_pandas()
        data[name] = df
        cols, nullable = t["cols"], t["nullable"]
        got = table.schema.names
        missing = [c for c in cols if c not in got]
        extra = [c for c in got if c not in cols]
        rep.add("BLOQUANT", f"{name}: columns match contract",
                not missing and not extra,
                f"missing={missing} extra={extra}" if missing or extra
                else "")
        bad_t = [c for c in cols if c in got and
                 not kind_ok[cols[c]](table.schema.field(c).type)]
        rep.add("BLOQUANT", f"{name}: Arrow types match contract", not bad_t,
                str(bad_t) if bad_t else "")
        nulls = {c: int(df[c].isna().sum()) for c in cols
                 if c in got and c not in nullable and df[c].isna().any()}
        rep.add("BLOQUANT", f"{name}: no NULL in required columns", not nulls,
                str(nulls) if nulls else "")
        pk = [c for c in t["pk"] if c in got]
        dup = int(df.duplicated(pk).sum()) if pk else 0
        rep.add("BLOQUANT", f"{name}: primary key {t['pk']} unique", dup == 0,
                f"{dup} duplicates" if dup else "")
        if t["rows"] is not None:
            rep.add("AVERT", f"{name}: row count = contract volume",
                    len(df) == t["rows"],
                    f"{len(df)} rows, contract says {t['rows']}"
                    if len(df) != t["rows"] else "")
        rep.stats[name] = dict(
            rows=int(len(df)),
            null_rate={c: round(float(df[c].isna().mean()), 4)
                       for c in got if df[c].isna().any()})
    return data


def check_manifest(args, out, rejets, secure, data, rep):
    rep.section("2. Manifest, rejects, traceability")
    mpath = out / "_manifest.json"
    if not mpath.exists():
        rep.add("BLOQUANT", "_manifest.json present", False, str(mpath))
        return
    man = json.loads(mpath.read_text(encoding="utf-8"))
    for name, m in man.get("tables", {}).items():
        if "rows_in" not in m:
            rep.info(f"{name}: {m.get('status', 'no counts')}")
            continue
        kept = m["rows_in"] - m["exact_duplicates_dropped"]
        rep.add("BLOQUANT", f"{name}: rows_in - duplicates = out + rejected",
                kept == m["rows_out"] + m["rows_rejected"],
                f"{kept} vs {m['rows_out']} + {m['rows_rejected']}")
        if name in data:
            rep.add("BLOQUANT", f"{name}: Parquet rows = manifest rows_out",
                    len(data[name]) == m["rows_out"],
                    f"{len(data[name])} vs {m['rows_out']}")
        rpath = rejets / f"{name}.csv"
        if m["rows_rejected"] > 0:
            n = len(pd.read_csv(rpath, dtype=str, usecols=[0])) \
                if rpath.exists() else -1
            rep.add("BLOQUANT", f"{name}: rejects file = manifest count",
                    n == m["rows_rejected"], f"{n} vs {m['rows_rejected']}")
            rep.info(f"{name}: reject reasons {m['reject_reasons']}")
        else:
            rep.add("AVERT", f"{name}: no stale rejects file",
                    not rpath.exists())
        ratio = m["rows_rejected"] / m["rows_in"] if m["rows_in"] else 0
        rep.add("AVERT", f"{name}: reject ratio <= {args.max_reject_ratio:.0%}",
                ratio <= args.max_reject_ratio, f"{ratio:.2%}")
        for motif, n in m.get("warnings", {}).items():
            rep.add("AVERT", f"{name}: clean.py warning {motif}", False,
                    f"{n} rows")
    if args.src or args.labels:
        for fname, sha in man.get("inputs", {}).items():
            for base in (args.src, args.labels):
                p = Path(base) / fname if base else None
                if p and p.exists():
                    rep.add("AVERT", f"{fname}: source SHA-256 unchanged",
                            sha256_file(p) == sha)
                    break
            else:
                rep.info(f"{fname}: source not found, hash not re-checked")


def check_formats(data, rep):
    rep.section("3. Identifier formats and foreign keys")
    for name, df in data.items():
        bad = {}
        for col, rx in ID_RX.items():
            if col in df:
                s = df[col].dropna().astype(str)
                n = int((~s.str.fullmatch(rx)).sum())
                if n:
                    bad[col] = n
        if name == "prestataires":
            for col, rx in (("nif", r"\d{10}"),
                            ("jours_ouverture", r"[1-7]{1,7}")):
                n = int((~df[col].dropna().astype(str).str.fullmatch(rx)).sum())
                if n:
                    bad[col] = n
        if name == "identites_pii":
            s = df["numero_cin"].dropna().astype(str)
            n = int((~s.str.fullmatch(r"\d{12}")).sum())
            if n:
                bad["numero_cin"] = n
        rep.add("BLOQUANT", f"{name}: identifier formats", not bad,
                str(bad) if bad else "")
    for child, col, parent, pcol in FK:
        if child not in data or parent not in data:
            continue
        keys = set(data[parent][pcol].dropna())
        orphans = int((~data[child][col].dropna().isin(keys)).sum())
        rep.add("BLOQUANT", f"FK {child}.{col} -> {parent}.{pcol}",
                orphans == 0, f"{orphans} orphans" if orphans else "")
    if "factures" in data and "lignes_facture" in data:
        n = int((~data["factures"].facture_id
                 .isin(set(data["lignes_facture"].facture_id))).sum())
        rep.add("BLOQUANT", "every invoice has at least one line "
                "(FACTURE_SANS_LIGNE)", n == 0, f"{n} invoices" if n else "")


# --------------------------------------------------------------------------- #
# Contract section 9.2 rules (shared by both modes)
# --------------------------------------------------------------------------- #
def build_rules(today: dt.date):
    ts = pd.Timestamp(today)

    def r_tarifs(T):
        a = T["referentiel_actes"]
        return ((a.tarif_min > a.tarif_reference)
                | (a.tarif_reference > a.tarif_max))

    def r_contrat_statut(T):
        c = T["contrats"]
        return ((c.statut == "RESILIE") != c.date_fin.notna())

    def r_contrat_dates(T):
        c = T["contrats"]
        return (D(c.date_fin) < D(c.date_effet))

    def r_radiation(T):
        a = T["assures"]
        return (D(a.date_radiation) < D(a.date_adhesion))

    def r_principal(T):
        a, c = T["assures"], T["contrats"]
        p = a[a.rang_beneficiaire == "ASSURE_PRINCIPAL"] \
            .groupby("contrat_id").size()
        # contracts with != 1 principal, or no principal at all
        bad_contrats = set(p[p != 1].index) | (set(c.contrat_id) - set(p.index))
        # return a mask on contrats (the table we want to flag)
        return c.contrat_id.isin(bad_contrats)

    def r_effet(T):
        a, c = T["assures"], T["contrats"]
        p = (a[a.rang_beneficiaire == "ASSURE_PRINCIPAL"]
             .drop_duplicates("contrat_id")
             .set_index("contrat_id").date_adhesion)
        x, y = D(c.contrat_id.map(p)), D(c.date_effet)
        return ((x != y) & x.notna() & y.notna())

    def r_diag(T):
        o = T["ordonnances"]
        return (o.diagnostic.isna() != o.renseignement_clinique.isna())

    def r_dates_fact(T):
        f = T["factures"]
        return ((D(f.date_soin) > D(f.date_facture))
                | (D(f.date_facture) > D(f.date_reception)))

    def r_future(T):
        return (D(T["factures"].date_soin) > ts)

    def r_devise(T):
        f = T["factures"]
        return (f.devise.notna() & (f.devise != "MGA"))

    def r_eligible(T):
        f = T["factures"]
        return (f.montant_eligible > f.montant_total)

    def r_total(T):
        f, l = T["factures"], T["lignes_facture"]
        m = f.facture_id.map(l.groupby("facture_id")["montant"].sum())
        return (m.notna() & (m != f.montant_total))

    def r_nb(T):
        f, l = T["factures"], T["lignes_facture"]
        m = f.facture_id.map(l.groupby("facture_id").size())
        return (m.notna() & (m != f.nb_lignes))

    def r_ligne_montant(T):
        l = T["lignes_facture"]
        return ((l.montant != l.quantite * l.prix_unitaire)
                | (l.quantite <= 0) | (l.prix_unitaire < 0))

    def r_type_ligne(T):
        l, r = T["lignes_facture"], T["ref_codes"]
        m = l.code.map(r.set_index("code").type_ligne)
        return (m.notna() & (m != l.type_ligne))

    def r_reste(T):
        f = T["factures"]
        return (f.montant_pris_en_charge + f.reste_a_charge
                != f.montant_total)

    def r_en_cours(T):
        f = T["factures"]
        return ((f.statut == "EN_COURS") != f.date_traitement.isna())

    def r_year(T):
        a, i = T["assures"], T["identites_pii"]
        y = D(i.date_naissance).dt.year
        return (i.assure_id.map(a.set_index("assure_id").annee_naissance)
                != y)

    return [
        ("BLOQUANT", "9.2 #2 tarif_min <= tarif_reference <= tarif_max",
         ["referentiel_actes"], r_tarifs),
        ("BLOQUANT", "9.2 #4 contrat RESILIE <=> date_fin renseignee",
         ["contrats"], r_contrat_statut),
        ("BLOQUANT", "9.2 #3 contrat date_fin >= date_effet", ["contrats"],
         r_contrat_dates),
        ("BLOQUANT", "9.2 #3 assure date_radiation >= date_adhesion",
         ["assures"], r_radiation),
        ("BLOQUANT", "each contract has exactly one ASSURE_PRINCIPAL",
         ["assures", "contrats"], r_principal),
        ("AVERT", "contrat.date_effet = date_adhesion of the principal",
         ["assures", "contrats"], r_effet),
        ("BLOQUANT", "ordonnance: diagnostic and renseignement_clinique "
         "null together", ["ordonnances"], r_diag),
        ("BLOQUANT", "facture: date_soin <= date_facture <= date_reception",
         ["factures"], r_dates_fact),
        ("BLOQUANT", "facture: no date_soin in the future (DATE_FUTURE)",
         ["factures"], r_future),
        ("BLOQUANT", "facture: devise = MGA (DEVISE_INVALIDE)", ["factures"],
         r_devise),
        ("BLOQUANT", "facture: montant_eligible <= montant_total",
         ["factures"], r_eligible),
        ("BLOQUANT", "facture: montant_total = sum(lignes.montant) "
         "(TOTAL_INCOHERENT)", ["factures", "lignes_facture"], r_total),
        ("BLOQUANT", "facture: nb_lignes = number of lines",
         ["factures", "lignes_facture"], r_nb),
        ("BLOQUANT", "ligne: montant = quantite x prix_unitaire, "
         "no negative (MONTANT_NEGATIF)", ["lignes_facture"], r_ligne_montant),
        ("AVERT", "ligne: type_ligne agrees with the referential",
         ["lignes_facture", "ref_codes"], r_type_ligne),
        ("AVERT", "facture: pris_en_charge + reste_a_charge = montant_total",
         ["factures"], r_reste),
        ("AVERT", "facture: EN_COURS <=> date_traitement null", ["factures"],
         r_en_cours),
        ("BLOQUANT", "9.2 #5 annee_naissance = year(date_naissance)",
         ["assures", "identites_pii"], r_year),
    ]

def check_rules(data, today, rep, raw=False):
    rep.section("3. Business rules (contract section 9.2)" if raw
                else "4. Business rules (contract section 9.2)")
    rule_masks = {}
    for sev, name, needs, fn in build_rules(today):
        if not all(n in data for n in needs):
            rep.info(f"{name}: skipped ({', '.join(needs)} not available)")
            continue
        try:
            mask = fn(data)
        except Exception as exc:
            rep.add("AVERT" if raw else "BLOQUANT",
                    f"{name}: could not be computed", False,
                    f"{type(exc).__name__}: {exc}")
            continue
        n = int(mask.sum())
        if raw:
            # decide which table this mask belongs to: use the first table
            # whose index length matches the mask length
            table = next((t for t in needs
                          if len(data[t]) == len(mask)), needs[0])
            rep.finding(table, name, n,
                        D_REJECT if sev == "BLOQUANT" else D_SOFT)
            if sev == "BLOQUANT":
                if table not in rule_masks:
                    rule_masks[table] = pd.Series(False,
                                                  index=data[table].index)
                rule_masks[table] |= mask.reindex(data[table].index,
                                                  fill_value=False)
        else:
            rep.add(sev, name, n == 0, f"{n} violations" if n else "")
    return rule_masks if raw else None

def check_signals(data, rep):
    rep.section("5. Fraud signals preserved (no over-cleaning)")
    if "factures" in data:
        f = data["factures"]
        n = int(f.numero_facture.duplicated().sum())
        rep.add("AVERT", "numero_facture repeated rows kept (DOUBLON / "
                "FRACTIONNEMENT)", n > 0,
                f"{n} extra rows (contract: ~553)")
        n = int(f.ordonnance_id.dropna().duplicated().sum())
        rep.add("AVERT", "ordonnance_id re-use kept (SURCONSOMMATION)", n > 0,
                f"{n} re-uses (contract: ~1430)")
    if "lignes_facture" in data and "ref_codes" in data:
        l, r = data["lignes_facture"], data["ref_codes"]
        tmax = l.code.map(r.set_index("code").tarif_max)
        n = int((tmax.notna() & (l.prix_unitaire > tmax)).sum())
        rep.add("AVERT", "prices above tarif_max kept (SURFACTURATION)",
                n > 0, f"{n} lines")

    # Cross-check with labels_fraude
    if "labels_fraude" in data:
        labels = data["labels_fraude"]
        if "factures" in data:
            factures = data["factures"]
            # Verify DOUBLON
            rep.add("AVERT", "DOUBLON signal matches labels",
                    (factures.numero_facture.duplicated().sum() ==
                     labels[labels.type_fraude == "DOUBLON"].shape[0]))
            # Verify SURCONSOMMATION
            rep.add("AVERT", "SURCONSOMMATION signal matches labels",
                    (factures.ordonnance_id.duplicated().sum() ==
                     labels[labels.type_fraude == "SURCONSOMMATION"].shape[0]))
        # Verify est_fraude = 1 <=> type_fraude is filled
        mismatch = labels[(labels.est_fraude == 1) != labels.type_fraude.notna()]
        rep.add("BLOQUANT", "est_fraude = 1 <=> type_fraude renseigné",
                mismatch.empty, f"{len(mismatch)} mismatch(es)")


def git_state(path: Path):
    """(ignored, tracked); (None, None) when git is unavailable."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(path)],
                           capture_output=True)
        if r.returncode in (0, 1):
            tracked = subprocess.run(["git", "ls-files", str(path.parent)],
                                     capture_output=True, text=True)
            return r.returncode == 0, bool(tracked.stdout.strip())
    except FileNotFoundError:
        pass
    return None, None


def check_privacy(data, out, rejets, secure, rep):
    rep.section("6. Privacy and leakage")
    for p in sorted(out.glob("*.parquet")):
        cols = set(pq.read_schema(p).names)
        leaked = PII_COLS & cols
        if p.stem not in ("prestataires",):  # business entities, fictional
            rep.add("BLOQUANT", f"{p.name}: no personal column", not leaked,
                    str(sorted(leaked)) if leaked else "")
    if "factures_features" in data:
        leak = LEAK & set(data["factures_features"].columns)
        rep.add("BLOQUANT", "factures_features: no leak column", not leak,
                str(sorted(leak)) if leak else "")
    for name in SCAN_TABLES:
        if name not in data:
            continue
        strcols = [c for c, k in TABLES[name]["cols"].items() if k == "str"]
        hits = pii_value_hits(data[name], strcols)
        rep.add("BLOQUANT", f"{name}: no personal-looking value in text "
                "columns", not hits, str(hits) if hits else "")
    rp = rejets / "identites_pii.csv"
    if rp.exists():
        cols = set(pd.read_csv(rp, nrows=0).columns)
        rep.add("BLOQUANT", "rejets/identites_pii.csv holds ids + reasons "
                "only", cols <= {"source_row", "assure_id", "motif"},
                str(sorted(cols)))
    ignored, tracked = git_state(secure / "identites_pii.parquet")
    if ignored is None:
        rep.info("git not available here: check .gitignore for data/secure "
                 "manually")
    else:
        rep.add("BLOQUANT", f"{secure}/ ignored by Git", ignored)
        rep.add("BLOQUANT", f"{secure}/ not tracked by Git", not tracked)

    enc = secure / "identites_chiffrees.parquet"
    if enc.exists():
        check_encrypted(enc, data, secure, rep)
    elif (secure / "identites_pii.parquet").exists():
        rep.info("identites_chiffrees.parquet not built yet "
                 "(run encrypt_identities.py)")


def check_encrypted(enc: Path, data, secure: Path, rep):
    t = pq.read_table(enc)
    df = t.to_pandas()
    want = {"assure_id", "donnees_chiffrees", "key_version", "created_at"}
    rep.add("BLOQUANT", "identites_chiffrees: columns match section 6.4",
            set(t.schema.names) == want, str(sorted(t.schema.names)))
    if not want <= set(t.schema.names):
        return
    ty = t.schema.field
    rep.add("BLOQUANT", "identites_chiffrees: types (binary, int16, "
            "timestamp tz)",
            (pa.types.is_binary(ty("donnees_chiffrees").type)
             or pa.types.is_large_binary(ty("donnees_chiffrees").type))
            and pa.types.is_int16(ty("key_version").type)
            and pa.types.is_timestamp(ty("created_at").type)
            and ty("created_at").type.tz is not None)
    rep.add("BLOQUANT", "identites_chiffrees: assure_id unique, no NULL",
            df.assure_id.notna().all() and df.assure_id.is_unique)
    rep.add("BLOQUANT", "identites_chiffrees: key_version and created_at "
            "filled", df.key_version.notna().all()
            and df.created_at.notna().all())
    tokens = df.donnees_chiffrees.dropna()
    ok = tokens.map(lambda b: bytes(b).startswith(b"gAAAA")).all() \
        and len(tokens) == len(df)
    rep.add("BLOQUANT", "identites_chiffrees: every payload looks like a "
            "Fernet token (not plaintext)", ok)
    if "assures" in data:
        ids, ref = set(df.assure_id), set(data["assures"].assure_id)
        rep.add("BLOQUANT", "identites_chiffrees: no identity without assure",
                ids <= ref, f"{len(ids - ref)} orphans" if ids - ref else "")
        rep.add("AVERT", "identites_chiffrees: every assure has an identity "
                "(1:1)", ref <= ids,
                f"{len(ref - ids)} without identity" if ref - ids else "")
    if (secure / "identites_pii.parquet").exists():
        rep.add("AVERT", "plaintext identites_pii.parquet deleted after "
                "encryption (--delete-source)", False)


def check_against_raw(args, out, rep):
    rep.section("7. Consistency with the raw profile")
    p = Path(args.raw_report)
    mpath = out / "_manifest.json"
    if not p.exists():
        rep.add("AVERT", "raw profile report found", False, str(p))
        return
    if not mpath.exists():
        return
    raw = json.loads(p.read_text(encoding="utf-8"))
    man = json.loads(mpath.read_text(encoding="utf-8"))
    for fname, sha in raw.get("inputs", {}).items():
        other = man.get("inputs", {}).get(fname)
        if other is None:
            rep.info(f"{fname}: profiled but not in the clean.py manifest")
            continue
        rep.add("AVERT", f"{fname}: cleaned file is the profiled file",
                other == sha)
    for name, est in raw.get("estimated_rejects", {}).items():
        m = man.get("tables", {}).get(name, {})
        if "rows_rejected" in m:
            rep.info(f"{name}: profile estimated {est} reject(s) "
                     f"(row-level checks), clean.py rejected "
                     f"{m['rows_rejected']}")


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--mode", choices=["raw", "clean"], default="clean",
                   help="raw = profile source CSVs before clean.py; "
                        "clean = audit clean.py outputs (default)")
    p.add_argument("--src", default=None,
                   help="source CSV folder (raw: default 'dataset'; clean: "
                        "optional, to re-check SHA-256)")
    p.add_argument("--labels", default=None,
                   help="labels folder (optional)")
    p.add_argument("--out", default="data/clean")
    p.add_argument("--rejets", default="data/rejets")
    p.add_argument("--secure", default="data/secure")
    p.add_argument("--raw-report", default=None,
                   help="clean mode: JSON written by --mode raw, to compare")
    p.add_argument("--today", default=dt.date.today().isoformat())
    p.add_argument("--max-reject-ratio", type=float, default=0.05)
    p.add_argument("--report", default=None,
                   help="raw: data/quality/profile_raw.json; "
                        "clean: <out>/_quality_report.json")
    p.add_argument("--verbose", action="store_true",
                   help="raw mode: also print the checks that passed")
    p.add_argument("--strict", action="store_true",
                   help="warnings also give exit code 1")
    args = p.parse_args(argv)
    today = dt.date.fromisoformat(args.today)
    raw_mode = args.mode == "raw"
    if raw_mode and args.src is None:
        args.src = "dataset"
    if not raw_mode and pq is None:
        sys.exit("ERROR: --mode clean needs pyarrow (pip install pyarrow)")

    rep = Report(verbose=args.verbose or not raw_mode)
    if raw_mode:
        print(f"quality_check.py --mode raw  src={args.src}  today={today}")
        state = run_raw(args, rep, today)
        report_path = Path(args.report) if args.report \
            else Path("data/quality/profile_raw.json")
    else:
        out, rejets, secure = (Path(args.out), Path(args.rejets),
                               Path(args.secure))
        print(f"quality_check.py --mode clean  out={out}  today={today}")
        data = load_tables(out, secure, rep)
        check_manifest(args, out, rejets, secure, data, rep)
        check_formats(data, rep)
        check_rules(data, today, rep)
        check_signals(data, rep)
        check_privacy(data, out, rejets, secure, rep)
        if args.raw_report:
            check_against_raw(args, out, rep)
        state = {}
        report_path = Path(args.report) if args.report \
            else out / "_quality_report.json"

    fails, warns = rep.failed("BLOQUANT"), rep.failed("AVERT")
    summary = dict(mode=args.mode, checks=rep.total_checks(),
                   blocking_failures=len(fails), warnings=len(warns),
                   today=args.today)
    payload = dict(summary=summary, results=rep.results, tables=rep.stats)
    if raw_mode:
        payload.update(
            inputs=state["inputs"], tables=state["tables"],
            estimated_rejects=state["estimated_rejects"],
            findings=rep.findings,
            decisions=dict(Counter(f["decision"].split(" ")[0]
                                   for f in rep.findings)))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                                      default=str), encoding="utf-8")
    if raw_mode:
        write_markdown(report_path.with_suffix(".md"), rep, state, args.today)

    print(f"\n{summary['checks']} checks: {len(fails)} blocking failure(s), "
          f"{len(warns)} warning(s). Report: {report_path}"
          + (f" (+ {report_path.with_suffix('.md').name})" if raw_mode
             else ""))
    for r in fails:
        print(f"  BLOCKING: {r['check']} {r['detail']}", file=sys.stderr)
    return 1 if fails or (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
