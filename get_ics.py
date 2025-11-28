#!/usr/bin/env python3
# get_ics.py — safe ICS generation + atomic replace
# Usage: set ONBOARD_PASS env (and optional ONBOARD_USER), puis python get_ics.py

import os
import re
import sys
import time
import json
import shutil
import tempfile
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin
from typing import Optional

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

def save_ics_from_partial_response(partial_response: str, filename: str = "planning.ics"):
    # Chercher la portion JSON qui contient "events"
    match = re.search(r'\{ *"events" *: *\[.*?\]\}', partial_response, re.DOTALL)
    if not match:
        raise ValueError("Impossible de trouver les événements dans la réponse partielle")
    
    events_json = match.group(0)
    data = json.loads(events_json)
    events = data.get("events", [])

    cal = Calendar()
    cal.add("prodid", "-//Planning Export//")
    cal.add("version", "2.0")

    for ev in events:
        # On vérifie que les champs existent
        if "start" not in ev or "end" not in ev:
            continue

        dt_start = datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
        dt_end = datetime.fromisoformat(ev["end"].replace("Z", "+00:00"))

        ical_event = Event()
        ical_event.add("uid", ev.get("id"))
        ical_event.add("summary", ev.get("title", "Sans titre"))
        ical_event.add("dtstart", dt_start)
        ical_event.add("dtend", dt_end)
        ical_event.add("description", ev.get("className", ""))

        cal.add_component(ical_event)

    with open(filename, "wb") as f:
        f.write(cal.to_ical())
        
        
        
BASE = "https://onboard.ec-nantes.fr"
LOGIN_PAGE = BASE + "/faces/Login.xhtml"
MAINMENU_PAGE = BASE + "/faces/MainMenuPage.xhtml"
PLANNING_PAGE = BASE + "/faces/Planning.xhtml"

UA = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0"

# récupère mot de passe et user
password = os.environ.get("ONBOARD_PASS")
if not password:
    # Ne pas écraser l'ancien planning si ONBOARD_PASS manquant
    print("Erreur: la variable d'environnement ONBOARD_PASS n'est pas définie. Ajouter dans Settings → Secrets.")
    # Le script termine normalement sans remplacer l'ICS.
    sys.exit(0)

USERNAME = os.environ.get("ONBOARD_USER", "mseverac2023")

# session
session = requests.Session()
session.headers.update({"User-Agent": UA})
current_viewstate = None
new_id = None

# ---------- Helpers ----------
def save_debug_response(name, response_text):
    """Sauvegarde la réponse textuelle pour debug (HTML ou XML partial)."""
    fname = f"debug_response_{name}.html"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(response_text)
        print(f"-> debug saved: {fname} (len={len(response_text)})")
    except Exception as e:
        print(f"[WARN] impossible d'écrire debug file {fname}: {e}")

def extract_viewstate_from_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "javax.faces.ViewState"})
    if inp and inp.get("value"):
        return inp["value"]
    return None

