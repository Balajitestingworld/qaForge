"""Event verification - prove that what the UI did reached the event backbone (Kafka).

Design choices that make this reliable in shared SIT/UAT clusters:
  * No long-lived subscription. When a test asks for an event, a fresh consumer (unique
    group id, no commits) seeks every partition to the test's start time minus a small
    lookback and scans forward. Nothing is missed even if the event was produced before
    we started looking, and other teams' consumer groups are never disturbed.
  * Correlation by business key (txn reference, account number...) in the message key,
    a header, or any field of the payload.
  * Read-only: the framework never produces to business topics.

Sources:  KafkaEventSource (confluent-kafka, JSON / Avro + Schema Registry / string)
          HttpEventSource  (an HTTP endpoint returning events - used by the bundled demo bank,
                            and by teams that expose an event-capture test API)
"""
from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from qaforge.compliance.evidence import EvidencePack
from qaforge.compliance.logger import get_logger, utc_now_iso
from qaforge.compliance.masker import masker
from qaforge.config.secrets import SecretProvider
from qaforge.config.settings import Settings
from qaforge.data.context import ScenarioContext
from qaforge.verifiers.compare import FieldMapping, VerificationRecord, get_path, verify

log = get_logger("events")
Matcher = Callable[["Event"], bool] | dict[str, Any] | None


class EventNotFoundError(AssertionError):
    pass


@dataclass
class Event:
    topic: str
    key: str | None
    value: Any
    timestamp_ms: int
    headers: dict[str, str] = field(default_factory=dict)
    partition: int | None = None
    offset: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "key": self.key, "headers": self.headers, "timestamp_ms": self.timestamp_ms,
                "partition": self.partition, "offset": self.offset, "value": self.value}


def _matches(ev: Event, key: str | None, where: Matcher) -> bool:
    if key is not None and ev.key != key and ev.headers.get("key") != key:
        return False
    if where is None:
        return True
    if callable(where):
        return bool(where(ev))
    return all(str(get_path(ev.value, path)) == str(expected) for path, expected in where.items())


class EventSource(ABC):
    @abstractmethod
    def scan(self, topic: str, since_ms: int, deadline: float, poll_s: float,
             accept: Callable[[Event], bool]) -> tuple[Event | None, int]:
        """Return the first accepted event (or None at deadline) and how many events were scanned."""

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


