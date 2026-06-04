"""Сохранение результата crawl_docdoc в PostgreSQL."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import orm as models

log = logging.getLogger(__name__)


def _parse_review_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip()[:19], fmt)
        except ValueError:
            continue
    return None


class _BatchCommitter:
    """Пакетные db.commit() по числу операций (как TELEGRAM_INGEST_BATCH)."""

    def __init__(self, db: Session, batch_size: int, *, label: str = "docdoc") -> None:
        self._db = db
        self._batch_size = max(1, batch_size)
        self._label = label
        self._pending = 0
        self.commits = 0

    def bump(self, n: int = 1) -> None:
        self._pending += n
        if self._pending >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if self._pending <= 0:
            return
        self._db.commit()
        self.commits += 1
        log.info("%s ingest: commit #%s (%s ops)", self._label, self.commits, self._pending)
        self._pending = 0


def _pg_upsert(
    db: Session,
    table: Any,
    constraint: str,
    key_values: dict[str, Any],
    fields: dict[str, Any],
) -> None:
    """INSERT ... ON CONFLICT DO UPDATE — безопасно при дублях в одном commit."""
    now = datetime.utcnow()
    stmt = pg_insert(table).values(**key_values, collected_at=now, **fields)
    stmt = stmt.on_conflict_do_update(
        constraint=constraint,
        set_={**fields, "collected_at": now},
    )
    db.execute(stmt)


def get_or_create_docdoc_source(db: Session, base_url: str, city_slug: str) -> models.Source:
    name = f"docdoc-{city_slug or 'unknown'}"
    source = db.scalar(select(models.Source).where(models.Source.name == name))
    if source:
        return source
    source = models.Source(
        name=name,
        base_url=base_url.rstrip("/"),
        parser_type="docdoc",
    )
    db.add(source)
    db.flush()
    return source


def _service_fields(parsed: dict[str, Any], city_slug: str) -> tuple[int, dict[str, Any]] | None:
    svc = parsed.get("service") or {}
    ext_id = svc.get("id")
    if ext_id is None:
        return None
    crumbs = parsed.get("breadcrumbs") or []
    direction = ""
    if crumbs and isinstance(crumbs[0], dict):
        direction = str(crumbs[0].get("name") or "")
    if not direction:
        direction = str(svc.get("parent_service_name") or "")
    fields = dict(
        page_url=str(parsed.get("page_url") or ""),
        name=str(svc.get("name") or ""),
        parent_service_name=str(svc.get("parent_service_name") or ""),
        category_direction=direction,
        avg_price=float(svc["avg_price"]) if svc.get("avg_price") is not None else None,
        description_plain=str(svc.get("description_plain") or ""),
        city_slug=city_slug,
        reviews_count_total=int(parsed["reviews_count_total"])
        if parsed.get("reviews_count_total") is not None
        else None,
    )
    return int(ext_id), fields


def _upsert_service(db: Session, source_id: int, parsed: dict[str, Any], city_slug: str) -> bool:
    row = _service_fields(parsed, city_slug)
    if not row:
        return False
    ext_id, fields = row
    _pg_upsert(
        db,
        models.DocdocService,
        "uq_docdoc_service",
        {"source_id": source_id, "external_service_id": ext_id},
        fields,
    )
    return True


def _upsert_clinic(db: Session, source_id: int, parsed: dict[str, Any], city_slug: str) -> bool:
    alias = parsed.get("clinic_alias") or ""
    if not alias:
        return False
    clinic = parsed.get("clinic") or {}
    fields = dict(
        external_clinic_id=int(clinic["id"]) if clinic.get("id") is not None else None,
        page_url=str(parsed.get("page_url") or ""),
        name=str(clinic.get("name") or ""),
        rating=float(clinic["rating"]) if clinic.get("rating") is not None else None,
        full_address=str(clinic.get("full_address") or ""),
        city_slug=city_slug,
    )
    _pg_upsert(
        db,
        models.DocdocClinic,
        "uq_docdoc_clinic",
        {"source_id": source_id, "clinic_alias": alias},
        fields,
    )
    return True


def _doctor_fields_from_card(doc: dict[str, Any], city_slug: str) -> tuple[int, dict[str, Any]] | None:
    ext = doc.get("doctor_id")
    if ext is None:
        return None
    fields = dict(
        doctor_alias=str(doc.get("profile_path") or "").split("?")[0].replace("/doctor/", ""),
        name=str(doc.get("name") or ""),
        profile_url=str(doc.get("profile_url") or ""),
        speciality="",
        total_rating=float(doc["total_rating"]) if doc.get("total_rating") is not None else None,
        reviews_count=int(doc["reviews_count"]) if doc.get("reviews_count") is not None else None,
        price=float(doc["price"]) if doc.get("price") is not None else None,
        address=str(doc.get("address") or ""),
        service_external_id=int(doc["service_id"]) if doc.get("service_id") is not None else None,
        service_name=str(doc.get("service_name") or ""),
        parent_service_name=str(doc.get("parent_service_name") or ""),
        city_slug=city_slug,
    )
    return int(ext), fields


def _upsert_doctor(db: Session, source_id: int, doc: dict[str, Any], city_slug: str) -> bool:
    row = _doctor_fields_from_card(doc, city_slug)
    if not row:
        return False
    ext_id, fields = row
    _pg_upsert(
        db,
        models.DocdocDoctor,
        "uq_docdoc_doctor",
        {"source_id": source_id, "external_doctor_id": ext_id},
        fields,
    )
    return True


def _upsert_doctor_profile(db: Session, source_id: int, parsed: dict[str, Any], city_slug: str) -> bool:
    info = parsed.get("doctor") or {}
    ext = info.get("id")
    if ext is None:
        return False
    fields = dict(
        doctor_alias=str(parsed.get("doctor_alias") or ""),
        name=str(info.get("name") or ""),
        profile_url=str(parsed.get("page_url") or ""),
        speciality=str(info.get("speciality") or ""),
        total_rating=float(info["total_rating"]) if info.get("total_rating") is not None else None,
        reviews_count=int(info["reviews_count"]) if info.get("reviews_count") is not None else None,
        price=None,
        address="",
        service_external_id=None,
        service_name="",
        parent_service_name="",
        city_slug=city_slug,
    )
    _pg_upsert(
        db,
        models.DocdocDoctor,
        "uq_docdoc_doctor",
        {"source_id": source_id, "external_doctor_id": int(ext)},
        fields,
    )
    return True


def _review_fields(rev: dict[str, Any]) -> dict[str, Any]:
    rating_val = rev.get("rating_value")
    try:
        rating_f = float(rating_val) if rating_val is not None else None
    except (TypeError, ValueError):
        rating_f = None
    rc = rev.get("rating_clinic")
    try:
        rc_f = float(rc) if rc is not None else None
    except (TypeError, ValueError):
        rc_f = None

    return dict(
        service_external_id=int(rev["service_id"]) if rev.get("service_id") is not None else None,
        clinic_alias=str(rev.get("clinic_alias") or ""),
        doctor_external_id=int(rev["doctor_id"])
        if rev.get("doctor_id") not in (None, 0, "0")
        else None,
        patient_public_name=str(rev.get("patient_public_name") or ""),
        doctor_name=str(rev.get("doctor_name") or ""),
        clinic_name=str(rev.get("clinic_name") or ""),
        service_name=str(rev.get("service_name") or ""),
        parent_service_name=str(rev.get("parent_service_name") or ""),
        category_direction=str(rev.get("category_direction_title") or ""),
        body=str(rev.get("text") or ""),
        answer=str(rev.get("answer") or ""),
        rating_value=rating_f,
        rating_clinic=rc_f,
        review_created=_parse_review_dt(rev.get("created")),
        source_page_url=str(rev.get("source_page_url") or ""),
    )


def _normalize_review_id(rev: dict[str, Any]) -> int | None:
    rid = rev.get("review_id")
    if rid is None:
        return None
    try:
        rid_int = int(rid)
    except (TypeError, ValueError):
        return None
    if rid_int <= 0:
        return None
    return rid_int


def _synthetic_review_id(rev: dict[str, Any]) -> int:
    """Стабильный int для SSR-отзывов без id (страница клиники)."""
    import zlib

    key = "|".join(
        [
            str(rev.get("clinic_alias") or ""),
            str(rev.get("created") or ""),
            (rev.get("text") or "")[:300],
            str(rev.get("patient_public_name") or ""),
        ]
    )
    rid = zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF
    return rid or 1


def _ensure_review_id(rev: dict[str, Any]) -> dict[str, Any]:
    if _normalize_review_id(rev) is not None:
        return rev
    out = dict(rev)
    out["review_id"] = _synthetic_review_id(rev)
    return out


def _needs_synthetic_review_id(rev: dict[str, Any]) -> bool:
    return rev.get("review_id") is None and bool((rev.get("text") or "").strip())


def iter_reviews_deduped(crawl: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Все отзывы из crawl один раз, по уникальному review_id."""
    seen: set[int] = set()
    sources: list[list[dict[str, Any]]] = []
    for parsed in crawl.get("services") or []:
        if isinstance(parsed, dict):
            sources.append(parsed.get("reviews") or [])
    for parsed in crawl.get("clinics") or []:
        if isinstance(parsed, dict):
            sources.append(parsed.get("reviews") or [])
    for dp in crawl.get("doctor_profiles") or []:
        if isinstance(dp, dict):
            sources.append(dp.get("reviews") or [])
    flat = crawl.get("reviews")
    if isinstance(flat, list):
        sources.append(flat)

    for chunk in sources:
        if not isinstance(chunk, list):
            continue
        for rev in chunk:
            if not isinstance(rev, dict):
                continue
            rid_int = _normalize_review_id(rev)
            if rid_int is None:
                if rev.get("review_id") is not None:
                    continue
                if not _needs_synthetic_review_id(rev):
                    continue
                rev = _ensure_review_id(rev)
                rid_int = _normalize_review_id(rev)
            if rid_int is None or rid_int in seen:
                continue
            seen.add(rid_int)
            yield rev