def extract_viewstate_from_jsf_partial(xml_text: str) -> Optional[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for upd in root.findall('.//update'):
        id_attr = upd.get('id') or ''
        if 'ViewState' in id_attr or 'javax.faces.ViewState' in id_attr:
            return (upd.text or '').strip()
    return None

def ensure_success(r, context="request"):
    if r.status_code >= 400:
        print(f"[ERROR] {context} returned HTTP {r.status_code}")
        # sauvegarde debug
        try:
            save_debug_response(context, r.text)
        except:
            pass
        raise RuntimeError(f"HTTP {r.status_code} for {context}")

# ---------- ID replacement helpers ----------
def replace_id(payload: dict, schedule_id: str, old_id: str):
    print(f"[replace_id] remplacement automatique : {old_id} -> {schedule_id}")
    for key in list(payload.keys()):
        if old_id in key:
            new_key = key.replace(old_id, schedule_id)
            payload[new_key] = payload.pop(key)
    for key in list(payload.keys()):
        if isinstance(payload[key], str) and payload[key] == old_id:
            payload[key] = schedule_id

def get_old_id(payload: dict) -> Optional[str]:
    old_candidates = set()
    for v in payload.values():
        if isinstance(v, str):
            for m in re.findall(r'(form:j_idt\d+)', v):
                old_candidates.add(m)
    for k in list(payload.keys()):
        for m in re.findall(r'(form:j_idt\d+)', k):
            old_candidates.add(m)
    old_id = None
    if isinstance(payload.get("javax.faces.source"), str) and re.match(r'form:j_idt\d+', payload.get("javax.faces.source")):
        old_id = payload.get("javax.faces.source")
    elif old_candidates:
        old_id = next(iter(old_candidates))
    return old_id

# ---------- robust POST for JSF/PrimeFaces ----------
def requete_post(payload: dict, name: str, url: Optional[str]=None, ajax: bool=False, extra_headers: dict=None, pause: float=1.0):
    global current_viewstate, session, new_id

    if new_id is not None:
        old_id = get_old_id(payload)
        if old_id:
            replace_id(payload, new_id, old_id)

    if url is None:
        url = MAINMENU_PAGE

    # inject ViewState si absent
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
    print(f"payload before POST: keys={list(payload.keys())}")

    r = session.post(url, data=payload, headers=headers, allow_redirects=True)
    ensure_success(r, f"POST {name}")

    # Analysis: try XML partial first, else HTML fallback
    schedule_id = None
    collected_hidden = {}
    new_vs = None

    # try XML parse
    try:
        root = ET.fromstring(r.text)
        for upd in root.findall(".//update"):
            upd_id = upd.get("id") or ""
            upd_text = upd.text or ""
            m = re.search(r'id\s*:\s*"([^"]+)"', upd_text)
            if m:
                candidate = m.group(1)
                if re.match(r'form:j_idt\d+', candidate):
                    schedule_id = candidate
            if not schedule_id:
                m2 = re.search(r'(form:j_idt\d+)', upd_id)
                if m2:
                    schedule_id = m2.group(1)
            soup_upd = BeautifulSoup(upd_text, "html.parser")
            for inp in soup_upd.find_all("input", {"type": "hidden"}):
                name = inp.get("name") or inp.get("id")
                value = inp.get("value", "")
                if name:
                    collected_hidden[name] = value
        new_vs = extract_viewstate_from_jsf_partial(r.text)
    except ET.ParseError:
        # fallback to HTML parsing
        soup = BeautifulSoup(r.text, "html.parser")
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name") or inp.get("id")
            value = inp.get("value", "")
            if name:
                collected_hidden[name] = value
        m = re.search(r'PrimeFaces\.cw\("Schedule".*?id\s*:\s*"([^"]+)"', r.text)
        if m:
            schedule_id = m.group(1)
        else:
            m2 = re.search(r'<update id="(form:j_idt\d+)"', r.text)
            if m2:
                schedule_id = m2.group(1)
        new_vs = extract_viewstate_from_html(r.text) or extract_viewstate_from_jsf_partial(r.text)

    # merge collected hidden into payload if missing
    for k, v in collected_hidden.items():
        if k not in payload or not payload.get(k):
            payload[k] = v

    if new_vs:
        if new_vs != current_viewstate:
            print(f"[viewstate] mis à jour après {name} (len={len(new_vs)})")
        current_viewstate = new_vs
        payload["javax.faces.ViewState"] = current_viewstate

    # detect old_id and replace if schedule_id found
    old_candidates = set()
    for v in payload.values():
        if isinstance(v, str):
            for m in re.findall(r'(form:j_idt\d+)', v):
                old_candidates.add(m)
    for k in list(payload.keys()):
        for m in re.findall(r'(form:j_idt\d+)', k):
            old_candidates.add(m)
    old_id = None
    if isinstance(payload.get("javax.faces.source"), str) and re.match(r'form:j_idt\d+', payload.get("javax.faces.source")):
        old_id = payload.get("javax.faces.source")
    elif old_candidates:
        old_id = next(iter(old_candidates))

    if schedule_id:
        print("schedule_id détecté:", schedule_id)
        if old_id and old_id != schedule_id:
            new_id = schedule_id
            replace_id(payload, schedule_id, old_id)
        payload["javax.faces.source"] = schedule_id
        payload["javax.faces.partial.execute"] = schedule_id
        payload["javax.faces.partial.render"] = schedule_id
        payload[schedule_id] = schedule_id

    if pause:
        time.sleep(pause)

    print(f"POST {name} done (len response={len(r.text)})")
    return r

# ---------- ICS generation & safe write ----------
def generate_ics_from_partial_response(response_text: str) -> str:
    """
    Extrait le JSON présent dans une réponse partielle JSF (ex: '[{...}]') puis génère
    un texte ICS (string). Lève ValueError si JSON non trouvé ou format inattendu.
    """
    m = re.search(r'\[\{.*\}\]', response_text, re.DOTALL)
    if not m:
        raise ValueError("Impossible de trouver le JSON des événements dans la réponse.")
    events_json = m.group(0)
    events = json.loads(events_json)

    # Construction du ICS en string (on garde le format UTC natif)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Onboard//Planning//FR",
        "CALSCALE:GREGORIAN",
    ]

    # Certains payloads ont events encapsulés différemment : on assure robustesse
    # On suppose events est une liste, et events[0]["events"] contient la liste réelle
    evt_container = events[0].get("events") if isinstance(events, list) and events else None
    if evt_container is None:
        # peut-être events est déjà la liste d'événements
        if isinstance(events, list) and all(isinstance(x, dict) and "start" in x for x in events):
            evt_container = events
        else:
            raise ValueError("Format JSON inattendu pour les événements.")

    def to_ics_date(dt_str):
        # exemple: "2025-09-08T10:15:00+0200"
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z")
        return dt.strftime("%Y%m%dT%H%M%S")

    for ev in evt_container:
        uid = (ev.get("id", "") or "") + "@onboard.ec-nantes.fr"
        start = to_ics_date(ev["start"])
        end = to_ics_date(ev["end"])
        summary = (ev.get("title", "") or "").strip().replace("\n", " ").replace("\r", " ")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART;TZID=Europe/Paris:{start}",
            f"DTEND;TZID=Europe/Paris:{end}",
            f"SUMMARY:{summary}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\n".join(lines)

