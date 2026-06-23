import streamlit as st
import sqlite3
import pandas as pd
import datetime
import random
import time
import os
from PIL import Image
from streamlit_autorefresh import st_autorefresh

# ==========================================
# 1. KONFIGURATION
# ==========================================
st.set_page_config(page_title="Papierpost AG | Leitstand", layout="wide")

DB_FILE = os.environ.get("PLANSPIEL_DB", "planspiel_data.db")
DC_STATION_ID = 5
ORDER_LIFETIME = 90  # Sekunden bis Auftrag verfällt

PRODUCT_TYPES = {
    "A": {"env": "Weißer Umschlag", "paper": "Weißes Blatt", "color": "#F8F9FA"},
    "B": {"env": "Weißer Umschlag", "paper": "Rotes Blatt", "color": "#FFE5E5"},
    "C": {"env": "Roter Umschlag", "paper": "Weißes Blatt", "color": "#FFD6D6"},
    "D": {"env": "Roter Umschlag", "paper": "Rotes Blatt", "color": "#FFB3B3"}
}

ORDER_SEQUENCE = [
    "C", "B", "D", "A", "B", "C", "A", "D", "B", "C", "D", "A",
    "C", "B", "A", "D", "C", "B", "D", "A", "B", "C", "D", "A",
    "C", "B", "D", "B", "A"
]

# HINWEIS: Der GETEILTE Simulationszustand (Modus, läuft/gestoppt, Marktaufträge,
# Produktionsaufträge, Timer) liegt jetzt in der DB – NICHT mehr in st.session_state.
# Nur rein lokale UI-Dinge dürfen weiter in session_state stehen.


# ==========================================
# 2. DATENBANK LOGIK
# ==========================================
def get_connection():
    # timeout hilft gegen "database is locked" bei mehreren gleichzeitigen Nutzern
    return sqlite3.connect(DB_FILE, timeout=10)


