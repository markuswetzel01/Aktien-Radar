# -*- coding: utf-8 -*-
"""
Aktien-Radar: Relative Stärke nach Levy (RSL) und 4-Wochen-Momentum.

  RSL      = aktueller Schlusskurs / Durchschnitt der letzten 130 Schlusskurse
  Momentum = aktueller Schlusskurs / Schlusskurs vor 20 Handelstagen

Liest tickers.csv (ticker,name,region,immer); immer=ja umgeht den Liquiditätsfilter, lädt Kurse von Yahoo Finance und
schreibt aktien_radar.html (Charts) und aktien_radar.csv (Rangliste).
"""
import re
import sys
import urllib.request
from html.parser import HTMLParser
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf

# ---------------- Einstellungen ----------------
RSL_TAGE = 130            # Fenster für den Levy-Durchschnitt
MOMENTUM_TAGE = 20        # 4 Wochen = 20 Handelstage
MIN_UMSATZ_MIO_EUR = 10   # Liquiditätsfilter: mittlerer Tagesumsatz in Mio. EUR (0 = aus)
INDIZES_LADEN = ["sp500", "nasdaq100"]  # komplett von Wikipedia ergänzen ([] = nur tickers.csv)
KEINE_AKTIEN = {"ETFs", "Rohstoffe", "Währungen", "Krypto"}  # eigene Charts, nicht im Welt-Chart, kein Liquiditätsfilter
ETF_NAMEN_PRUEFEN = True  # zeigt nach dem Lauf, wie Yahoo die ETFs nennt (zur Kontrolle der Kürzel)
BREITE, HOEHE = 1150, 780 # Chartgröße in Pixeln
MAX_ALTER_TAGE = 7        # Aktien ohne Kurs in den letzten N Tagen überspringen
# -----------------------------------------------

ORDNER = Path(__file__).resolve().parent

# Börsenkürzel -> Handelswährung (für die Umrechnung des Umsatzes in EUR)
WAEHRUNG = {
    "DE": "EUR", "F": "EUR", "PA": "EUR", "AS": "EUR", "BR": "EUR", "MI": "EUR",
    "MC": "EUR", "HE": "EUR", "VI": "EUR", "LS": "EUR", "IR": "EUR",
    "SW": "CHF", "L": "GBp", "CO": "DKK", "ST": "SEK", "OL": "NOK",
    "T": "JPY", "HK": "HKD", "KS": "KRW", "KQ": "KRW", "TW": "TWD",
    "NS": "INR", "BO": "INR", "AX": "AUD", "TO": "CAD", "SI": "SGD",
    "SA": "BRL", "MX": "MXN", "JO": "ZAc", "SS": "CNY", "SZ": "CNY",
}

# Klick auf einen Punkt öffnet die Aktienseite in einem neuen Tab ({t} = Yahoo-Kürzel)
AKTIEN_URL = "https://de.finance.yahoo.com/quote/{t}/"
KLICK_JS = """
var gd = document.getElementById('{plot_id}');
gd.on('plotly_click', function(d) {
    var t = d.points[0].customdata[1];
    window.open('%s'.replace('{t}', encodeURIComponent(t)), '_blank');
});
""" % AKTIEN_URL

QUADRANTEN = {  # Name -> Farbe
    "Aufwärtstrend": "#1a9850",
    "Erholung": "#4575b4",
    "Rücksetzer": "#f39c12",
    "Abwärtstrend": "#d73027",
}


def waehrung_von(ticker):
    if "." not in ticker:
        return "USD"
    return WAEHRUNG.get(ticker.rsplit(".", 1)[1].upper())


INDIZES = {  # Name -> (Wikipedia-Seite, Mindestzahl erwarteter Zeilen)
    "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 400),
    "nasdaq100": ("https://en.wikipedia.org/wiki/Nasdaq-100", 80),
}


