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
import hashlib
from datetime import datetime
from typing import Dict, Tuple


new_id = None

def save_ics_from_partial_response(response_text: str,
                                   filename: str = "monplanning.ics",
                                   prodid_base: str = "-//Onboard//Planning//FR") -> Dict[str, str]:
    """
    Extrait les événements depuis une réponse partielle JSF contenant un JSON
    et enregistre un fichier .ics contenant plusieurs VCALENDAR (un par groupe).
    Retourne un dictionnaire 'headers' à utiliser pour la réponse HTTP :
      - Content-Type: text/calendar; charset=utf-8
      - Cache-Control: no-cache
      - ETag: "<sha1>"

    Structure attendue du JSON : une liste d'objets contenant chacun une clé "events"
    (comme dans tes exemples précédents).
    """
    # --- 1) extraire le JSON avec une regex ---
    m = re.search(r'\[\{.*\}\]', response_text, re.DOTALL)
    if not m:
        raise ValueError("Impossible de trouver le JSON des événements dans la réponse.")
    events_json = m.group(0)

    # --- 2) parser le JSON ---
    events_container = json.loads(events_json)

    # --- 3) mapping summary -> (calendar name, hex color) ---
    mapping = {
        "CM":    ("CM - Cours (CM)", "#000080"),        # bleu marine
        "TD":    ("TD - Travaux Dirigés", "#ADD8E6"),   # bleu clair
        "TP":    ("TP - Travaux Pratiques", "#008000"), # vert
        "DS":    ("DS - Devoir Surveillé", "#FF0000"),  # rouge
        "MANIF": ("MANIF - Manifestation", "#FFD700"), # jaune
    }

    def find_key_for_summary(summary: str) -> str:
        if not summary:
            return "DEFAULT"
        s = summary.upper()
        for key in mapping:
            # mot séparé (évite faux-positifs)
            if re.search(rf'(?<![A-Z0-9]){re.escape(key)}(?![A-Z0-9])', s):
                return key
        return "DEFAULT"

    def to_ics_date(dt_str: str) -> str:
        # exemple attendu: "2025-09-08T10:15:00+0200"
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z")
        return dt.strftime("%Y%m%dT%H%M%S")

    # --- 4) regrouper les events par clé (CM, TD, ...) ---
    calendars = {}  # key -> {"name":..., "color":..., "events":[...]}
    for cal_obj in events_container:
        ev_list = cal_obj.get("events") if isinstance(cal_obj, dict) else None
        if not ev_list:
            continue
        for ev in ev_list:
            summary = (ev.get("title") or "").strip()
            key = find_key_for_summary(summary)
            if key == "DEFAULT":
                name = "Planning"
                color = None
            else:
                name, color = mapping[key]
            calendars.setdefault(key, {"name": name, "color": color, "events": []})
            calendars[key]["events"].append(ev)

    # --- 5) construire le contenu ICS (CRLF \r\n) avec plusieurs VCALENDAR ---
    blocks = []
    total_events = 0
    cal_index = 0
    for key, cal in calendars.items():
        cal_index += 1
        lines = []
        # VCALENDAR header
        prodid = prodid_base if cal_index == 1 else f"{prodid_base}{cal_index}"
        lines.extend([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:{prodid}",
            "CALSCALE:GREGORIAN",
            f"X-WR-CALNAME:{cal['name']}",
        ])
        if cal["color"]:
            lines.append(f"X-APPLE-CALENDAR-COLOR:{cal['color']}")

        # Events
        for ev in cal["events"]:
            total_events += 1
            uid_raw = str(ev.get("id", "")) or f"ev{total_events}"
            uid = f"{uid_raw}@onboard.ec-nantes.fr"
            dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            start = to_ics_date(ev["start"])
            end = to_ics_date(ev["end"])
            summary = (ev.get("title") or "Événement").replace("\n", " ").strip()

            vevent = [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART;TZID=Europe/Paris:{start}",
                f"DTEND;TZID=Europe/Paris:{end}",
                f"SUMMARY:{summary}",
                "END:VEVENT",
            ]
            lines.extend(vevent)

        lines.append("END:VCALENDAR")
        blocks.append("\r\n".join(lines))

    # Concaténer tous les VCALENDAR dans l'ordre (un seul fichier)
    ics_content = "\r\n".join(blocks) + "\r\n"

    # --- 6) écrire le fichier avec CRLF ---
    # Important: utiliser newline='' et écrire la chaîne telle quelle pour préserver CRLF
    with open(filename, "w", encoding="utf-8", newline='') as f:
        f.write(ics_content)

    # --- 7) calculer un ETag (SHA1) basé sur le contenu ---
    sha1 = hashlib.sha1(ics_content.encode("utf-8")).hexdigest()
    etag = f'"{sha1}"'

    # headers à exposer via HTTP (ou à configurer dans ton hébergement)
    headers = {
        "Content-Type": "text/calendar; charset=utf-8",
        "Cache-Control": "no-cache",
        "ETag": etag,
    }

    print(f"✅ Fichier ICS généré ({total_events} événements) -> {filename}")
    print(f"ℹ️ ETag: {etag}")

    return headers



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
from bs4 import BeautifulSoup
import re

