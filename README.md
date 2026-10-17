# Home Assistant Clap Sensor (HACS Integration & Add-on)

Erkennt **Doppelklatschen (`2x Klatschen`)** und **Dreifachklatschen (`3x Klatschen`)** über das Mikrofon einer USB-Webcam (z. B. am Proxmox-Server per USB-Passthrough an deine HAOS-VM durchgereicht) und stellt echte **Home-Assistant-Entitäten** (`binary_sensor` & `sensor`) sowie das Event `clap_detected` bereit.

Dieses Repository unterstützt **beide Installationswege ohne manuelles Kopieren von Dateien**:
1. **Per HACS (Custom Integration)**: Erstellt direkt ein Gerät *Clap Sensor (USB-Mikrofon)* mit fertigen Entitäten:
   - `binary_sensor.clap_sensor_doppelklatschen_2x` (schaltet bei 2x Klatschen für 2 Sek. auf `on`)
   - `binary_sensor.clap_sensor_dreifachklatschen_3x` (schaltet bei 3x Klatschen für 2 Sek. auf `on`)
   - `sensor.clap_sensor_letztes_klatsch_muster` & `sensor.clap_sensor_letzter_klatsch_snr`
2. **Per Home Assistant Add-on Store (Repository-URL)**: Installiert das eigenständige Add-on mit Mikrofon-Dropdown-Auswahl in der Home Assistant UI.

---

## 1. Installation über HACS (ohne Dateien manuell zu kopieren)

1. Öffne in Home Assistant **HACS**.
2. Klicke oben rechts auf das Drei-Punkte-Menü **⋮ -> Benutzerdefinierte Repositories (Custom repositories)**.
3. Füge die GitHub-URL dieses Repositories ein und wähle als Typ **Integration** (Kategorie: *Integration*).
4. Klicke auf **Hinzufügen**, suche in HACS nach **Clap Sensor (Double & Triple Clap)** und klicke auf **Herunterladen (Download)**.
5. **Starte Home Assistant neu**.
6. Gehe zu **Einstellungen -> Geräte & Dienste -> Integration hinzufügen** und suche nach **Clap Sensor**.
   - Im Einrichtungs-Dialog kannst du die Empfindlichkeit jederzeit über **Konfigurieren** anpassen!

---

## 2. Optional: Installation als Add-on über Repository-URL (ebenfalls ohne Samba/Kopieren)

Falls du zusätzlich (oder alternativ) das eigenständige HAOS-Add-on mit grafischer Mikrofon-Auswahl nutzen möchtest:
1. Öffne **Einstellungen -> Add-ons -> Add-on Store**.
2. Klicke oben rechts auf **⋮ -> Repositories**.
3. Füge dieselbe GitHub-URL ein und klicke auf **Hinzufügen**.
4. Installiere **Clap Sensor (Double & Triple Clap)** direkt aus dem Add-on Store und wähle unter **Konfiguration -> Audio -> Eingabe** deine USB-Webcam aus.
   *(Tipp: Wenn sowohl das Add-on als auch die HACS-Integration installiert sind, kannst du in der HACS-Integration als Eingabegerät `event_only` eintragen – dann empfängt die HACS-Integration automatisch die Erkennungen vom Add-on und schaltet deine `binary_sensor`-Entitäten!)*

---

## 3. USB-Webcam in Proxmox an die HAOS-VM durchreichen

1. Stecke die USB-Webcam in deinen Proxmox-Server (mit ca. 30–50 cm Abstand zum Lüfter / nicht direkt auf das vibrierende Gehäuse legen).
2. Öffne in **Proxmox** deine **HAOS-VM -> Hardware -> Add -> USB Device**.
3. Wähle **Use USB Vendor/Device ID** und wähle deine Webcam (z. B. *Logitech C270 HD WebCam*) aus.
   *(Kein Neustart der VM nötig – Proxmox verbindet das USB-Gerät sofort per Hotplug).*
