#!/usr/bin/env python3
"""
Radar Polar - Détecte les téléfilms policiers du terroir
sur les chaînes francophones belges et françaises.

Source : XML local généré par iptv-org/epg (grabber Pickx).
Usage : python3 radar.py --source=/tmp/pickx_guide.xml
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

# ============================================================
# CONFIGURATION
# ============================================================

WINDOW_DAYS = 7
OUTPUT_FILE = "radar.json"

# Chaînes ciblées : nom canonique -> liste de noms d'affichage acceptés
# Pickx donne les noms tels qu'ils apparaissent sur ton décodeur Proximus.
TARGET_CHANNELS = {
    "TF1":      ["TF1", "TF1 HD"],
    "France 2": ["France 2", "France 2 HD"],
    "France 3": ["France 3", "France 3 HD"],
    "TV5 Monde": ["TV5 Monde", "TV5MONDE", "TV5 MONDE"],
    "La Une":   ["La Une", "La Une HD", "RTBF La Une"],
    "Tipik":    ["Tipik", "Tipik HD"],
}

# Mots-clés signature des polars du terroir
KEYWORDS = [
    "Meurtres", "Meurtre",
    "Crimes", "Crime",
    "Mystères", "Mystère",
    "Secrets", "Secret",
]

# Prépositions reliant le mot-clé au lieu
PREPOSITIONS = ["à", "au", "aux", "en", "dans", "de", "d'", "du", "des", "sur"]

# Articles et particules tolérés en minuscule entre préposition et lieu propre
# ("au mont Ventoux", "dans le Vercors", "en pays Cathare")
INTERMEDIATE_ARTICLES = (
    "le ", "la ", "les ", "l'",
    "mont ", "saint ", "sainte ", "st ", "ste ",
    "val ", "île ", "ile ", "cap ", "lac ", "bois ",
    "pointe ", "baie ", "côte ", "pays ",
)


# ============================================================
# PARSING DES ARGUMENTS
# ============================================================

def parse_args():
    p = argparse.ArgumentParser(description="Radar des polars du terroir")
    p.add_argument("--source", required=True,
                   help="Chemin vers le fichier XML local (Pickx via iptv-org)")
    p.add_argument("--no-filter", action="store_true",
                   help="Désactive le filtre par mots-clés (debug)")
    return p.parse_args()


# ============================================================
# LECTURE DU XML
# ============================================================

def load_xml(source_path):
    """Charge un fichier XML local et renvoie l'arbre parsé."""
    print(f"📂 Lecture du fichier : {source_path}")
    with open(source_path, "rb") as f:
        data = f.read()
    print(f"   Taille : {len(data):,} octets")
    return ET.fromstring(data)


# ============================================================
# IDENTIFICATION DES CHAÎNES
# ============================================================

def collect_channel_ids(tree):
    """
    Trouve les IDs XMLTV des chaînes correspondant aux noms cibles.
    Retourne : dict nom_canonique -> list[ID]
    """
    found = {name: [] for name in TARGET_CHANNELS}
    
    for channel in tree.findall("channel"):
        ch_id = channel.get("id", "")
        display_names = [dn.text or "" for dn in channel.findall("display-name")]
        display_names_norm = [dn.strip().lower() for dn in display_names]
        
        for canonical, variants in TARGET_CHANNELS.items():
            for variant in variants:
                if variant.lower() in display_names_norm:
                    if ch_id and ch_id not in found[canonical]:
                        found[canonical].append(ch_id)
                    break
    
    return found


# ============================================================
# FILTRAGE DES TITRES
# ============================================================

def title_matches(title):
    """
    Vérifie si le titre matche un motif 'Mot-clé + préposition + lieu propre'.
    Ex : 'Meurtres en Balagne', 'Crimes au mont Ventoux', 'Secrets du Finistère'.
    """
    if not title:
        return False
    
    title_lower = title.lower()
    
    for kw in KEYWORDS:
        if kw.lower() not in title_lower:
            continue
        
        # Position du mot-clé
        idx = title_lower.find(kw.lower())
        after = title[idx + len(kw):].strip()
        
        # Tester chaque préposition possible
        for prep in PREPOSITIONS:
            pattern = prep.lower() + " "
            if not after.lower().startswith(pattern):
                continue
            
            # Ce qui vient après la préposition
            remainder = after[len(prep):].strip()
            
            # Tolérer un article ou une particule toponymique en minuscule
            for article in INTERMEDIATE_ARTICLES:
                if remainder.lower().startswith(article):
                    remainder = remainder[len(article):].strip()
                    break
            
            # Le premier caractère du lieu doit être en majuscule
            if remainder and remainder[0].isupper():
                return True
    
    return False


# ============================================================
# PARSING DES DATES XMLTV
# ============================================================