def _upsert_review_pg(db: Session, source_id: int, rev: dict[str, Any]) -> bool:
    """INSERT ... ON CONFLICT DO UPDATE (PostgreSQL)."""
    rid_int = _normalize_review_id(rev)
    if rid_int is None:
        return False

    fields = _review_fields(rev)
    _pg_upsert(
        db,
        models.DocdocReview,
        "uq_docdoc_review",
        {"source_id": source_id, "external_review_id": rid_int},
        fields,
    )
    return True


def _service_has_reviews(parsed: dict[str, Any]) -> bool:
    revs = parsed.get("reviews") or []
    if isinstance(revs, list) and revs:
        return True
    total = parsed.get("reviews_count_total")
    try:
        return total is not None and int(total) > 0
    except (TypeError, ValueError):
        return False


def _doctor_card_has_reviews(doc: dict[str, Any]) -> bool:
    rc = doc.get("reviews_count")
    try:
        return rc is not None and int(rc) > 0
    except (TypeError, ValueError):
        return False


def _doctor_profile_has_reviews(parsed: dict[str, Any]) -> bool:
    info = parsed.get("doctor") or {}
    rc = info.get("reviews_count")
    try:
        if rc is not None and int(rc) > 0:
            return True
    except (TypeError, ValueError):
        pass
    revs = parsed.get("reviews") or []
    return isinstance(revs, list) and bool(revs)


