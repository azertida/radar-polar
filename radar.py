#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radar à téléfilms policiers du terroir
========================================
Télécharge les guides EPG d'epgshare01, filtre les chaînes ciblées,
détecte les séries "Meurtres à…", "Crime à…", "Mystères de…", etc.,
et produit un fichier radar.json prêt pour la PWA.

Usage :
    python3 radar.py              # mode normal
    python3 radar.py --inspect    # liste les chaînes disponibles dans les XML
    python3 radar.py --keep-xml   # garde les XML téléchargés pour debug
"""

import argparse
import gzip
import io
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================

# Sources XMLTV (epgshare01 met à jour quotidiennement)
SOURCES = {
    "FR1": "https://epgshare01.online/epgshare01/epg_ripper_FR1.xml.gz",
    "BE2": "https://epgshare01.online/epgshare01/epg_ripper_BE2.xml.gz",
}

# Chaînes recherchées : on matche sur le display-name (insensible à la casse).
# La liste de variantes permet de tolérer "France 2 HD", "RTBF La Une", etc.
TARGET_CHANNELS = {
    "TF1":      ["TF1"],
    "France 2": ["France 2", "France2"],
    "France 3": ["France 3", "France3"],
    "TV5Monde": ["TV5Monde", "TV5 Monde", "TV5MONDE"],
    "La Une":   ["La Une", "RTBF La Une", "LaUne"],
    "La Deux":  ["La Deux", "LaDeux"],
    "Tipik":    ["Tipik"],
}

# Mots-clés thématiques (ce qui définit le genre "polar du terroir").
# Insensibles à la casse. Un éventuel article "Le/La/Les" en tête du titre
# est ignoré automatiquement (donc "Les Mystères" matche aussi "Mystères").
TITLE_KEYWORDS = [
    "Meurtres", "Meurtre",
    "Crimes", "Crime",
    "Mystères", "Mystère",
    "Secrets", "Secret",
]

# Prépositions de lieu acceptées entre le mot-clé et le nom du lieu.
# Le mot QUI SUIT la préposition doit commencer par une majuscule
# (= nom propre, lieu), ce qui élimine "en série", "de l'amour", etc.
LOCATION_PREPOSITIONS = [
    "à", "au", "aux",
    "en", "dans",
    "de", "d'", "du", "des",
    "sur",
]

# Fenêtre temporelle : J à J+7
WINDOW_DAYS = 7

# Fichiers de sortie
OUTPUT_JSON = "radar.json"


# ============================================================
# UTILITAIRES
# ============================================================

def log(msg):
    """Affichage horodaté pour suivre l'exécution."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def download_xml(url):
    """Télécharge un fichier .xml.gz et renvoie son contenu décompressé (bytes)."""
    log(f"Téléchargement : {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "RadarTerroir/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        gz_data = response.read()
    log(f"  → {len(gz_data) // 1024} Ko téléchargés")
    xml_data = gzip.decompress(gz_data)
    log(f"  → {len(xml_data) // 1024} Ko décompressés")
    return xml_data


def parse_xmltv_date(date_str):
    """Parse une date XMLTV (ex: '20260603205500 +0200') en datetime aware."""
    # Format : YYYYMMDDHHMMSS +ZZZZ
    main, _, tz = date_str.strip().partition(" ")
    dt = datetime.strptime(main, "%Y%m%d%H%M%S")
    if tz:
        sign = 1 if tz[0] == "+" else -1
        hours = int(tz[1:3])
        minutes = int(tz[3:5])
        dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
    else:
        # Pas de TZ : on suppose UTC pour éviter les mauvaises surprises
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def find_matching_channels(root, targets):
    """
    Parcourt les <channel> du XMLTV et renvoie un dict :
        {channel_id_xmltv: nom_canonique}
    pour les chaînes qui correspondent à notre liste cible.
    """
    found = {}
    all_channels = []
    for ch in root.findall("channel"):
        ch_id = ch.get("id", "")
        names = [dn.text or "" for dn in ch.findall("display-name")]
        all_channels.append((ch_id, names))
        for canonical, variants in targets.items():
            for variant in variants:
                if any(variant.lower() == n.strip().lower() for n in names):
                    found[ch_id] = canonical
                    break
            if ch_id in found:
                break
    return found, all_channels


# ============================================================
# CŒUR DU TRAITEMENT
# ============================================================

def title_matches(title, keywords, prepositions):
    """
    Détecte un titre de type "polar du terroir".

    Règle : [article optionnel] + MOT-CLÉ + PRÉPOSITION + [article] + LIEU
    Où :
    - article optionnel = "Le", "La", "Les", "L'" en début de titre (ignoré)
    - MOT-CLÉ ∈ keywords (insensible à la casse)
    - PRÉPOSITION ∈ prepositions (insensible à la casse)
    - article intermédiaire = "le", "la", "les", "l'" (toléré, ex: "dans le Vercors")
    - LIEU = mot commençant par une MAJUSCULE (Unicode)

    L'exigence de majuscule sur le lieu élimine les faux positifs comme
    "Meurtres en série" ou "Les Mystères de l'amour", où le mot final
    est un nom commun en minuscule.
    """
    if not title:
        return False

    # 1. Retire un éventuel article au début (Le/La/Les/L')
    stripped = title.strip()
    for article in ("Les ", "Le ", "La ", "L'"):
        if stripped.lower().startswith(article.lower()):
            stripped = stripped[len(article):].lstrip()
            break

    # 2. Vérifie qu'on commence par un mot-clé suivi d'un espace
    stripped_lower = stripped.lower()
    matched_kw_len = 0
    for kw in keywords:
        kw_lower = kw.lower()
        if stripped_lower.startswith(kw_lower + " "):
            matched_kw_len = len(kw_lower)
            break
    if matched_kw_len == 0:
        return False

    # 3. Ce qui suit le mot-clé doit être : préposition + [article] + lieu majuscule
    remainder = stripped[matched_kw_len:].lstrip()
    if not remainder:
        return False
    remainder_lower = remainder.lower()

    intermediate_articles = ("le ", "la ", "les ", "l'", "mont ", "saint ", "sainte ", "st ", "ste ", "val ", "île ", "ile ", "cap ", "lac ", "bois ", "pointe ", "baie ", "côte ")

    def starts_with_uppercase_after(text):
        """True si text commence par un caractère majuscule, ou par un
        article minuscule suivi d'une majuscule."""
        if not text:
            return False
        if text[0].isupper():
            return True
        text_lower = text.lower()
        for art in intermediate_articles:
            if text_lower.startswith(art):
                rest = text[len(art):].lstrip()
                if rest and rest[0].isupper():
                    return True
        return False

    for prep in prepositions:
        prep_lower = prep.lower()
        # Préposition avec apostrophe finale ("d'") : pas d'espace après
        if prep_lower.endswith("'"):
            if remainder_lower.startswith(prep_lower):
                after = remainder[len(prep_lower):].lstrip()
                if starts_with_uppercase_after(after):
                    return True
        # Préposition normale : suivie d'un espace
        else:
            if remainder_lower.startswith(prep_lower + " "):
                after = remainder[len(prep_lower) + 1:].lstrip()
                if starts_with_uppercase_after(after):
                    return True
    return False


def extract_programmes(root, channel_map, keywords, prepositions,
                       window_start, window_end, no_filter=False):
    """
    Extrait les programmes :
    - dont la chaîne est dans channel_map
    - dont le titre matche la règle "polar du terroir" (sauf si no_filter)
    - dont le début est dans [window_start, window_end]
    """
    hits = []
    for prog in root.findall("programme"):
        ch_id = prog.get("channel", "")
        if ch_id not in channel_map:
            continue
        try:
            start = parse_xmltv_date(prog.get("start", ""))
            stop = parse_xmltv_date(prog.get("stop", ""))
        except Exception:
            continue
        if not (window_start <= start <= window_end):
            continue
        title_el = prog.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not no_filter and not title_matches(title, keywords, prepositions):
            continue
        desc_el = prog.find("desc")
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        cat_el = prog.find("category")
        category = (cat_el.text or "").strip() if cat_el is not None else ""
        icon_el = prog.find("icon")
        icon = icon_el.get("src", "") if icon_el is not None else ""
        subtitle_el = prog.find("sub-title")
        subtitle = (subtitle_el.text or "").strip() if subtitle_el is not None else ""
        hits.append({
            "start": start.isoformat(),
            "stop": stop.isoformat(),
            "channel": channel_map[ch_id],
            "title": title,
            "subtitle": subtitle,
            "description": desc,
            "category": category,
            "icon": icon,
        })
    return hits


def deduplicate(programmes):
    """Supprime les doublons exacts (même chaîne, même heure, même titre)."""
    seen = set()
    out = []
    for p in programmes:
        key = (p["channel"], p["start"], p["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true",
                        help="Liste toutes les chaînes des XML (pour calibrer)")
    parser.add_argument("--keep-xml", action="store_true",
                        help="Garde les XML téléchargés sur disque")
    parser.add_argument("--no-filter", action="store_true",
                        help="Liste TOUS les programmes des chaînes ciblées "
                             "(pour vérifier le filtre par titres)")
    args = parser.parse_args()

    # Fenêtre temporelle (en local, naive puis aware UTC pour comparaison)
    now = datetime.now(timezone.utc)
    window_start = now
    window_end = now + timedelta(days=WINDOW_DAYS)
    log(f"Fenêtre : {window_start.date()} → {window_end.date()}")

    all_programmes = []
    all_found_channels = {}

    for name, url in SOURCES.items():
        log(f"--- Source {name} ---")
        try:
            xml_bytes = download_xml(url)
        except Exception as e:
            log(f"  ⚠️  Échec téléchargement {name} : {e}")
            continue

        if args.keep_xml:
            Path(f"{name}.xml").write_bytes(xml_bytes)
            log(f"  → sauvegardé : {name}.xml")

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            log(f"  ⚠️  XML invalide : {e}")
            continue

        channel_map, all_channels = find_matching_channels(root, TARGET_CHANNELS)
        log(f"  → {len(all_channels)} chaînes au total dans {name}")
        log(f"  → {len(channel_map)} chaînes ciblées trouvées :")
        for ch_id, canon in channel_map.items():
            log(f"     • {canon}  (id XMLTV : {ch_id})")
            all_found_channels[canon] = ch_id

        if args.inspect:
            log("  --- Liste complète (mode inspect) ---")
            for ch_id, names in all_channels:
                log(f"     [{ch_id}] {' | '.join(names)}")
            continue

        progs = extract_programmes(root, channel_map,
                                   TITLE_KEYWORDS, LOCATION_PREPOSITIONS,
                                   window_start, window_end,
                                   no_filter=args.no_filter)
        log(f"  → {len(progs)} programmes correspondants extraits")
        all_programmes.extend(progs)

    if args.inspect:
        log("Mode inspect terminé. Pas de radar.json généré.")
        return

    # Cherche les chaînes manquantes
    missing = [c for c in TARGET_CHANNELS if c not in all_found_channels]
    if missing:
        log(f"⚠️  Chaînes NON trouvées : {', '.join(missing)}")
        log("   Lance avec --inspect pour voir les noms disponibles.")

    # Dédoublonnage et tri chronologique
    all_programmes = deduplicate(all_programmes)
    all_programmes.sort(key=lambda p: p["start"])
    log(f"=> Total après dédoublonnage : {len(all_programmes)} programmes")

    # Génération du JSON
    output = {
        "generated_at": now.isoformat(),
        "window_start": window_start.date().isoformat(),
        "window_end": window_end.date().isoformat(),
        "channels": sorted(all_found_channels.keys()),
        "missing_channels": missing,
        "keywords_used": TITLE_KEYWORDS,
        "prepositions_used": LOCATION_PREPOSITIONS,
        "count": len(all_programmes),
        "programmes": all_programmes,
    }

    output_file = "radar_all.json" if args.no_filter else OUTPUT_JSON
    Path(output_file).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log(f"✅ Écrit : {output_file} ({Path(output_file).stat().st_size // 1024} Ko)")

    # Aperçu console
    if all_programmes:
        log("--- Aperçu des 5 premiers ---")
        for p in all_programmes[:5]:
            dt = datetime.fromisoformat(p["start"])
            log(f"  {dt.strftime('%a %d/%m %Hh%M')} | {p['channel']:10s} | {p['title']}")


if __name__ == "__main__":
    main()