def parse_xmltv_date(date_str):
    """Parse une date au format XMLTV. Ex: '20260607211000 +0200'."""
    s = date_str.strip()
    if " " in s:
        dt_part, tz_part = s.split(" ", 1)
        sign = 1 if tz_part[0] == "+" else -1
        hours = int(tz_part[1:3])
        mins = int(tz_part[3:5])
        offset = timezone(timedelta(hours=sign * hours, minutes=sign * mins))
    else:
        dt_part = s
        offset = timezone.utc
    
    dt = datetime.strptime(dt_part, "%Y%m%d%H%M%S")
    return dt.replace(tzinfo=offset)


# ============================================================
# EXTRACTION DES PROGRAMMES
# ============================================================

def extract_programmes(tree, channel_ids_by_canonical, window_start, window_end, no_filter=False):
    """Extrait les programmes correspondant aux critères."""
    # Inverser : id -> nom canonique
    id_to_canonical = {}
    for canonical, ids in channel_ids_by_canonical.items():
        for cid in ids:
            id_to_canonical[cid] = canonical
    
    programmes = []
    
    for prog in tree.findall("programme"):
        ch_id = prog.get("channel", "")
        if ch_id not in id_to_canonical:
            continue
        
        # Parser les dates
        start_str = prog.get("start", "")
        stop_str = prog.get("stop", "")
        try:
            start_dt = parse_xmltv_date(start_str)
            stop_dt = parse_xmltv_date(stop_str)
        except (ValueError, IndexError):
            continue
        
        # Filtrer par fenêtre temporelle
        if start_dt < window_start or start_dt > window_end:
            continue
        
        # Extraire le titre
        title_el = prog.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        
        # Sous-titre (récupéré AVANT le filtrage pour pouvoir le tester aussi)
        subtitle_el = prog.find("sub-title")
        subtitle = (subtitle_el.text or "").strip() if subtitle_el is not None else ""
        
        # Filtrage par mots-clés : on teste le titre ET le sous-titre
        # (Pickx met parfois le vrai nom du téléfilm dans le sous-titre)
        if not no_filter and not (title_matches(title) or title_matches(subtitle)):
            continue
        
        # Description
        desc_el = prog.find("desc")
        description = (desc_el.text or "").strip() if desc_el is not None else ""
        
        # Catégorie
        cat_el = prog.find("category")
        category = (cat_el.text or "").strip() if cat_el is not None else ""
        
        # Icône (image)
        icon_el = prog.find("icon")
        icon = icon_el.get("src", "") if icon_el is not None else ""
        
        programmes.append({
            "start": start_dt.isoformat(),
            "stop": stop_dt.isoformat(),
            "channel": id_to_canonical[ch_id],
            "title": title,
            "subtitle": subtitle,
            "description": description,
            "category": category,
            "icon": icon,
        })
    
    # Tri par date de début
    # Déduplication : un programme est identifié par (title, start, channel)
    # Pickx contient plusieurs variantes par chaîne (HD, SD, +1) → mêmes programmes répétés
    seen = set()
    deduped = []
    for p in programmes:
        key = (p["title"], p["start"], p["channel"])
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    
    # Tri par date de début
    deduped.sort(key=lambda p: p["start"])
    return deduped

# ============================================================
# MAIN
# ============================================================

def main():
    args = parse_args()
    
    # Charger la source
    tree = load_xml(args.source)
    
    n_channels = len(tree.findall("channel"))
    n_programmes = len(tree.findall("programme"))
    print(f"   {n_channels} chaînes, {n_programmes} programmes au total")
    
    # Identifier les chaînes ciblées
    channel_ids = collect_channel_ids(tree)
    
    print("\n📡 Chaînes ciblées :")
    found_canonicals = []
    missing_canonicals = []
    for canonical in TARGET_CHANNELS:
        ids = channel_ids[canonical]
        if ids:
            found_canonicals.append(canonical)
            print(f"   ✓ {canonical} : {ids}")
        else:
            missing_canonicals.append(canonical)
            print(f"   ✗ {canonical} : non trouvée")
    
    # Fenêtre temporelle
    now = datetime.now(tz=timezone.utc)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(days=WINDOW_DAYS)
    print(f"\n📅 Fenêtre : {window_start.date()} → {window_end.date()}")
    
    # Extraire les programmes
    programmes = extract_programmes(
        tree, channel_ids, window_start, window_end, no_filter=args.no_filter
    )
    
    print(f"\n🎯 Programmes captés : {len(programmes)}")
    for p in programmes:
        print(f"   [{p['start']}] [{p['channel']}] {p['title']}")
    
    # Générer le radar.json
    output = {
        "generated_at": now.isoformat(),
        "window_start": window_start.date().isoformat(),
        "window_end": window_end.date().isoformat(),
        "source": "Pickx via iptv-org/epg",
        "channels": sorted(found_canonicals),
        "missing_channels": sorted(missing_canonicals),
        "keywords_used": KEYWORDS,
        "prepositions_used": PREPOSITIONS,
        "count": len(programmes),
        "programmes": programmes,
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\n💾 Généré : {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