class HttpEventSource(EventSource):
    def __init__(self, base_url: str, timeout_s: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.http = requests.Session()

    def scan(self, topic: str, since_ms: int, deadline: float, poll_s: float,
             accept: Callable[[Event], bool]) -> tuple[Event | None, int]:
        scanned_ids: set[tuple[Any, ...]] = set()
        while True:
            resp = self.http.get(f"{self.base_url}/api/events", params={"topic": topic, "since": since_ms},
                                 timeout=self.timeout_s)
            resp.raise_for_status()
            for raw in resp.json():
                ev = Event(topic=raw["topic"], key=raw.get("key"), value=raw.get("value"),
                           timestamp_ms=int(raw.get("timestamp_ms", 0)), headers=raw.get("headers") or {},
                           partition=raw.get("partition"), offset=raw.get("offset"))
                scanned_ids.add((ev.partition, ev.offset, ev.key, ev.timestamp_ms))
                if accept(ev):
                    return ev, len(scanned_ids)
            if time.monotonic() >= deadline:
                return None, len(scanned_ids)
            time.sleep(poll_s)


class KafkaEventSource(EventSource):
    def __init__(self, settings: Settings, secrets: SecretProvider) -> None:
        try:
            import confluent_kafka  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError("Kafka verification needs: pip install 'qaforge[kafka]' (confluent-kafka)") from exc
        k = settings.events.kafka
        self.cfg = k
        conf: dict[str, Any] = {
            "bootstrap.servers": k.bootstrap_servers,
            "security.protocol": k.security_protocol,
            "enable.auto.commit": False,
            "auto.offset.reset": "latest",
            "session.timeout.ms": 10000,
        }
        if k.security_protocol.startswith("SASL") and k.sasl_mechanism:
            conf["sasl.mechanism"] = k.sasl_mechanism
            if k.sasl_mechanism not in ("GSSAPI", "OAUTHBEARER"):
                conf["sasl.username"] = secrets.get(k.username_secret or "KAFKA_USERNAME")
                conf["sasl.password"] = secrets.get(k.password_secret or "KAFKA_PASSWORD")
        for opt, val in (("ssl.ca.location", k.ssl_ca_location),
                         ("ssl.certificate.location", k.ssl_certificate_location),
                         ("ssl.key.location", k.ssl_key_location)):
            if val:
                conf[opt] = val
        conf.update(k.extra)
        self.base_conf = conf
        self._deserialize = self._build_deserializer(settings, secrets)

    def _build_deserializer(self, settings: Settings, secrets: SecretProvider) -> Callable[[str, bytes], Any]:
        k = settings.events.kafka
        if k.value_format == "avro":
            from confluent_kafka.schema_registry import SchemaRegistryClient
            from confluent_kafka.schema_registry.avro import AvroDeserializer
            from confluent_kafka.serialization import MessageField, SerializationContext

            sr_conf: dict[str, Any] = {"url": k.schema_registry_url}
            user = secrets.get_optional(k.schema_registry_user_secret)
            if user:
                sr_conf["basic.auth.user.info"] = f"{user}:{secrets.get(k.schema_registry_password_secret or '')}"
            deser = AvroDeserializer(SchemaRegistryClient(sr_conf))
            return lambda topic, raw: deser(raw, SerializationContext(topic, MessageField.VALUE))
        if k.value_format == "json":
            return lambda _t, raw: json.loads(raw) if raw else None
        return lambda _t, raw: raw.decode("utf-8", errors="replace") if raw else None

    def scan(self, topic: str, since_ms: int, deadline: float, poll_s: float,
             accept: Callable[[Event], bool]) -> tuple[Event | None, int]:
        from confluent_kafka import OFFSET_END, Consumer, TopicPartition

        consumer = Consumer({**self.base_conf, "group.id": f"{self.cfg.group_id_prefix}-{uuid.uuid4().hex[:10]}"})
        scanned = 0
        try:
            md = consumer.list_topics(topic, timeout=15)
            if topic not in md.topics or md.topics[topic].error is not None:
                raise EventNotFoundError(f"Kafka topic '{topic}' not found or not authorised")
            wanted = [TopicPartition(topic, p, since_ms) for p in md.topics[topic].partitions]
            resolved = consumer.offsets_for_times(wanted, timeout=15)
            consumer.assign([tp if tp.offset >= 0 else TopicPartition(topic, tp.partition, OFFSET_END)
                             for tp in resolved])
            while time.monotonic() < deadline:
                msg = consumer.poll(poll_s)
                if msg is None:
                    continue
                if msg.error():
                    log.warning("Kafka error on %s: %s", topic, msg.error())
                    continue
                scanned += 1
                ev = Event(
                    topic=msg.topic(),
                    key=msg.key().decode("utf-8", errors="replace") if msg.key() else None,
                    value=self._deserialize(msg.topic(), msg.value()),
                    timestamp_ms=msg.timestamp()[1],
                    headers={k: (v.decode("utf-8", errors="replace") if isinstance(v, bytes) else str(v))
                             for k, v in (msg.headers() or [])},
                    partition=msg.partition(),
                    offset=msg.offset(),
                )
                if accept(ev):
                    return ev, scanned
            return None, scanned
        finally:
            consumer.close()


class NullEventSource(EventSource):
    def scan(self, topic: str, since_ms: int, deadline: float, poll_s: float,
             accept: Callable[[Event], bool]) -> tuple[Event | None, int]:
        raise RuntimeError("Event verification is disabled for this env (events.source: none)")


def build_event_source(settings: Settings, secrets: SecretProvider) -> EventSource:
    src = settings.events.source
    if src == "kafka":
        return KafkaEventSource(settings, secrets)
    if src == "http":
        return HttpEventSource(settings.events.http_base_url)
    return NullEventSource()


class EventProbe:
    """Per-test handle: knows the test start time, the scenario context and the evidence pack."""

    def __init__(self, source: EventSource, settings: Settings, evidence: EvidencePack,
                 ctx: ScenarioContext, test_started_ms: int) -> None:
        self.source = source
        self.settings = settings
        self.evidence = evidence
        self.ctx = ctx
        self.since_ms = test_started_ms - settings.events.kafka.lookback_seconds * 1000
        self._n = 0

    def expect(self, topic: str, *, key: str | None = None, where: Matcher = None,
               timeout_s: float | None = None, description: str | None = None) -> Event:
        timeout = timeout_s or self.settings.events.default_timeout_s
        label = description or f"event on '{topic}'" + (f" with key {key}" if key else "")
        started, t0 = utc_now_iso(), time.monotonic()
        log.info("Waiting up to %ss for %s", timeout, label)
        ev, scanned = self.source.scan(topic, self.since_ms, t0 + timeout, self.settings.events.poll_interval_s,
                                       lambda e: _matches(e, key, where))
        ms = int((time.monotonic() - t0) * 1000)
        if ev is None:
            self.evidence.add_step(f"Expect {label}", "failed", started, ms, app="events",
                                   error=f"Not found within {timeout}s ({scanned} events scanned)")
            raise EventNotFoundError(f"No {label} within {timeout}s ({scanned} events scanned)")
        self._n += 1
        fname = f"event_{self._n:02d}_{topic.replace('.', '_')}.json"
        self.evidence.attach_json(fname, ev.as_dict(), kind="event")
        self.evidence.add_step(f"Received {label}", "passed", started, ms, app="events",
                               detail=f"partition={ev.partition} offset={ev.offset} -> {fname}")
        log.info("Received %s after %d ms: %s", label, ms, masker.mask(json.dumps(ev.value, default=str))[:500])
        return ev

    def expect_and_verify(self, topic: str, mapping: FieldMapping | list[dict[str, Any]] | str, *,
                          key: str | None = None, where: Matcher = None, timeout_s: float | None = None,
                          soft: bool = False) -> tuple[Event, VerificationRecord]:
        if isinstance(mapping, str) and "/" not in mapping and not mapping.endswith(".yaml"):
            mapping = FieldMapping.load(mapping, self.settings.path("config/mappings"))
        ev = self.expect(topic, key=key, where=where, timeout_s=timeout_s)
        rec = verify(ev.value, mapping, ctx=self.ctx, evidence=self.evidence, source=f"kafka:{topic}", soft=soft)
        return ev, rec

    def expect_none(self, topic: str, *, key: str | None = None, where: Matcher = None,
                    within_s: float = 15) -> None:
        """Negative check, e.g. a rejected payment must NOT produce a payments.initiated event."""
        started, t0 = utc_now_iso(), time.monotonic()
        ev, scanned = self.source.scan(topic, self.since_ms, t0 + within_s, self.settings.events.poll_interval_s,
                                       lambda e: _matches(e, key, where))
        ms = int((time.monotonic() - t0) * 1000)
        label = f"no event on '{topic}'" + (f" with key {key}" if key else "")
        if ev is not None:
            self.evidence.attach_json(f"unexpected_event_{topic}.json", ev.as_dict(), kind="event")
            self.evidence.add_step(f"Expect {label}", "failed", started, ms, app="events",
                                   error="An event was published")
            raise AssertionError(f"Unexpected event on '{topic}' (key={ev.key})")
        self.evidence.add_step(f"Confirmed {label} for {within_s}s", "passed", started, ms, app="events",
                               detail=f"{scanned} events scanned")
