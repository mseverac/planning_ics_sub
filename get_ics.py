# refactor of get_ics_full_flow.py
# Usage: ensure mdp.password exists, then run `python3 get_ics_refactor.py`

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
import sys
import time

import re
import json
from datetime import datetime


import os
# Récupère le mot de passe depuis la variable d'environnement ONBOARD_PASS



password = os.environ.get("ONBOARD_PASS")
if not password:
    raise SystemExit("Erreur: la variable d'environnement ONBOARD_PASS n'est pas définie. "
                     "Sous GitHub Actions, ajoute-la dans Settings → Secrets.")
# optionnel : username aussi configurable via env (par défaut ta valeur actuelle)
USERNAME = os.environ.get("ONBOARD_USER", "mseverac2023")

import re
import json
from datetime import datetime

def save_ics_from_partial_response(response_text: str, filename="monplanning.ics"):
    """
    Extrait les événements depuis une réponse partielle JSF contenant un JSON
    et enregistre un fichier ICS. Ajoute une couleur par événement selon le SUMMARY :
      - CM   -> bleu marine (#000080)
      - TD   -> bleu clair  (#ADD8E6)
      - TP   -> vert        (#008000)
      - DS   -> rouge       (#FF0000)
      - MANIF-> jaune       (#FFD700)
    """
    # --- 1) extraire le JSON avec une regex ---
    m = re.search(r'\[\{.*\}\]', response_text, re.DOTALL)
    if not m:
        raise ValueError("Impossible de trouver le JSON des événements dans la réponse.")
    events_json = m.group(0)

    # --- 2) parser le JSON ---
    events = json.loads(events_json)

    # helper: map summary -> couleur hex
    def color_for_summary(summary: str):
        if not summary:
            return None
        s = summary.upper()
        if re.search(r'\bCM\b', s):
            return "#000080"   # bleu marine
        if re.search(r'\bTD\b', s):
            return "#ADD8E6"   # bleu clair
        if re.search(r'\bTP\b', s):
            return "#008000"   # vert
        if re.search(r'\bDS\b', s):
            return "#FF0000"   # rouge
        if re.search(r'\bMANIF\b', s):
            return "#FFD700"   # jaune
        return None

    # --- 3) construire le contenu ICS ---
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Onboard//Planning//FR",
        "CALSCALE:GREGORIAN",
    ]

    event_count = 0
    # attention: ton JSON semble contenir une liste et les events dans events[0]["events"]
    for cal in events:
        ev_list = cal.get("events") if isinstance(cal, dict) else None
        if not ev_list:
            continue
        for ev in ev_list:
            event_count += 1

            # convertir start/end en format iCalendar (local)
            def to_ics_date(dt_str):
                # exemple: "2025-09-08T10:15:00+0200"
                dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z")
                return dt.strftime("%Y%m%dT%H%M%S")

            uid = ev.get("id", "") + "@onboard.ec-nantes.fr"
            start = to_ics_date(ev["start"])
            end = to_ics_date(ev["end"])
            summary = ev.get("title", "").strip()

            # déterminer couleur
            color = color_for_summary(summary)

            vevent = [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTART;TZID=Europe/Paris:{start}",
                f"DTEND;TZID=Europe/Paris:{end}",
                f"SUMMARY:{summary}",
            ]

            # ajouter propriétés de couleur si définie (X-APPLE pour Apple Calendar)
            if color:
                vevent.append(f"X-APPLE-CALENDAR-COLOR:{color}")
                vevent.append(f"COLOR:{color}")  # propriété non standard, parfois utilisée

            vevent.append("END:VEVENT")
            lines.extend(vevent)

    lines.append("END:VCALENDAR")

    # --- 4) écrire le fichier ---
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✅ Fichier ICS généré avec {event_count} événements -> {filename}")

BASE = "https://onboard.ec-nantes.fr"
LOGIN_PAGE = BASE + "/faces/Login.xhtml"
MAINMENU_PAGE = BASE + "/faces/MainMenuPage.xhtml"
PLANNING_PAGE = BASE + "/faces/Planning.xhtml"

UA = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0"

# session globale utilisée par la fonction requete_post
session = requests.Session()
session.headers.update({"User-Agent": UA})
# viewstate courant (sera mis à jour automatiquement par requete_post)
current_viewstate = None


def save_debug_response(name, response):
    """Sauvegarde la réponse textuelle pour debug (HTML ou XML partial)."""
    fname = f"debug_response_{name}.html"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(response.text)
    print(f"-> debug saved: {fname} (len={len(response.text)})")


def extract_viewstate_from_html(html):
    """Retourne la valeur de javax.faces.ViewState si présente dans un HTML."""
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "javax.faces.ViewState"})
    if inp and inp.get("value"):
        return inp["value"]
    return None


