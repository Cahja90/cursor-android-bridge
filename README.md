# handy cursor

Android-Handy als Alternative zur iOS-Version von Cursor. Nur für meine Bedürfnisse gemacht.

Brücke vom Android-Handy zum Cursor-Agent auf diesem Windows-PC. Im Chrome des Handys liegt eine dunkle Chat-Seite. Dazu wählst du ein Desktop-Fenster, dessen Bild live auf dem Handy erscheint.

Was du auf dem Handy schreibst, wird in das gewählte Fenster auf dem PC getippt und dort gestartet. Das Bild dieses Fensters bleibt auf dem Handy.

## voraussetzungen

- Python 3 auf dem PC, plus Pillow (`py -3 -m pip install pillow`)
- Cursor-CLI. Falls `agent` fehlt:

```powershell
irm 'https://cursor.com/install?win32=true' | iex
```

- einmalig auf dem PC angemeldet: `agent login`
- Handy im gleichen WLAN
- Chrome auf dem Handy

## server starten

Nach der Windows-Anmeldung startet die Aufgabe `handy_cursor` den Wächter `watch_desk.py`. Sobald Cursor läuft, startet er die Brücke. Hört schon etwas auf Port 8765, bleibt diese eine Kopie.

Von Hand, im Ordner dieses Projekts:

```powershell
py -3 desk_server.py
```

Anderer Projektordner:

```powershell
py -3 desk_server.py --workspace D:\mein\projekt
```

Die Konsole zeigt die lokale Adresse, die LAN-Adresse und, falls Tailscale läuft, die `100.x`-Adresse. Der PC muss wach sein und Cursor muss laufen. Die Brücke bleibt dann von selbst an.

## token

Jede Anfrage braucht ein Token. Der Server schreibt es in `.desk_token` (nicht ins Git legen, nicht weitergeben). Dieselbe Zeichenkette steht in der Adresse, die die Konsole ausgibt.

So kommt das Token an:

- in der Adresse als `?token=`
- als Header `X-Desk-Token`
- als Cookie `desk_token`, gesetzt beim Öffnen der gültigen Adresse

Ohne Token antwortet die Seite mit Hinweis, die API mit einem Fehler.

Der Server hört auf `0.0.0.0` Port `8765`, damit das Handy im eigenen Netz rankommt. Es wird kein Port ins öffentliche Internet freigegeben. Wer das Token nicht hat, kommt nicht an Chat oder Bild.

## android handy

1. Auf dem Startbildschirm das Symbol **Cursor** antippen. Die Seite öffnet sich ohne Browser-Leiste.
2. Unter der Leiste ein Fenster wählen. Das Bild läuft mit, solange das Fenster gewählt ist. Zwei Finger zoomen das Bild, ein Finger verschiebt es. Der Chat darunter zoomt nicht mit.
3. Unten Nachricht schreiben. Modus: Agent, Plan oder Fragen. Senden. Der Text erscheint in diesem Fenster auf dem PC.
4. Jedes Fenster hat seinen eigenen Chat. Wechselst du das Fenster, wechselt der Verlauf. Nach drei Tagen wird ein Chat gelöscht.
5. Wenn der Agent etwas ausführen will, erscheinen die Knöpfe erlauben und ablehnen.

Die Adresse im Heimnetz steht in der Konsole. Dieselbe Seite liegt zusätzlich unter https auf Port 8766. Chrome installiert auf einer reinen http-LAN-Adresse keine App, weil dort kein Dienst-Worker erlaubt ist. Das Symbol auf dem Startbildschirm ist deshalb eine kleine Vollbild-App, die das Zertifikat der Brücke selbst kennt und die Seite als App öffnet.

Die App fragt nicht erneut nach einem Symbol. Das Symbol **Cursor** auf dem Startbildschirm öffnet die Seite.

Für andere läuft die App im WLAN über die LAN-Adresse aus der Konsole. Der Laptop muss an sein und Cursor muss laufen. Eine eigene Adresse für unterwegs bleibt in der privaten Konfiguration.