def replace_id(payload,schedule_id,old_id):

    print(f"[replace_id] remplacement automatique : {old_id} -> {schedule_id}")
    for key in list(payload.keys()):
        if old_id in key:
            new_key = key.replace(old_id, schedule_id)
            # déplacer la valeur vers la nouvelle clé
            payload[new_key] = payload.pop(key)
    # remplacer les valeurs exactes égales à old_id
    for key in list(payload.keys()):
        if isinstance(payload[key], str) and payload[key] == old_id:
            payload[key] = schedule_id


def get_old_id(payload):
    old_candidates = set()

    # chercher dans les valeurs
    for v in payload.values():
        if isinstance(v, str):
            for m in re.findall(r'(form:j_idt\d+)', v):
                old_candidates.add(m)
    # chercher dans les clés
    for k in list(payload.keys()):
        for m in re.findall(r'(form:j_idt\d+)', k):
            old_candidates.add(m)

    # prioriser la source explicite si présente
    old_id = None
    if isinstance(payload.get("javax.faces.source"), str) and re.match(r'form:j_idt\d+', payload.get("javax.faces.source")):
        old_id = payload.get("javax.faces.source")
    elif old_candidates:
        # choisir le plus fréquent / premier candidat
        old_id = next(iter(old_candidates))


    return old_id