class _Tabellen(HTMLParser):
    """Liest alle Tabellen einer Seite als Listen von Zeilen."""
    def __init__(self):
        super().__init__()
        self.tabellen, self.tiefe, self.in_zelle = [], 0, False
        self.zeile, self.text = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tiefe += 1
            if self.tiefe == 1:
                self.tabellen.append({"id": dict(attrs).get("id"), "zeilen": []})
        elif self.tiefe == 1 and tag == "tr":
            self.zeile = []
        elif self.tiefe == 1 and tag in ("td", "th"):
            self.in_zelle, self.text = True, []

    def handle_endtag(self, tag):
        if tag == "table" and self.tiefe:
            self.tiefe -= 1
        elif self.tiefe == 1 and tag in ("td", "th"):
            self.zeile.append("".join(self.text).strip())
            self.in_zelle = False
        elif self.tiefe == 1 and tag == "tr" and self.zeile:
            self.tabellen[-1]["zeilen"].append(self.zeile)
            self.zeile = []

    def handle_data(self, data):
        if self.tiefe == 1 and self.in_zelle:
            self.text.append(data)


def _mitglieder(html, minimum):
    """Sucht die Tabelle mit Spalte Symbol/Ticker und liefert (Kürzel, Name)-Paare."""
    p = _Tabellen()
    p.feed(html)
    gesehen = []
    for tab in sorted(p.tabellen, key=lambda t: t["id"] != "constituents"):
        zeilen = tab["zeilen"]
        if len(zeilen) < 20:
            continue
        for k in range(min(3, len(zeilen))):             # Kopfzeile in den ersten Zeilen suchen
            kopf = [c.lower() for c in zeilen[k]]
            i_t = next((i for i, c in enumerate(kopf) if "symbol" in c or "ticker" in c), None)
            i_n = next((i for i, c in enumerate(kopf)
                        if i != i_t and any(w in c for w in ("security", "company", "name"))), None)
            if i_t is not None:
                break
        else:
            gesehen.append(f"{len(zeilen)} Zeilen, Kopf {zeilen[0][:4]}")
            continue
        if i_n is None:
            i_n = i_t
        paare = [(z[i_t], z[i_n]) for z in zeilen[k + 1:] if len(z) > max(i_t, i_n)
                 and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", z[i_t].split("[")[0].strip())]
        paare = [(t.split("[")[0].strip(), n) for t, n in paare]
        if len(paare) >= minimum:
            return paare
        gesehen.append(f"{len(zeilen)} Zeilen, Kopf {zeilen[k][:4]}, {len(paare)} Kürzel erkannt")
    raise ValueError("Mitgliedertabelle nicht gefunden; Tabellen: " + " / ".join(gesehen[:6]))


def index_liste(name):
    """Indexmitglieder von Wikipedia; bei Fehler die zuletzt gespeicherte Liste."""
    url, minimum = INDIZES[name]
    cache = ORDNER / f"{name}_cache.csv"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Aktien-Radar, privat)"})
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        paare = _mitglieder(html, minimum)
        df = pd.DataFrame({"ticker": [t.replace(".", "-") for t, _ in paare],
                           "name": [n for _, n in paare], "region": "USA"})
        df.to_csv(cache, index=False, encoding="utf-8-sig")
        print(f"{name}: {len(df)} Aktien von Wikipedia geladen.")
        return df
    except Exception as e:
        if cache.exists():
            print(f"{name}: Liste nicht abrufbar ({e}); nutze gespeicherte Liste.")
            return pd.read_csv(cache, dtype=str, encoding="utf-8-sig")
        print(f"{name}: Liste nicht abrufbar ({e}); nutze nur tickers.csv.")
        return pd.DataFrame(columns=["ticker", "name", "region"])


def lade_block(tickers):
    """Schlusskurse und Volumen als zwei Tabellen (Spalten = Ticker)."""
    daten = yf.download(tickers, period="1y", interval="1d", auto_adjust=True,
                        progress=False, threads=True, group_by="column")
    if daten is None or daten.empty:
        return pd.DataFrame(), pd.DataFrame()
    if not isinstance(daten.columns, pd.MultiIndex):  # einzelner Ticker, alte Version
        daten.columns = pd.MultiIndex.from_product([daten.columns, tickers[:1]])
    kurs = daten["Close"]
    vol = daten["Volume"] if "Volume" in daten.columns.get_level_values(0) else pd.DataFrame()
    return kurs, vol


def lade(tickers, block=150):
    """Lädt in Blöcken, damit Yahoo bei langen Listen nicht abbricht."""
    kurse, vols = [], []
    for i in range(0, len(tickers), block):
        k, v = lade_block(tickers[i:i + block])
        if not k.empty:
            kurse.append(k)
            vols.append(v)
    if not kurse:
        return pd.DataFrame(), pd.DataFrame()
    return pd.concat(kurse, axis=1, sort=True), pd.concat(vols, axis=1, sort=True)


def eur_kurse(waehrungen):
    """Wie viele Einheiten der Währung kostet 1 EUR? Fehlende Kurse -> nicht enthalten."""
    basis = {"GBp": "GBP", "ZAc": "ZAR"}
    brauche = sorted({basis.get(w, w) for w in waehrungen if w and w != "EUR"})
    kurse = {"EUR": 1.0}
    if not brauche:
        return kurse
    fx, _ = lade([f"EUR{w}=X" for w in brauche])
    for w in brauche:
        spalte = f"EUR{w}=X"
        if spalte in fx.columns and fx[spalte].dropna().size:
            kurse[w] = float(fx[spalte].dropna().iloc[-1])
    for klein, gross in basis.items():   # Pence/Cent = 1/100
        if gross in kurse:
            kurse[klein] = kurse[gross] * 100
    return kurse


def berechne(liste, kurs, vol, fx):
    zeilen, fehler = [], []
    heute = pd.Timestamp(datetime.now().date())
    for _, z in liste.iterrows():
        t = z["ticker"]
        if t not in kurs.columns:
            fehler.append((t, "keine Daten"))
            continue
        k = kurs[t].dropna()
        if len(k) < RSL_TAGE:
            fehler.append((t, f"nur {len(k)} Kurse (brauche {RSL_TAGE})"))
            continue
        if (heute - k.index[-1].tz_localize(None)).days > MAX_ALTER_TAGE:
            fehler.append((t, f"letzter Kurs vom {k.index[-1]:%d.%m.%Y}"))
            continue
        letzter = float(k.iloc[-1])
        rsl = letzter / float(k.iloc[-RSL_TAGE:].mean())
        mom = letzter / float(k.iloc[-1 - MOMENTUM_TAGE])

        umsatz = None
        w = waehrung_von(t)
        aktie = z["region"] not in KEINE_AKTIEN
        if aktie and t in vol.columns and w in fx:
            u = (kurs[t] * vol[t]).dropna().iloc[-20:]
            if len(u):
                umsatz = float(u.median()) / fx[w] / 1e6
        if MIN_UMSATZ_MIO_EUR and not z["immer"] and umsatz is not None and umsatz < MIN_UMSATZ_MIO_EUR:
            fehler.append((t, f"zu illiquide ({umsatz:.1f} Mio. EUR/Tag)"))
            continue

        if rsl >= 1 and mom >= 1:
            q = "Aufwärtstrend"
        elif rsl < 1 and mom >= 1:
            q = "Erholung"
        elif rsl >= 1:
            q = "Rücksetzer"
        else:
            q = "Abwärtstrend"
        zeilen.append({"Ticker": t, "Name": z["name"], "Region": z["region"],
                       "RSL": round(rsl, 4), "Momentum": round(mom, 4),
                       "Quadrant": q, "Kurs": round(letzter, 2),
                       "Umsatz_Mio_EUR": None if umsatz is None else round(umsatz, 1),
                       "Stand": k.index[-1].strftime("%d.%m.%Y")})
    df = pd.DataFrame(zeilen)
    if len(df):
        df = df.sort_values("RSL", ascending=False).reset_index(drop=True)
    return df, fehler


def labels_setzen(df, x0, x1, y0, y1):
    """Beschriftet Punkte nur dort, wo Platz ist. Auffällige Werte zuerst."""
    pw, ph = BREITE - 90, HOEHE - 150            # Zeichenfläche ohne Ränder
    px = (df["RSL"].values - x0) / (x1 - x0) * pw
    py = (y1 - df["Momentum"].values) / (y1 - y0) * ph
    namen = df["Name"].tolist()
    text, pos = [""] * len(df), ["middle right"] * len(df)
    belegt = []                                   # gesetzte Textkästen
    reihenfolge = np.argsort(-np.hypot((px - px.mean()) / pw, (py - py.mean()) / ph))
    for i in reihenfolge:
        b, h = len(namen[i]) * 6.4 + 6, 15
        kandidaten = [("middle right", px[i] + 7, py[i] - h / 2),
                      ("middle left", px[i] - 7 - b, py[i] - h / 2),
                      ("top center", px[i] - b / 2, py[i] - 7 - h),
                      ("bottom center", px[i] - b / 2, py[i] + 7)]
        for name, lx, ly in kandidaten:
            if lx < 0 or ly < 0 or lx + b > pw or ly + h > ph:
                continue
            andere = np.arange(len(df)) != i
            if np.any(andere & (px > lx - 5) & (px < lx + b + 5) & (py > ly - 5) & (py < ly + h + 5)):
                continue
            if any(lx < ex + eb and ex < lx + b and ly < ey + eh and ey < ly + h
                   for ex, ey, eb, eh in belegt):
                continue
            belegt.append((lx, ly, b, h))
            text[i], pos[i] = namen[i], name
            break
    return text, pos


def chart(df, titel):
    df = df.reset_index(drop=True)
    n_auf = int((df["Quadrant"] == "Aufwärtstrend").sum())
    dx = (df["RSL"].max() - df["RSL"].min()) or 0.1
    dy = (df["Momentum"].max() - df["Momentum"].min()) or 0.1
    x0, x1 = min(df["RSL"].min(), 0.98) - 0.06 * dx, max(df["RSL"].max(), 1.02) + 0.12 * dx
    y0, y1 = min(df["Momentum"].min(), 0.98) - 0.06 * dy, max(df["Momentum"].max(), 1.02) + 0.06 * dy
    df["_text"], df["_pos"] = labels_setzen(df, x0, x1, y0, y1)
    df["_umsatz"] = [f"{u:.1f}".replace(".", ",") + " Mio. EUR/Tag" if pd.notna(u) else "–" for u in df["Umsatz_Mio_EUR"]]
    fig = go.Figure()
    for q, farbe in QUADRANTEN.items():
        teil = df[df["Quadrant"] == q]
        fig.add_trace(go.Scatter(
            x=teil["RSL"], y=teil["Momentum"], mode="markers+text",
            name=f"{q} ({len(teil)})",
            text=teil["_text"].tolist(), textposition=teil["_pos"].tolist() or "middle right",
            textfont=dict(size=11), cliponaxis=False,
            marker=dict(size=8, color=farbe, line=dict(width=1, color="white")),
            customdata=teil[["Name", "Ticker", "Region", "_umsatz"]].values,
            hovertemplate="<b>%{customdata[0]}</b> (%{customdata[1]})<br>"
                          "%{customdata[2]}<br>RSL %{x:.3f}<br>Momentum %{y:.3f}<br>"
                          "Umsatz %{customdata[3]}<extra></extra>"))
    fig.add_vline(x=1, line_width=1.5, line_color="#222")
    fig.add_hline(y=1, line_width=1.5, line_color="#222")
    fig.update_xaxes(range=[x0, x1], title=f"RSL ({RSL_TAGE} Tage)")
    fig.update_yaxes(range=[y0, y1], title=f"Momentum ({MOMENTUM_TAGE} Tage)")
    fig.update_layout(
        title=dict(text=f"<b>{titel}: {n_auf} von {len(df)} im Aufwärtstrend</b>", font=dict(size=20)),
        template="plotly_white", width=BREITE, height=HOEHE, separators=",.",
        legend=dict(orientation="h", y=-0.1), margin=dict(l=60, r=30, t=70, b=80))
    return fig


def etf_namen_pruefen(df):
    """Gibt für jeden ETF den Namen aus, den Yahoo zum Kürzel führt."""
    etfs = df[df["Region"].isin(["ETFs", "Krypto"])]
    if etfs.empty:
        return
    print("Kürzel-Kontrolle ETFs und Krypto (mein Name | Name bei Yahoo):")
    for _, z in etfs.sort_values("Ticker").iterrows():
        try:
            info = yf.Ticker(z["Ticker"]).info or {}
            yahoo = info.get("longName") or info.get("shortName") or "?"
        except Exception:
            yahoo = "? (nicht abrufbar)"
        print(f"  {z['Ticker']}: {z['Name']} | {yahoo}")


def main():
    pfad = ORDNER / "tickers.csv"
    if not pfad.exists():
        sys.exit("tickers.csv fehlt im Ordner des Skripts.")
    liste = pd.read_csv(pfad, dtype=str, encoding="utf-8-sig").dropna(subset=["ticker"])
    liste["ticker"] = liste["ticker"].str.strip()
    for name in INDIZES_LADEN:
        liste = pd.concat([liste, index_liste(name)], ignore_index=True)
    if "immer" not in liste.columns:
        liste["immer"] = ""
    liste["immer"] = liste["immer"].fillna("").str.strip().str.lower().eq("ja")
    liste["name"] = liste["name"].fillna(liste["ticker"])
    liste["region"] = liste["region"].fillna("Sonstige")

    # "ROG.SW|RO.SW" = Ersatzkürzel, falls Yahoo das erste nicht kennt
    liste["ersatz"] = liste["ticker"].str.split("|").str[1:]
    liste["ticker"] = liste["ticker"].str.split("|").str[0].str.strip()
    liste = liste.drop_duplicates("ticker").reset_index(drop=True)

    print(f"Lade Kurse für {len(liste)} Aktien ...")
    kurs, vol = lade(list(liste["ticker"]))
    if kurs.empty:
        sys.exit("Keine Kursdaten erhalten. Internetverbindung prüfen oder später erneut versuchen.")
    for i, z in liste.iterrows():
        t = z["ticker"]
        if t in kurs.columns and kurs[t].dropna().size >= RSL_TAGE:
            continue
        for alt in [a.strip() for a in z["ersatz"] if a.strip()]:
            k, v = lade([alt])
            if alt in k.columns and k[alt].dropna().size >= RSL_TAGE:
                kurs = kurs.join(k[[alt]], how="outer")
                if alt in v.columns:
                    vol = vol.join(v[[alt]], how="outer")
                liste.at[i, "ticker"] = alt
                print(f"  {t} nicht verfügbar, nutze Ersatzkürzel {alt}.")
                break
    fx = eur_kurse({waehrung_von(t) for t in liste["ticker"]})
    fx = {w: k for w, k in fx.items() if k}

    df, fehler = berechne(liste, kurs, vol, fx)
    if df.empty:
        sys.exit("Keine Aktie hat die Kriterien erfüllt.")

    df.to_csv(ORDNER / "aktien_radar.csv", index=False, sep=";", decimal=",", encoding="utf-8-sig")

    aktien = df[~df["Region"].isin(KEINE_AKTIEN)]
    teile = [chart(aktien, "Aktien weltweit")] if len(aktien) else []
    for region in sorted(df["Region"].unique(), key=lambda r: (r in KEINE_AKTIEN, r)):
        teil = df[df["Region"] == region]
        if len(teil) >= 3 and len(teil) < len(df):
            teile.append(chart(teil, region))

    stand = datetime.now().strftime("%d.%m.%Y %H:%M")
    html = ["<!DOCTYPE html><html lang='de'><head><meta charset='utf-8'>",
            "<title>Aktien-Radar</title></head>",
            "<body style='font-family:Arial,sans-serif;max-width:1180px;margin:20px auto'>",
            "<h1>Aktien-Radar: RSL nach Levy und 4-Wochen-Momentum</h1>",
            f"<p>Erstellt {stand} · {len(df)} Werte · Quelle: Yahoo Finance, eigene Berechnung · "
            "Keine Anlageberatung.<br>Maus auf einen Punkt zeigt die Aktie, Klick öffnet ihre Seite bei Yahoo Finance.</p>"]
    for i, f in enumerate(teile):
        html.append(f.to_html(full_html=False, include_plotlyjs=(i == 0), post_script=KLICK_JS))
    if fehler:
        html.append("<h3>Übersprungen</h3><ul>" +
                    "".join(f"<li>{t}: {grund}</li>" for t, grund in fehler) + "</ul>")
    html.append("</body></html>")
    (ORDNER / "aktien_radar.html").write_text("\n".join(html), encoding="utf-8")

    print(f"Fertig: {len(df)} Werte ausgewertet, {len(fehler)} übersprungen.")
    for t, grund in fehler:
        print(f"  übersprungen: {t} ({grund})")
    if ETF_NAMEN_PRUEFEN:
        etf_namen_pruefen(df)
    print("Ergebnis: aktien_radar.html und aktien_radar.csv")


if __name__ == "__main__":
    main()
