from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests

from detector import ClapEvent

logger = logging.getLogger("clap_sensor")


class HomeAssistantDispatcher:
    """
    Sendet erkannte Klatsch-Ereignisse (2x = double_clap, 3x = triple_clap)
    asynchron an Home Assistant:
    1. Automatisch über die interne Home Assistant Supervisor API (wenn als HA-Add-on gestartet).
       - Feuert immer das Event `clap_detected` auf den Home Assistant Event-Bus.
       - Schaltet optional direkt `target_entity_id` (bei 2x Klatschen) bzw.
         `triple_clap_entity_id` (bei 3x Klatschen).
    2. Optional zusätzlich oder alternativ über eine Webhook-URL (`POST /api/webhook/<id>`).
    """

    SUPERVISOR_BASE_URL = "http://supervisor/core/api"

    def __init__(self, options: Dict[str, Any]) -> None:
        self.options = options
        self.supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
        self.ha_token = str(options.get("ha_token", "") or "").strip()
        self.ha_base_url = str(options.get("ha_base_url", "") or "").strip().rstrip("/")
        self.webhook_url = str(options.get("webhook_url", "") or "").strip()

        self.target_entity_id = str(options.get("target_entity_id", "") or "").strip()
        self.target_service = str(
            options.get("target_service", "homeassistant.toggle") or "homeassistant.toggle"
        ).strip()

        self.triple_clap_entity_id = str(options.get("triple_clap_entity_id", "") or "").strip()
        self.triple_clap_service = str(
            options.get("triple_clap_service", "homeassistant.toggle") or "homeassistant.toggle"
        ).strip()

    @property
    def is_supervisor_available(self) -> bool:
        return bool(self.supervisor_token)

    def dispatch(self, event: ClapEvent, async_exec: bool = True) -> None:
        if async_exec:
            t = threading.Thread(target=self._dispatch_sync, args=(event,), daemon=True)
            t.start()
        else:
            self._dispatch_sync(event)

    def _dispatch_sync(self, event: ClapEvent) -> None:
        payload = {
            "pattern": event.pattern,
            "claps": event.claps,
            "peak_amplitude": round(event.peak_amplitude, 4),
            "snr_db": round(event.snr_db, 1),
            "high_freq_ratio": round(event.high_freq_ratio, 2),
            "timestamp": int(time.time()),
        }

        dispatched_any = False

        # 1. Home Assistant Supervisor API (innerhalb des HAOS-Add-ons)
        if self.supervisor_token:
            dispatched_any = True
            self._fire_ha_event(
                base_url=self.SUPERVISOR_BASE_URL,
                token=self.supervisor_token,
                event_type="clap_detected",
                payload=payload,
            )
            self._trigger_configured_entity(
                base_url=self.SUPERVISOR_BASE_URL,
                token=self.supervisor_token,
                event=event,
            )

        # 2. Externe Home Assistant REST API (falls außerhalb von HAOS mit Token genutzt)
        elif self.ha_base_url and self.ha_token:
            dispatched_any = True
            api_url = f"{self.ha_base_url}/api"
            self._fire_ha_event(
                base_url=api_url,
                token=self.ha_token,
                event_type="clap_detected",
                payload=payload,
            )
            self._trigger_configured_entity(
                base_url=api_url,
                token=self.ha_token,
                event=event,
            )

        # 3. Webhook-URL (funktioniert sowohl lokal als auch extern ohne Token)
        if self.webhook_url:
            dispatched_any = True
            try:
                resp = requests.post(self.webhook_url, json=payload, timeout=4.0)
                logger.info(
                    "Webhook ausgelöst (%s) -> HTTP %s",
                    self.webhook_url,
                    resp.status_code,
                )
            except Exception as exc:
                logger.error("Fehler beim Senden an Webhook (%s): %s", self.webhook_url, exc)

        if not dispatched_any:
            logger.info(
                "Hinweis: Kein SUPERVISOR_TOKEN oder webhook_url aktiv. Event-Payload: %s",
                payload,
            )

    def _fire_ha_event(
        self,
        base_url: str,
        token: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        url = f"{base_url}/events/{event_type}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=4.0)
            if resp.ok:
                logger.info(
                    "Home Assistant Event '%s' erfolgreich gesendet (claps=%d, pattern=%s)",
                    event_type,
                    payload["claps"],
                    payload["pattern"],
                )
            else:
                logger.warning(
                    "Home Assistant Event '%s' antwortete mit HTTP %s: %s",
                    event_type,
                    resp.status_code,
                    resp.text,
                )
        except Exception as exc:
            logger.error("Fehler beim Senden des Home Assistant Events: %s", exc)

    def _trigger_configured_entity(
        self,
        base_url: str,
        token: str,
        event: ClapEvent,
    ) -> None:
        if event.claps == 2 and self.target_entity_id:
            entity_id = self.target_entity_id
            service_spec = self.target_service
        elif event.claps == 3 and self.triple_clap_entity_id:
            entity_id = self.triple_clap_entity_id
            service_spec = self.triple_clap_service
        else:
            return

        if "." in service_spec:
            domain, service = service_spec.split(".", 1)
        else:
            domain, service = "homeassistant", service_spec

        url = f"{base_url}/services/{domain}/{service}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {"entity_id": entity_id}
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=4.0)
            if resp.ok:
                logger.info(
                    "Aktion '%s.%s' für '%s' erfolgreich ausgeführt!",
                    domain,
                    service,
                    entity_id,
                )
            else:
                logger.warning(
                    "Service-Aufruf '%s.%s' für '%s' fehlgeschlagen (HTTP %s): %s",
                    domain,
                    service,
                    entity_id,
                    resp.status_code,
                    resp.text,
                )
        except Exception as exc:
            logger.error(
                "Fehler beim Aufruf von '%s.%s' für '%s': %s",
                domain,
                service,
                entity_id,
                exc,
            )