def ingest_docdoc_crawl_result(
    crawl: dict[str, Any],
    *,
    batch_commit: int | None = None,
    skip_empty_entities: bool | None = None,
) -> dict[str, Any]:
    """
    Принимает dict от crawl_docdoc(), пишет в sources + docdoc_* таблицы.
    skip_empty_entities (default True): услуги/клиники/врачи без отзывов в БД
    не сохраняем — это снижает шум в чате и метриках.
    """
    if not crawl.get("ok"):
        return {"ok": False, "error": crawl.get("error", "crawl_not_ok")}

    if batch_commit is None:
        batch_commit = int(os.getenv("DOCDOC_INGEST_BATCH", "32"))
    log_every = int(os.getenv("DOCDOC_INGEST_LOG_EVERY", "50"))
    if skip_empty_entities is None:
        skip_empty_entities = (
            os.getenv("DOCDOC_INGEST_SKIP_EMPTY", "true").strip().lower() in ("1", "true", "yes")
        )

    base_url = str(crawl.get("base_url") or "")
    city_slug = str(crawl.get("city_slug") or "unknown")
    target_city_label = (crawl.get("target_city_name") or "").strip().casefold()

    inserted = {
        "services": 0,
        "clinics": 0,
        "doctors": 0,
        "reviews": 0,
        "reviews_skipped": 0,
        "services_skipped_empty": 0,
        "clinics_skipped_empty": 0,
        "doctors_skipped_empty": 0,
        "reviews_skipped_other_city": 0,
        "reviews_skipped_unknown_clinic": 0,
    }

    services = crawl.get("services") or []
    clinics = crawl.get("clinics") or []
    profiles = crawl.get("doctor_profiles") or []

    # Множество «своих» clinic_alias — клиники, которые мы реально записали
    # (либо из карточек services с их target_city, либо из clinics_parsed).
    # По нему фильтруем review.clinic_alias на финальном этапе.
    target_aliases: set[str] = set()
    for parsed in services:
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            continue
        for c in parsed.get("clinics") or []:
            if isinstance(c, dict):
                a = c.get("clinic_alias")
                if isinstance(a, str) and a:
                    target_aliases.add(a)

    with SessionLocal() as db:
        batch = _BatchCommitter(db, batch_commit, label="docdoc")
        source = get_or_create_docdoc_source(db, base_url, city_slug)
        batch.flush()

        svc_total = len([s for s in services if isinstance(s, dict) and s.get("ok")])
        log.info(
            "docdoc ingest start source_id=%s services=%s batch=%s skip_empty=%s",
            source.id, svc_total, batch_commit, skip_empty_entities,
        )

        svc_done = 0
        for parsed in services:
            if not isinstance(parsed, dict) or not parsed.get("ok"):
                continue
            if skip_empty_entities and not _service_has_reviews(parsed):
                inserted["services_skipped_empty"] += 1
                continue
            if _upsert_service(db, source.id, parsed, city_slug):
                inserted["services"] += 1
                batch.bump()
            for d in parsed.get("doctors") or []:
                if not isinstance(d, dict):
                    continue
                if skip_empty_entities and not _doctor_card_has_reviews(d):
                    inserted["doctors_skipped_empty"] += 1
                    continue
                if _upsert_doctor(db, source.id, d, city_slug):
                    inserted["doctors"] += 1
                    batch.bump()
            svc_done += 1
            if svc_done % log_every == 0:
                log.info(
                    "docdoc ingest services: %s/%s commits=%s",
                    svc_done, svc_total, batch.commits,
                )

        for parsed in clinics:
            if not isinstance(parsed, dict) or not parsed.get("ok"):
                continue
            alias = parsed.get("clinic_alias")
            # Клиника не проходит в БД, если её alias ни разу не встретился
            # в собранных карточках услуг (значит, она «висит» сама по себе
            # без услуг и отзывов в этом городе).
            if skip_empty_entities and (
                not isinstance(alias, str)
                or not alias
                or alias not in target_aliases
            ):
                inserted["clinics_skipped_empty"] += 1
                continue
            if _upsert_clinic(db, source.id, parsed, city_slug):
                inserted["clinics"] += 1
                batch.bump()

        for dp in profiles:
            if not isinstance(dp, dict) or not dp.get("ok"):
                continue
            if skip_empty_entities and not _doctor_profile_has_reviews(dp):
                inserted["doctors_skipped_empty"] += 1
                continue
            if _upsert_doctor_profile(db, source.id, dp, city_slug):
                inserted["doctors"] += 1
            batch.bump()

        review_n = 0
        for rev in iter_reviews_deduped(crawl):
            review_n += 1
            # Защита: даже если парсер пропустил, отзыв с чужим городом
            # либо clinic_alias, которой нет в наших услугах — отбросим.
            r_city = (rev.get("clinic_city") or "").strip().casefold()
            if target_city_label and r_city and r_city != target_city_label:
                inserted["reviews_skipped_other_city"] += 1
                continue
            r_alias = rev.get("clinic_alias")
            if (
                skip_empty_entities
                and target_aliases
                and isinstance(r_alias, str)
                and r_alias
                and r_alias not in target_aliases
            ):
                inserted["reviews_skipped_unknown_clinic"] += 1
                continue
            if _upsert_review_pg(db, source.id, rev):
                inserted["reviews"] += 1
            else:
                inserted["reviews_skipped"] += 1
            batch.bump()
            if review_n % log_every == 0:
                log.info(
                    "docdoc ingest reviews: %s commits=%s inserted=%s",
                    review_n, batch.commits, inserted["reviews"],
                )

        batch.flush()

        return {
            "ok": True,
            "source_id": source.id,
            "source_name": source.name,
            "city_slug": city_slug,
            "target_city_name": crawl.get("target_city_name"),
            "skip_empty_entities": skip_empty_entities,
            "inserted": inserted,
            "db_commits": batch.commits,
            "batch_size": batch_commit,
            "crawl_stats": crawl.get("stats"),
        }