def requete_post(payload, name, url=None, ajax=False, extra_headers=None, pause=1.0):
    global current_viewstate, session,new_id


    if new_id is not None :
        replace_id(payload,new_id,get_old_id(payload))

    if url is None:
        url = MAINMENU_PAGE

    # injecter le ViewState courant si pas déjà fourni
    # injecter le ViewState courant si pas déjà fourni
    if 'javax.faces.ViewState' not in payload or not payload.get('javax.faces.ViewState'):
        if current_viewstate:
            payload['javax.faces.ViewState'] = current_viewstate

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


    print("----")
    print(f"new id : {new_id}")
    print(" ")
    print(f"payload effectif : {payload}")
    r = session.post(url, data=payload, headers=headers, allow_redirects=True)
    print(" ")
    ensure_success(r, f"POST {name}")

    # -- tentative d'analyse robuste d'une partial-response JSF (XML) --
    schedule_id = None
    collected_hidden = {}

    try:
        root = ET.fromstring(r.text)
        # parcourir chaque <update> du partial-response
        for upd in root.findall(".//update"):
            upd_id = upd.get("id") or ""
            upd_text = upd.text or ""

            # si l'update contient le composant schedule, on peut récupérer l'id
            # cas 1: primefaces script contient id:"form:j_idtXXX"
            m = re.search(r'id\s*:\s*"([^"]+)"', upd_text)
            if m:
                candidate = m.group(1)
                # heuristique: les ids de primefaces commencent par 'form:j_idt'
                if re.match(r'form:j_idt\d+', candidate):
                    schedule_id = candidate

            # cas 2: l'attribut update lui-même peut porter l'id 'form:j_idtXXX'
            if not schedule_id:
                m2 = re.search(r'(form:j_idt\d+)', upd_id)
                if m2:
                    schedule_id = m2.group(1)

            # parser le contenu HTML (CDATAs) pour extraire les input hidden à l'intérieur de cet update
            soup_upd = BeautifulSoup(upd_text, "html.parser")
            for inp in soup_upd.find_all("input", {"type": "hidden"}):
                name = inp.get("name") or inp.get("id")
                value = inp.get("value", "")
                if name:
                    collected_hidden[name] = value

        # tenter d'extraire ViewState depuis ce XML (mise à jour)
        new_vs = extract_viewstate_from_jsf_partial(r.text)
    except ET.ParseError:
        # fallback (si la réponse n'est pas strict XML) : extraire inputs depuis le HTML complet
        soup = BeautifulSoup(r.text, "html.parser")
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name") or inp.get("id")
            value = inp.get("value", "")
            if name:
                collected_hidden[name] = value

        # essayer de retrouver schedule_id dans le texte brut
        m = re.search(r'PrimeFaces\.cw\("Schedule".*?id\s*:\s*"([^"]+)"', r.text)
        if m:
            schedule_id = m.group(1)
        else:
            m2 = re.search(r'<update id="(form:j_idt\d+)"', r.text)
            if m2:
                schedule_id = m2.group(1)

        # fallback pour ViewState depuis HTML
        new_vs = extract_viewstate_from_html(r.text)
        if not new_vs:
            new_vs = extract_viewstate_from_jsf_partial(r.text)
    # -- tentative d'analyse robuste d'une partial-response JSF (XML) --
    schedule_id = None
    collected_hidden = {}

    try:
        root = ET.fromstring(r.text)
        # parcourir chaque <update> du partial-response
        for upd in root.findall(".//update"):
            upd_id = upd.get("id") or ""
            upd_text = upd.text or ""

            # si l'update contient le composant schedule, on peut récupérer l'id
            # cas 1: primefaces script contient id:"form:j_idtXXX"
            m = re.search(r'id\s*:\s*"([^"]+)"', upd_text)
            if m:
                candidate = m.group(1)
                # heuristique: les ids de primefaces commencent par 'form:j_idt'
                if re.match(r'form:j_idt\d+', candidate):
                    schedule_id = candidate

            # cas 2: l'attribut update lui-même peut porter l'id 'form:j_idtXXX'
            if not schedule_id:
                m2 = re.search(r'(form:j_idt\d+)', upd_id)
                if m2:
                    schedule_id = m2.group(1)

            # parser le contenu HTML (CDATAs) pour extraire les input hidden à l'intérieur de cet update
            soup_upd = BeautifulSoup(upd_text, "html.parser")
            for inp in soup_upd.find_all("input", {"type": "hidden"}):
                name = inp.get("name") or inp.get("id")
                value = inp.get("value", "")
                if name:
                    collected_hidden[name] = value

        # tenter d'extraire ViewState depuis ce XML (mise à jour)
        new_vs = extract_viewstate_from_jsf_partial(r.text)
    except ET.ParseError:
        # fallback (si la réponse n'est pas strict XML) : extraire inputs depuis le HTML complet
        soup = BeautifulSoup(r.text, "html.parser")
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name") or inp.get("id")
            value = inp.get("value", "")
            if name:
                collected_hidden[name] = value

        # essayer de retrouver schedule_id dans le texte brut
        m = re.search(r'PrimeFaces\.cw\("Schedule".*?id\s*:\s*"([^"]+)"', r.text)
        if m:
            schedule_id = m.group(1)
        else:
            m2 = re.search(r'<update id="(form:j_idt\d+)"', r.text)
            if m2:
                schedule_id = m2.group(1)

        # fallback pour ViewState depuis HTML
        new_vs = extract_viewstate_from_html(r.text)
        if not new_vs:
            new_vs = extract_viewstate_from_jsf_partial(r.text)

    # fusionner les hidden collectés dans le payload si absent ou vide
    for k, v in collected_hidden.items():
        if k not in payload or not payload.get(k):
            payload[k] = v

    # mettre à jour ViewState si trouvé
    # fusionner les hidden collectés dans le payload si absent ou vide
    for k, v in collected_hidden.items():
        if k not in payload or not payload.get(k):
            payload[k] = v

    # mettre à jour ViewState si trouvé
    if new_vs:
        if new_vs != current_viewstate:
            print(f"[viewstate] mis à jour après {name} (len={len(new_vs)})")
        current_viewstate = new_vs
        payload["javax.faces.ViewState"] = current_viewstate

    # --- détecter l'old_id présent dans le payload (ex: form:j_idt118) ---
    old_candidates = set()

    # chercher dans les valeurs
    for v in payload.values():
        if isinstance(v, str):
            for m in re.findall(r'(form:j_idt\d+)', v):
                old_candidates.add(m)
    # chercher dans les clés
    for k in list(payload.keys()):
        for m in re.findall(r'(form:j_idt\d+)', k):
            old_candidates.add(m)

    # prioriser la source explicite si présente
    old_id = None
    if isinstance(payload.get("javax.faces.source"), str) and re.match(r'form:j_idt\d+', payload.get("javax.faces.source")):
        old_id = payload.get("javax.faces.source")
    elif old_candidates:
        # choisir le plus fréquent / premier candidat
        old_id = next(iter(old_candidates))

    # si on a un schedule_id différent de old_id, on remplace toutes les clés/valeurs contenant old_id
    if schedule_id:

        print(" ")
        print("---->>>> remplacement auto   <<<<<-------")
        print(" ")
        if old_id and old_id != schedule_id:
            new_id = schedule_id
            
            replace_id(payload,schedule_id,old_id)
        # s'assurer que les champs source/execute/render pointent vers le schedule_id
        payload["javax.faces.source"] = schedule_id
        payload["javax.faces.partial.execute"] = schedule_id
        payload["javax.faces.partial.render"] = schedule_id
        payload[schedule_id] = schedule_id

    # courte pause pour ne pas spammer le serveur
    if pause:
        time.sleep(pause)

    print(f"payload modifié ou pas : {payload}")
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

    global new_id


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

    new_id = "form:j_idt121"

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
            "form:j_idt118_start": "1757677600000",
            "form:j_idt118_end": "1775944800000",
            "form": "form",
            "form:largeurDivCenter": "1550",
            "form:idInit": "webscolaapp.Planning_-3130307915882446410",
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

    date1 = "15/09/2025"
    week1 = "38-2025"


    dl_ics(date1,week1)

print("Fini.")


    


if __name__ == '__main__':
    main()