def write_ics_safely(ics_text: str, final_path="planning.ics"):
    """
    Écrit dans un fichier tmp, parse l'ICS avec icalendar et s'assure qu'il y a au moins 1 VEVENT,
    puis remplace final_path atomiquement. Lève ValueError en cas de pb de validation.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".ics.tmp")
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(ics_text)

        # Validation : parser l'ICS (binaire)
        with open(tmp_path, "rb") as f:
            try:
                cal = Calendar.from_ical(f.read())
            except Exception as e:
                raise ValueError(f"Parse ICS failed: {e}")

        has_event = any(comp.name == "VEVENT" for comp in cal.walk())
        if not has_event:
            raise ValueError("ICS parsed mais ne contient aucun VEVENT -> rejeté")

        # taille minimale (heuristique)
        if os.path.getsize(tmp_path) < 200:
            raise ValueError("ICS trop petit -> rejeté")

        # remplacement atomique
        shutil.move(tmp_path, final_path)
        print(f"✅ {final_path} mis à jour en atomique.")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except:
                pass

# ---------- Script principal ----------
def main():
    global current_viewstate, session, new_id
    try:
        print("GET login page...")
        r = session.get(LOGIN_PAGE)
        ensure_success(r, "GET login page")
        current_viewstate = extract_viewstate_from_html(r.text)
        if not current_viewstate:
            raise RuntimeError("Impossible de trouver ViewState sur la page de login.")
        print("ViewState login trouvé (len):", len(current_viewstate))

        # récupérer action du formulaire si présent
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form", id="formulaireSpring")
        if form and form.get("action"):
            login_post_url = urljoin(BASE, form["action"])
        else:
            login_post_url = LOGIN_PAGE
        print("Login POST URL:", login_post_url)

        # POST login
        login_payload = {
            "username": USERNAME,
            "password": password,
            "j_idt27": "",
        }
        r = requete_post(login_payload, "post_login", url=login_post_url, ajax=False)

        if ("Déconnexion" in r.text) or ("Mon compte" in r.text) or ("MainMenuPage" in r.text):
            print("✅ Login probablement réussi (mot-clé détecté).")
        else:
            print("Login response ne contient pas les mots-clés attendus, GET / pour confirmer...")
            r2 = session.get(BASE + "/")
            if "Déconnexion" in r2.text or "MainMenuPage" in r2.text:
                print("✅ Après GET /, on est connecté.")
                r = r2
            else:
                raise RuntimeError("❌ Login semble échouer — vérifier identifiants ou ViewState envoyé.")

        # GET MainMenuPage pour ViewState propre
        print("GET MainMenuPage...")
        r = session.get(MAINMENU_PAGE)
        ensure_success(r, "GET MainMenuPage")
        vs = extract_viewstate_from_html(r.text) or extract_viewstate_from_jsf_partial(r.text)
        if vs:
            current_viewstate = vs
            print("ViewState main trouvé (len):", len(current_viewstate))
        else:
            raise RuntimeError("Pas de ViewState trouvé sur MainMenuPage -> abort")

        # requête 1 : ouverture sous-menu (AJAX)
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
        }
        r = requete_post(payload1, "ajax_open_submenu", url=MAINMENU_PAGE, ajax=True)

        # navigation vers Planning
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

        # tu peux ajuster si besoin : heuristique initiale pour l'id
        new_id = "form:j_idt141"
        r = requete_post(payload2, "navigate_planning", url=MAINMENU_PAGE, ajax=False)

        print("Attente courte pour que la navigation prenne effet...")
        time.sleep(2)

        vs = extract_viewstate_from_html(r.text) or extract_viewstate_from_jsf_partial(r.text)
        if vs:
            current_viewstate = vs

        # GET Planning.xhtml pour récupérer tokens / inputs
        print("GET Planning.xhtml pour récupérer tokens si nécessaire...")
        r_planning = session.get(PLANNING_PAGE)
        ensure_success(r_planning, "GET Planning.xhtml")
        viewstate_planning = extract_viewstate_from_html(r_planning.text) or extract_viewstate_from_jsf_partial(r_planning.text)
        if not viewstate_planning:
            raise RuntimeError("Impossible de récupérer ViewState sur Planning.xhtml -> abort")
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

        # Fonction pour télécharger et générer ICS
        def dl_ics(date, week):
            # payload de téléchargement (tu peux ajuster timestamps si besoin)
            payload3 = {
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": "form:j_idt118",
                "javax.faces.partial.execute": "form:j_idt118",
                "javax.faces.partial.render": "form:j_idt118",
                "form:j_idt118": "form:j_idt118",
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
            # debug dump
            print("---------- server response start ----------")
            print(content[:2000])
            print("---------- server response end ----------")

            # tenter de générer ICS puis écrire atomiquement
            ics_text = generate_ics_from_partial_response(content)
            write_ics_safely(ics_text, final_path="planning.ics")

        # Les paramètres que tu utilises (ajuste si besoin)
        date1 = "15/09/2025"
        week1 = "38-2025"
        dl_ics(date1, week1)

        print("Fini.")
    except Exception as e:
        # En cas d'erreur, on logge et on ne remplace PAS l'ancien planning.ics
        print(f"[ERROR] get ICS failed : {e}")
        # sauvegarde d'un debug si possible
        try:
            save_debug_response("failure", str(e))
        except:
            pass
        # Ne pas échouer brutalement pour que GitHub Actions conserve l'ancien fichier
        return

if __name__ == '__main__':
    main()
