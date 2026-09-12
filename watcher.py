#!/usr/bin/env python3
"""
Surveille en continu les logements Crous disponibles à Rennes et envoie un
email (via Resend) :
  - immédiatement si la liste des logements disponibles a changé depuis la
    dernière vérification,
  - sinon, seulement si ça fait au moins COOLDOWN_HOURS (4h par défaut)
    depuis le dernier email envoyé (pour ne pas spammer avec le même résultat).

Conçu pour tourner comme un "Background Worker" sur Render (processus qui
tourne en continu), PAS comme un Cron Job : un Cron Job redémarre le script
à zéro à chaque exécution et perdrait donc la mémoire du dernier résultat
envoyé, ce qui empêcherait la logique "attendre 4h" de fonctionner.

Variables d'environnement :

    RESEND_API_KEY         clé API Resend (https://resend.com/api-keys)
    EMAIL_FROM             adresse expéditrice validée sur Resend
    EMAIL_TO               adresse(s) destinataire(s), séparées par des virgules
    CHECK_INTERVAL_MINUTES intervalle entre deux vérifications (défaut: 15)
    COOLDOWN_HOURS         délai avant de renvoyer un résultat identique (défaut: 4)
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv

    load_dotenv()  # charge un fichier .env en local ; sans effet si absent (ex: sur Render)
except ImportError:
    pass

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SEARCH_URL = (
    "https://trouverunlogement.lescrous.fr/tools/47/search"
    "?bounds=-1.7525876_48.1549705_-1.6244045_48.0769155&locationName=Rennes"
)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_TO = [addr.strip() for addr in os.environ.get("EMAIL_TO", "").split(",") if addr.strip()]

CHECK_INTERVAL_MINUTES = float(os.environ.get("CHECK_INTERVAL_MINUTES", "15"))
COOLDOWN_HOURS = float(os.environ.get("COOLDOWN_HOURS", "4"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrousRennesWatcher/1.0; +https://render.com)"
}

# ----------------------------------------------------------------------------
# Récupération et parsing des annonces
# ----------------------------------------------------------------------------

def fetch_listings():
    """Récupère la page de recherche et renvoie une liste de dicts {id, name, url, price, address, surface}."""
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    listings = []
    seen_ids_this_run = set()

    # Chaque logement contient un lien vers /tools/{toolId}/accommodations/{id}
    link_pattern = re.compile(r"/tools/\d+/accommodations/(\d+)")

    for link in soup.find_all("a", href=link_pattern):
        match = link_pattern.search(link["href"])
        if not match:
            continue
        listing_id = match.group(1)
        if listing_id in seen_ids_this_run:
            continue
        seen_ids_this_run.add(listing_id)

        full_url = link["href"]
        if full_url.startswith("/"):
            full_url = "https://trouverunlogement.lescrous.fr" + full_url

        name = link.get_text(strip=True) or f"Logement {listing_id}"

        # On remonte au bloc conteneur (carte de l'annonce) pour en extraire le texte complet
        container = link.find_parent(["li", "article", "div"])
        block_text = container.get_text(" ", strip=True) if container else link.get_text(" ", strip=True)

        price_match = re.search(r"(\d+[,.]?\d*)\s*€", block_text)
        price = price_match.group(0) if price_match else "prix non précisé"

        surface_match = re.search(r"(\d+[,.]?\d*)\s*m²", block_text)
        surface = surface_match.group(0) if surface_match else None

        address_match = re.search(r"\d{1,3}[^,]*?\b\d{5}\b[^,€]*", block_text)
        address = address_match.group(0).strip() if address_match else None

        listings.append(
            {
                "id": listing_id,
                "name": name,
                "url": full_url,
                "price": price,
                "surface": surface,
                "address": address,
            }
        )

    # Tri par id pour que la comparaison "même résultat ?" soit stable
    listings.sort(key=lambda item: item["id"])
    return listings


def listings_signature(listings):
    """Représentation stable pour comparer deux résultats (id + prix suffisent à détecter un changement utile)."""
    return tuple((item["id"], item["price"]) for item in listings)


# ----------------------------------------------------------------------------
# Envoi de l'email via Resend
# ----------------------------------------------------------------------------

def send_email(listings):
    if not RESEND_API_KEY or not EMAIL_FROM or not EMAIL_TO:
        print("RESEND_API_KEY / EMAIL_FROM / EMAIL_TO manquant(s) : email non envoyé.", file=sys.stderr)
        return

    if listings:
        rows = ""
        for item in listings:
            details = " · ".join(
                filter(None, [item["price"], item["surface"], item["address"]])
            )
            rows += (
                f"<li style='margin-bottom:14px'>"
                f"<a href='{item['url']}' style='font-weight:bold;font-size:16px'>{item['name']}</a><br>"
                f"<span style='color:#444'>{details}</span>"
                f"</li>"
            )
        body = f"<ul style='list-style:none;padding:0'>{rows}</ul>"
        subject = f"{len(listings)} logement(s) Crous disponible(s) à Rennes"
    else:
        body = "<p>Aucun logement disponible pour le moment.</p>"
        subject = "Aucun logement Crous disponible à Rennes"

    html = f"""
    <div style="font-family:sans-serif;max-width:600px">
      <h2>🏠 Logements Crous à Rennes</h2>
      {body}
      <p style="color:#888;font-size:12px">
        Vérifié le {datetime.now(timezone.utc).strftime('%d/%m/%Y à %H:%M UTC')} —
        <a href="{SEARCH_URL}">voir la recherche complète</a>
      </p>
    </div>
    """

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": EMAIL_FROM,
            "to": EMAIL_TO,
            "subject": subject,
            "html": html,
        },
        timeout=20,
    )

    if resp.status_code >= 300:
        print(f"Erreur Resend ({resp.status_code}): {resp.text}", file=sys.stderr)
        resp.raise_for_status()

    print(f"Email envoyé ({len(listings)} logement(s)).")


# ----------------------------------------------------------------------------
# Boucle principale
# ----------------------------------------------------------------------------

def check_once(last_signature, last_sent_at):
    """Fait une vérification. Renvoie (nouvelle_signature, nouvelle_date_envoi)."""
    now = datetime.now(timezone.utc)
    print(f"[{now.isoformat()}] Vérification des logements Crous à Rennes...")

    listings = fetch_listings()
    signature = listings_signature(listings)
    print(f"{len(listings)} logement(s) actuellement affiché(s) sur le site.")
    for item in listings:
        print(f"  - {item['name']} ({item['price']}) -> {item['url']}")

    changed = signature != last_signature
    cooldown_elapsed = (
        last_sent_at is None or now - last_sent_at >= timedelta(hours=COOLDOWN_HOURS)
    )

    if changed:
        print("Le résultat a changé depuis la dernière vérification -> envoi immédiat.")
        send_email(listings)
        return signature, now

    if cooldown_elapsed:
        print(f"Résultat identique mais {COOLDOWN_HOURS}h se sont écoulées -> renvoi.")
        send_email(listings)
        return signature, now

    remaining = timedelta(hours=COOLDOWN_HOURS) - (now - last_sent_at)
    print(f"Résultat identique, on attend encore {remaining} avant de renvoyer.")
    return signature, last_sent_at


def main():
    last_signature = None
    last_sent_at = None

    while True:
        try:
            last_signature, last_sent_at = check_once(last_signature, last_sent_at)
        except requests.RequestException as exc:
            print(f"Erreur réseau, on réessaiera au prochain cycle : {exc}", file=sys.stderr)

        print(f"Prochaine vérification dans {CHECK_INTERVAL_MINUTES} minute(s).\n")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()