def extract_viewstate_from_jsf_partial(xml_text):
    """
    JSF partial responses are XML like:
    <?xml ...?><partial-response>...<update id="javax.faces.ViewState">VALUE</update>...</partial-response>
    Return VALUE or None.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for upd in root.findall('.//update'):
        id_attr = upd.get('id') or upd.get('name') or ''
        if 'ViewState' in id_attr or 'javax.faces.ViewState' in id_attr:
            return (upd.text or '').strip()
    return None


def ensure_success(r, context="request"):
    if r.status_code >= 400:
        print(f"[ERROR] {context} returned HTTP {r.status_code}")
        print(r.text[:800])
        sys.exit(1)


# ----- fonction demandée: requete_post -----

def requete_post(payload, name, url=None, ajax=False, extra_headers=None, pause=1.0):
    """
    Envoie un POST avec le payload donné, sauvegarde la réponse pour debug
    et met à jour la variable globale `current_viewstate` si un nouveau ViewState est trouvé
    (dans un HTML classique ou dans une réponse JSF partial XML).

    Arguments:
        payload (dict): données du formulaire
        name (str): préfixe pour le fichier debug et affichage
        url (str): URL cible (fallback: MAINMENU_PAGE si None)
        ajax (bool): si True, ajoute les headers pour une requête JSF partial/ajax
        extra_headers (dict): headers additionnels (fusionnés)
        pause (float): pause en secondes après la requête

    Retourne: requests.Response
    """
    global current_viewstate, session

    if url is None:
        url = MAINMENU_PAGE

    # injecter le ViewState courant si disponible et si l'appel ne fournit pas déjà un ViewState
    if 'javax.faces.ViewState' not in payload or not payload.get('javax.faces.ViewState'):
        if current_viewstate:
            payload['javax.faces.ViewState'] = current_viewstate

    # headers par défaut selon ajax ou navigation classique
    headers = {
        "Origin": BASE,
        "Referer": url if url else BASE + "/",
    }
    if ajax:
        headers.update({
            "Accept": "application/xml, text/xml, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Faces-Request": "partial/ajax",
            "X-Requested-With": "XMLHttpRequest",
        })
    else:
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
        })

    if extra_headers:
        headers.update(extra_headers)

    print(f"POST {name} -> {url} (ajax={ajax})")
    r = session.post(url, data=payload, headers=headers, allow_redirects=True)
    ensure_success(r, f"POST {name}")

    # sauvegarde debug
    #save_debug_response(name, r)

    # tentative d'extraction d'un nouveau ViewState
    new_vs = extract_viewstate_from_html(r.text)
    if not new_vs:
        new_vs = extract_viewstate_from_jsf_partial(r.text)

    if new_vs:
        if new_vs != current_viewstate:
            print(f"[viewstate] mis à jour après {name} (len={len(new_vs)})")
        current_viewstate = new_vs

    # courte pause pour ne pas spammer le serveur
    if pause:
        time.sleep(pause)

    return r


# ------------------ Script simplifié utilisant requete_post ------------------

def main():
    global current_viewstate, session

    # 1) GET page de login
    print("GET login page...")
    r = session.get(LOGIN_PAGE)
    ensure_success(r, "GET login page")
    #save_debug_response("1_get_login", r)

    current_viewstate = extract_viewstate_from_html(r.text)
    if not current_viewstate:
        print("Impossible de trouver ViewState sur la page de login.")
        sys.exit(1)
    print("ViewState login trouvé (len):", len(current_viewstate))

    # récupérer action du formulaire si présent
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", id="formulaireSpring")
    if form and form.get("action"):
        login_post_url = urljoin(BASE, form["action"])
    else:
        login_post_url = LOGIN_PAGE
    print("Login POST URL:", login_post_url)

    # 2) POST login
    login_payload = {
        "username": "mseverac2023",
        "password": password,
        "j_idt27": "",
        # javax.faces.ViewState will be injecté automatiquement par requete_post
    }
    r = requete_post(login_payload, "post_login", url=login_post_url, ajax=False)

    # vérifier si login OK sinon GET / et réessayer
    if ("Déconnexion" in r.text) or ("Mon compte" in r.text) or ("MainMenuPage" in r.text):
        print("✅ Login probablement réussi (détecté par mot-clé).")
    else:
        print("Login response ne contient pas les mots-clés attendus, GET / pour confirmer...")
        r2 = session.get(BASE + "/")
        #save_debug_response("after_login_get_root", r2)
        if "Déconnexion" in r2.text or "MainMenuPage" in r2.text:
            print("✅ Après GET /, on est connecté.")
            r = r2
        else:
            print("❌ Login semble échouer — vérifier identifiants ou ViewState envoyé.")
            print(r.text[:500])
            sys.exit(1)

    # 3) GET MainMenuPage pour avoir un ViewState propre
    print("GET MainMenuPage...")
    r = session.get(MAINMENU_PAGE)
    ensure_success(r, "GET MainMenuPage")
    #save_debug_response("get_mainmenu", r)
    vs = extract_viewstate_from_html(r.text) or extract_viewstate_from_jsf_partial(r.text)
    if vs:
        current_viewstate = vs
        print("ViewState main trouvé (len):", len(current_viewstate))
    else:
        print("Pas de ViewState trouvé sur MainMenuPage -> abort")
        sys.exit(1)

    # 4) POST requête 1 (AJAX) — ouverture sous-menu
    payload1 = {
        "javax.faces.partial.ajax": "true",
        "javax.faces.source": "form:j_idt52",
        "javax.faces.partial.execute": "form:j_idt52",
        "javax.faces.partial.render": "form:sidebar",
        "form:j_idt52": "form:j_idt52",
        "webscolaapp.Sidebar.ID_SUBMENU": "submenu_8817755",
        "form": "form",
        "form:largeurDivCenter": "907",
        "form:idInit": "webscolaapp.MainMenuPage_5977318950196537139",
        "form:sauvegarde": "",
        "form:j_idt856:j_idt858_reflowDD": "0_0",
        "form:j_idt815_focus": "",
        "form:j_idt815_input": "45803",
        # ViewState injecté automatiquement
    }
    r = requete_post(payload1, "ajax_open_submenu", url=MAINMENU_PAGE, ajax=True)

    # 5) POST requête 2 (navigation vers Planning)
    payload2 = {
        "form": "form",
        "form:largeurDivCenter": "907",
        "form:idInit": "webscolaapp.MainMenuPage_5977318950196537139",
        "form:sauvegarde": "",
        "form:j_idt856:j_idt858_reflowDD": "0_0",
        "form:j_idt815_focus": "",
        "form:j_idt815_input": "45803",
        "form:sidebar": "form:sidebar",
        "form:sidebar_menuid": "8_0",
    }
    r = requete_post(payload2, "navigate_planning", url=MAINMENU_PAGE, ajax=False)

    # attendre que la navigation se fasse côté serveur
    print("Attente courte pour que la navigation prenne effet...")
    time.sleep(2)

    # essaye d'extraire le ViewState depuis la réponse de navigation
    vs = extract_viewstate_from_html(r.text) or extract_viewstate_from_jsf_partial(r.text)
    if vs:
        current_viewstate = vs

    # 6) GET Planning.xhtml au besoin pour récupérer inputs dynamiques
    print("GET Planning.xhtml pour récupérer tokens si nécessaire...")
    r_planning = session.get(PLANNING_PAGE)
    ensure_success(r_planning, "GET Planning.xhtml")
    #save_debug_response("planning_page_get", r_planning)

    # extraire ViewState planning
    viewstate_planning = extract_viewstate_from_html(r_planning.text) or extract_viewstate_from_jsf_partial(r_planning.text)
    if not viewstate_planning:
        print("Impossible de récupérer ViewState sur Planning.xhtml -> abort")
        sys.exit(1)
    current_viewstate = viewstate_planning
    print("ViewState planning (len):", len(current_viewstate))

    soup = BeautifulSoup(r_planning.text, "html.parser")

    def get_input_value(soup, name, default=None):
        el = soup.find("input", {"name": name})
        if not el:
            return default
        return el.get("value", default)

    largeur = get_input_value(soup, "form:largeurDivCenter", "907")
    id_init_val = get_input_value(soup, "form:idInit", "webscolaapp.Planning_-1425867247129692267")
    print("idInit:", id_init_val)

    # 7) POST requête 3 (download ICS)


    def dl_ics(date,week):
        payload3 = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": "form:j_idt118",
            "javax.faces.partial.execute": "form:j_idt118",
            "javax.faces.partial.render": "form:j_idt118",
            "form:j_idt118": "form:j_idt118",
            # timestamps: ajuster si besoin
            "form:j_idt118_start": "1757887200000",
            "form:j_idt118_end": "1775944800000",
            "form": "form",
            "form:largeurDivCenter": largeur,
            "form:idInit": id_init_val,
            "form:date_input": date,
            "form:week": week,
            "form:j_idt118_view": "agendaWeek",
            "form:offsetFuseauNavigateur": "-7200000",
            "form:onglets_activeIndex": "0",
            "form:onglets_scrollState": "0",
            # ViewState injecté automatiquement
        }

        r = requete_post(payload3, "download_ics", url=PLANNING_PAGE, ajax=False, pause=1.0)

        payload4 = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": "form:j_idt118",
            "javax.faces.partial.execute": "form:j_idt118",
            "javax.faces.partial.render": "form:j_idt118",
            "form:j_idt118": "form:j_idt118",
            "form:j_idt118_start": "1756677600000",
            "form:j_idt118_end": "1775944800000",
            "form": "form",
            "form:largeurDivCenter": "1550",
            "form:idInit": "webscolaapp.Planning_-7772623432697926238",
            "form:date_input": date,
            "form:week": week,
            "form:j_idt118_view": "agendaWeek",
            "form:offsetFuseauNavigateur": "-7200000",
            "form:onglets_activeIndex": "0",
            "form:onglets_scrollState": "0",
        }

        r = requete_post(payload4, "final_ics_download", url=PLANNING_PAGE, ajax=False, pause=1.0)



        content = r.text

        print("----------")

        print(content)

        print("----------")

        save_ics_from_partial_response(content, filename="planning.ics")

    date1 = "13/09/2025"
    week1 = "38-2025"


    dl_ics(date1,week1)



    


if __name__ == '__main__':
    main()