def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, start_time TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS process_log 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, station_id INTEGER, timestamp TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders_log 
                 (order_id INTEGER PRIMARY KEY, product_type TEXT, created_at TIMESTAMP, served_at TIMESTAMP, status TEXT)''')

    # --- NEU: geteilter Zustand ---
    c.execute('''CREATE TABLE IF NOT EXISTS sim_state (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS market_orders 
                 (slot INTEGER PRIMARY KEY, order_id INTEGER, product_type TEXT, 
                  created_at REAL, po_sent INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS production_orders 
                 (po_id INTEGER PRIMARY KEY, product_type TEXT, source_order_id INTEGER, sent_at TEXT)''')

    # Standardwerte nur anlegen, falls noch nicht vorhanden
    defaults = {
        'sim_running': '0',
        'sim_mode': 'Push',
        'last_order_time': '0',
        'next_interval': str(random.randint(25, 35)),
        'order_sequence_index': '0',
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO sim_state (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()


# --- Helfer für den geteilten Zustand ---
def get_state(key, default=None, cast=str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM sim_state WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    if row is None:
        return default
    return cast(row[0])


def set_state(key, value):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO sim_state (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def is_running():
    return get_state('sim_running', '0') == '1'


def get_mode():
    return get_state('sim_mode', 'Push')


def load_market_orders():
    """Marktaufträge als Dict slot -> order aus der DB laden."""
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM market_orders", conn)
    conn.close()
    orders = {}
    for _, r in df.iterrows():
        orders[int(r['slot'])] = {
            "type": r['product_type'],
            "id": int(r['order_id']),
            "created_at": float(r['created_at']),
            "po_sent": bool(r['po_sent']),
        }
    return orders


def reset_simulation():
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    init_db()
    st.rerun()


def generate_order_logic():
    conn = get_connection()
    c = conn.cursor()

    # Freien Slot finden (1-3) aus der DB
    c.execute("SELECT slot FROM market_orders")
    occupied = {row[0] for row in c.fetchall()}
    free_slots = [s for s in [1, 2, 3] if s not in occupied]
    if not free_slots:
        conn.close()
        return  # Alle Slots belegt

    slot = random.choice(free_slots)
    new_id = random.randint(10000, 99999)

    # Nächsten Auftragstyp aus der Sequenz holen (mit Wiederholung von vorne)
    raw_idx = get_state('order_sequence_index', '0', int)
    p_key = ORDER_SEQUENCE[raw_idx % len(ORDER_SEQUENCE)]
    set_state('order_sequence_index', raw_idx + 1)

    now = datetime.datetime.now()
    created_epoch = time.time()

    c.execute("INSERT INTO orders_log (order_id, product_type, created_at, status) VALUES (?, ?, ?, ?)",
              (new_id, p_key, now, 'offen'))
    c.execute("INSERT INTO market_orders (slot, order_id, product_type, created_at, po_sent) VALUES (?, ?, ?, ?, 0)",
              (slot, new_id, p_key, created_epoch))
    conn.commit()
    conn.close()


def update_market_orders():
    now = time.time()

    # Abgelaufene Aufträge entfernen (Slots bleiben leer – kein Nachrücken)
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT slot, order_id, created_at FROM market_orders")
    for slot, order_id, created_at in c.fetchall():
        if now - created_at >= ORDER_LIFETIME:
            c.execute("DELETE FROM market_orders WHERE slot=?", (slot,))
            c.execute("UPDATE orders_log SET status='verfallen' WHERE order_id=?", (order_id,))
    conn.commit()
    conn.close()

    # Neuen Auftrag generieren
    if is_running():
        last = get_state('last_order_time', '0', float)
        interval = get_state('next_interval', '30', int)
        if last == 0 or (now - last >= interval):
            generate_order_logic()
            set_state('last_order_time', now)
            set_state('next_interval', random.randint(30, 40))


init_db()

# ==========================================
# 3. SIDEBAR (NUR NAVIGATION & LOGO)
# ==========================================
st.sidebar.title("Papierpost AG")
try:
    st.sidebar.image(Image.open("Logo_Papierpost.png"), use_container_width=True)
except Exception:
    st.sidebar.warning("Logo fehlt")

st.sidebar.markdown("---")
view = st.sidebar.radio("Navigation:",
                        ["📊 Dashboard", "🏭 Station A", "🏭 Station B", "🏭 Station C", "🏭 Station D", "📦 DC"])

# Statusanzeige klein in der Sidebar (jetzt aus der DB → für alle gleich)
st.sidebar.markdown("---")
if is_running():
    st.sidebar.success("🟢 Simulation läuft")
else:
    st.sidebar.error("🔴 Simulation gestoppt")
st.sidebar.caption(f"Modus: **{get_mode()}**")

# ==========================================
# 4. DASHBOARD (MIT STEUERUNG)
# ==========================================
if view == "📊 Dashboard":
    st.title("📊 Leitstand & KPI Dashboard")

    running = is_running()
    mode = get_mode()

    # --- STEUERKONSOLE ---
    with st.container():
        st.subheader("🛠️ Simulation-Steuerung")

        # Modus-Auswahl (Push / Pull) – nur änderbar wenn Simulation gestoppt
        selected_mode = st.radio(
            "Steuerungsmodus:",
            ["Push", "Pull"],
            index=0 if mode == "Push" else 1,
            horizontal=True,
            disabled=running,
            help="Push: Produktion treibt den Materialfluss. "
                 "Pull: Das DC löst per Knopfdruck Produktionsaufträge für Station 1 aus."
        )
        # Modus in die DB schreiben (für ALLE sichtbar), nur wenn gestoppt und geändert
        if not running and selected_mode != mode:
            set_state('sim_mode', selected_mode)
            st.rerun()

        c_start, c_stop, c_reset = st.columns(3)

        if not running:
            if c_start.button("▶️ START", type="primary", use_container_width=True):
                set_state('sim_running', '1')
                set_state('last_order_time', '0')
                update_market_orders()
                st.rerun()
        else:
            if c_stop.button("🛑 STOPP", use_container_width=True):
                set_state('sim_running', '0')
                st.rerun()

        if c_reset.button("🗑️ RESET (Alle Daten löschen)", use_container_width=True):
            reset_simulation()

    st.markdown("---")

    # --- KPI BERECHNUNG ---
    conn = get_connection()
    items_df = pd.read_sql_query("SELECT id, julianday(start_time) AS start_jd FROM items", conn)
    logs_df = pd.read_sql_query("SELECT item_id, station_id, julianday(timestamp) AS time_jd FROM process_log", conn)
    orders_df = pd.read_sql_query(
        "SELECT *, julianday(created_at) as start_jd, julianday(served_at) as end_jd FROM orders_log", conn)
    conn.close()

    if not items_df.empty:
        # 1. Durchlaufzeit (LT) S1 -> DC
        finished_s5 = logs_df[logs_df['station_id'] == DC_STATION_ID]
        merged_lt = items_df.merge(finished_s5, left_on='id', right_on='item_id')
        if not merged_lt.empty:
            merged_lt['lt_sec'] = (merged_lt['time_jd'] - merged_lt['start_jd']) * 24 * 60 * 60
            avg_lt_total = merged_lt['lt_sec'].mean()
        else:
            avg_lt_total = 0

        # 2. WIP & Ø WIP
        wip_current = len(items_df) - len(finished_s5)
        all_events = pd.concat([
            items_df[['start_jd']].assign(c=1),
            finished_s5[['time_jd']].assign(c=-1).rename(columns={'time_jd': 'start_jd'})
        ]).sort_values('start_jd')
        wip_avg = all_events['c'].cumsum().mean() if not all_events.empty else 0

        # 3. Lagerbestand (Inventory)
        finished_s4 = logs_df[logs_df['station_id'] == 4]
        served_orders = orders_df[orders_df['status'] == 'bedient']
        inventory_current = len(finished_s4) - len(served_orders)
        inv_events = pd.concat([
            finished_s4[['time_jd']].assign(c=1),
            served_orders[['end_jd']].assign(c=-1).rename(columns={'end_jd': 'time_jd'})
        ]).sort_values('time_jd')
        inventory_avg = inv_events['c'].cumsum().mean() if not inv_events.empty else 0

        # 4. Auftrags-KPIs
        lost_orders = len(orders_df[orders_df['status'] == 'verfallen'])
        avg_serve_time = (served_orders['end_jd'] - served_orders[
            'start_jd']).mean() * 24 * 60 * 60 if not served_orders.empty else 0

        # --- DARSTELLUNG ---
        kpi1, kpi2, kpi3 = st.columns(3)
        kpi1.metric("WIP (Aktuell | Ø)", f"{wip_current} | {wip_avg:.1f}")
        kpi2.metric("Lagerbestand (Aktuell | Ø)", f"{inventory_current} | {inventory_avg:.1f}")
        kpi3.metric("Ø Durchlaufzeit (S1-DC)", f"{avg_lt_total:.1f} s")

        st.markdown("---")
        kpi4, kpi5, kpi6 = st.columns(3)
        kpi4.metric("Bediente Aufträge", len(served_orders))
        kpi5.metric("Ø Zeit bis Bedienung", f"{avg_serve_time:.1f} s")
        kpi6.metric("Verfallene Aufträge", lost_orders, delta=lost_orders, delta_color="inverse")
    else:
        st.info("Simulation im Dashboard starten, um Daten zu erfassen.")

# ==========================================
# 5. DISTRIBUTION CENTER (DC)
# ==========================================
elif view == "📦 DC":
    update_market_orders()
    st.title("📦 Distribution Center")

    # Auto-Refresh alle 10 Sekunden
    st_autorefresh(interval=10000, limit=None, key="dc_autorefresh")

    # Aufgabenbeschreibung
    st.info("**Aufgabe:** ID im Lager einchecken, Brief einlagern, parallel Kundenaufträge bearbeiten.")

    st.subheader("Aktuelle Marktaufträge")
    cols = st.columns(3)
    now_ts = time.time()

    orders = load_market_orders()
    mode = get_mode()

    for slot_number in [1, 2, 3]:
        col_index = slot_number - 1  # Slot 1 → links, Slot 2 → mitte, Slot 3 → rechts
        with cols[col_index]:
            # Slot-Header
            st.markdown(
                f'<div style="text-align:center; font-weight:bold; background-color:#333; '
                f'color:white; padding:5px; border-radius:5px 5px 0 0;">SLOT {slot_number}</div>',
                unsafe_allow_html=True)

            if slot_number in orders:
                order = orders[slot_number]
                p = PRODUCT_TYPES[order['type']]
                elapsed = now_ts - order["created_at"]
                remaining = max(0, ORDER_LIFETIME - elapsed)

                # Timer-Balken: 10 Stufen
                steps_total = 10
                steps_remaining = int((remaining / ORDER_LIFETIME) * steps_total)
                steps_remaining = max(0, min(steps_total, steps_remaining))

                # Farbe: grün → gelb → rot
                if steps_remaining > 6:
                    bar_color = "#28a745"
                elif steps_remaining > 3:
                    bar_color = "#ffc107"
                else:
                    bar_color = "#dc3545"

                filled = "█" * steps_remaining
                empty = "░" * (steps_total - steps_remaining)
                bar_text = filled + empty

                st.markdown(f"""
                    <div style="background-color:{p['color']}; padding:15px; border:2px solid #333;
                         border-top:none; border-radius:0 0 10px 10px; color:black; min-height:160px;">
                        <h2 style="margin:0; text-align:center;">Typ {order['type']}</h2>
                        <hr style="border:0.5px solid #333;">
                        <p style="font-size:1.1em; margin:10px 0;">
                            <b>✉️ Umschlag:</b> {p['env']}<br>
                            <b>📄 Blatt:</b> {p['paper']}
                        </p>
                        <div style="font-family:monospace; font-size:1.4em; color:{bar_color};
                             letter-spacing:3px; text-align:center;" title="{remaining:.0f}s verbleibend">
                            {bar_text}
                        </div>
                        <div style="text-align:center; font-size:0.85em; color:#555; margin-top:4px;">
                            ⏱ noch {remaining:.0f}s
                        </div>
                    </div>
                """, unsafe_allow_html=True)

                if st.button(f"Bedienen {order['id']}", key=f"sv_{order['id']}", use_container_width=True):
                    conn = get_connection()
                    c = conn.cursor()
                    c.execute("UPDATE orders_log SET status='bedient', served_at=? WHERE order_id=?",
                              (datetime.datetime.now(), order['id']))
                    c.execute("DELETE FROM market_orders WHERE slot=?", (slot_number,))
                    conn.commit()
                    conn.close()
                    st.rerun()

                # --- PULL-MODUS: Produktionsauftrag an Station 1 senden ---
                if mode == "Pull":
                    if order.get("po_sent", False):
                        st.button("✅ Produktionsauftrag gesendet",
                                  key=f"po_{order['id']}", use_container_width=True, disabled=True)
                    else:
                        if st.button("📤 Produktionsauftrag senden",
                                     key=f"po_{order['id']}", use_container_width=True):
                            conn = get_connection()
                            c = conn.cursor()
                            c.execute(
                                "INSERT INTO production_orders (po_id, product_type, source_order_id, sent_at) "
                                "VALUES (?, ?, ?, ?)",
                                (random.randint(100000, 999999), order["type"], order["id"],
                                 datetime.datetime.now().strftime("%H:%M:%S")))
                            c.execute("UPDATE market_orders SET po_sent=1 WHERE slot=?", (slot_number,))
                            conn.commit()
                            conn.close()
                            st.rerun()
            else:
                st.markdown(
                    '<div style="border:2px dashed #ccc; border-top:none; padding:50px; '
                    'text-align:center; color:#ccc; border-radius:0 0 10px 10px; min-height:160px;">'
                    'Warten...</div>',
                    unsafe_allow_html=True)

    st.markdown("---")
    st.subheader("📥 DC Check-In (WIP Ende)")
    id_in = st.text_input("Baugruppen-ID scannen:", key="dc_in").strip().upper()
    if st.button("Check-In Bestätigen", type="primary", use_container_width=True):
        if id_in:
            conn = get_connection()
            c = conn.cursor()
            # Prüfen, ob schon im DC (Doppelbuchung)
            c.execute("SELECT id FROM process_log WHERE item_id=? AND station_id=5", (id_in,))
            if c.fetchone():
                st.warning(f"⚠️ Baugruppe {id_in} liegt bereits im DC (Doppelbuchung).")
            else:
                # Prüfen, ob an Station 4
                c.execute("SELECT id FROM process_log WHERE item_id=? AND station_id=4", (id_in,))
                if c.fetchone():
                    c.execute("INSERT INTO process_log (item_id, station_id, timestamp) VALUES (?, 5, ?)",
                              (id_in, datetime.datetime.now()))
                    conn.commit()
                    st.success(f"✅ {id_in} erfolgreich ins Lager aufgenommen.")
                else:
                    st.error("Fehler: Station 4 fehlt!")
            conn.close()

# ==========================================
# 6. STATIONEN
# ==========================================
elif view == "🏭 Station A":
    st.title("🏭 Station A (Start)")
    st.info(
        "**Aufgabe:** ID einchecken, Brief auswählen, stempeln, Produktart kennzeichnen, ID auf Umschlag schreiben, an Station B weitergeben.")

    # --- PULL-MODUS: Produktionsauftrags-Liste vom DC (aus der DB) ---
    if get_mode() == "Pull":
        # Auto-Refresh, damit neue Produktionsaufträge ohne manuelles Neuladen erscheinen
        st_autorefresh(interval=10000, limit=None, key="stationA_autorefresh")

        st.subheader("📋 Produktionsaufträge (vom DC)")
        conn = get_connection()
        pos_df = pd.read_sql_query("SELECT * FROM production_orders ORDER BY po_id", conn)
        conn.close()

        if not pos_df.empty:
            for _, po in pos_df.iterrows():
                p = PRODUCT_TYPES[po["product_type"]]
                c_info, c_btn = st.columns([4, 1])
                with c_info:
                    st.markdown(f"""
                        <div style="background-color:{p['color']}; padding:10px; border:2px solid #333;
                             border-radius:8px; color:black; margin-bottom:5px;">
                            <b>Typ {po['product_type']}</b> &nbsp;|&nbsp; ✉️ {p['env']} &nbsp;|&nbsp; 📄 {p['paper']}
                            <span style="float:right; color:#555;">⏱ eingegangen {po['sent_at']}</span>
                        </div>
                    """, unsafe_allow_html=True)
                with c_btn:
                    if st.button("✅ Erledigt", key=f"po_done_{int(po['po_id'])}", use_container_width=True):
                        conn = get_connection()
                        c = conn.cursor()
                        c.execute("DELETE FROM production_orders WHERE po_id=?", (int(po['po_id']),))
                        conn.commit()
                        conn.close()
                        st.rerun()
        else:
            st.caption("Keine offenen Produktionsaufträge. Warten auf Signal vom DC ...")
        st.markdown("---")

    id_in = st.text_input("ID vergeben:", key="s1_id").strip().upper()
    if st.button("Starten", type="primary"):
        if id_in:
            conn = get_connection()
            c = conn.cursor()
            now = datetime.datetime.now()
            try:
                c.execute("INSERT INTO items VALUES (?,?)", (id_in, now))
                c.execute("INSERT INTO process_log (item_id, station_id, timestamp) VALUES (?,1,?)", (id_in, now))
                conn.commit()
                st.success(f"✅ {id_in} gestartet!")
            except Exception:
                st.error("⚠️ Diese ID existiert bereits!")
            finally:
                conn.close()
        else:
            st.warning("ID eingeben!")

elif view in ["🏭 Station B", "🏭 Station C", "🏭 Station D"]:
    letter = view[-1]                       # "B", "C" oder "D"
    s_id = {"B": 2, "C": 3, "D": 4}[letter] # interne Stations-ID für die DB
    st.title(view)

    station_tasks = {
        2: "**Aufgabe:** ID einchecken, Papier auswählen, falten, in Umschlag stecken, an Station C weitergeben.",
        3: "**Aufgabe:** ID einchecken, Absenderadresse auf Umschlag schreiben, Umschlag zukleben, an Station D weitergeben.",
        4: "**Aufgabe:** ID einchecken, Brief versiegeln, ans Distribution Center weitergeben."
    }
    st.info(station_tasks.get(s_id))

    id_in = st.text_input(f"ID an Station {s_id} einchecken:", key=f"s{s_id}_id").strip().upper()
    if st.button("Check-In Bestätigen"):
        if id_in:
            conn = get_connection()
            c = conn.cursor()
            # Prüfen, ob schon hier eingecheckt (Doppelbuchung)
            c.execute("SELECT id FROM process_log WHERE item_id=? AND station_id=?", (id_in, s_id))
            if c.fetchone():
                st.warning(f"⚠️ Baugruppe {id_in} wurde hier bereits eingecheckt!")
            else:
                # Prüfen, ob vorherige Station erledigt ist
                c.execute("SELECT id FROM process_log WHERE item_id=? AND station_id=?", (id_in, s_id - 1))
                if c.fetchone():
                    c.execute("INSERT INTO process_log (item_id, station_id, timestamp) VALUES (?,?,?)",
                              (id_in, s_id, datetime.datetime.now()))
                    conn.commit()
                    st.success(f"✅ {id_in} weitergegeben.")
                else:
                    st.error(f"Fehler: Check-In an Station {s_id - 1} fehlt!")
            conn.close()
