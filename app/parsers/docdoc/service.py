"""Страница услуги: __NEXT_DATA__ / props.pageProps.preloadedState.servicePage."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.parsers.docdoc.doctors import normalize_best_doctors
from app.parsers.docdoc.htmlutil import extract_next_data, html_to_plain


def _pick_geo_city(state: dict[str, Any]) -> dict[str, Any] | None:
    c = state.get("city")
    if not isinstance(c, dict):
        return None
    cities = c.get("cities")
    if isinstance(cities, list) and cities:
        first = cities[0]
        return first if isinstance(first, dict) else None
    return None


# Маппинг city_slug домена (irk.docdoc.ru) → каноническое имя города.
# DocDoc в `fullAddress.city` пишет именно русское название, а в URL —
# slug, поэтому нам нужен мостик. Список покрывает основные миллионники
# на DocDoc; для незнакомого slug возвращаем None — фильтрация работает
# по equality, поэтому в этом случае пропустим всё (best-effort fallback).
_CITY_SLUG_TO_NAME: dict[str, str] = {
    "irk": "Иркутск",
    "msk": "Москва",
    "spb": "Санкт-Петербург",
    "ekb": "Екатеринбург",
    "nsk": "Новосибирск",
    "kzn": "Казань",
    "smr": "Самара",
    "rnd": "Ростов-на-Дону",
    "vrn": "Воронеж",
    "krd": "Краснодар",
    "ufa": "Уфа",
    "chl": "Челябинск",
    "perm": "Пермь",
    "vlg": "Волгоград",
    "krsk": "Красноярск",
    "tula": "Тула",
}


def city_slug_from_url(url: str) -> str | None:
    netloc = (urlparse(url).netloc or "").lower()
    if not netloc:
        return None
    head = netloc.split(".")[0]
    return head or None


def target_city_name(state: dict[str, Any] | None, page_url: str) -> str | None:
    """
    Каноническое имя текущего города (Иркутск/Москва/...) для фильтрации
    отзывов и клиник. Берём в порядке: preloadedState.city.cities[0].name
    → mapping по slug из URL (irk → Иркутск).
    """
    geo = _pick_geo_city(state) if isinstance(state, dict) else None
    if isinstance(geo, dict):
        name = (geo.get("name") or "").strip()
        if name:
            return name
    slug = city_slug_from_url(page_url)
    return _CITY_SLUG_TO_NAME.get(slug or "")


def _clinic_city_from_address(addr: Any) -> str | None:
    if isinstance(addr, dict):
        c = addr.get("city")
        return c.strip() if isinstance(c, str) and c.strip() else None
    if isinstance(addr, str) and addr.strip():
        return addr.strip()
    return None


def build_clinic_alias_city_map(
    state: dict[str, Any] | None, target_city: str | None
) -> dict[str, bool]:
    """
    Классификатор по clinic.alias: True — клиника в нашем городе, False — в другом.
    Строится из sp.clinics (наши) и sp.additional* (чужие).
    """
    out: dict[str, bool] = {}
    if not isinstance(state, dict):
        return out
    sp = state.get("servicePage")
    if not isinstance(sp, dict):
        return out

    def _walk(block: Any, default_in_target: bool) -> None:
        if not isinstance(block, list):
            return
        for row in block:
            if not isinstance(row, dict):
                continue
            c = row.get("clinic") if isinstance(row.get("clinic"), dict) else row
            alias = c.get("alias") if isinstance(c, dict) else None
            if not isinstance(alias, str) or not alias:
                continue
            city = _clinic_city_from_address(c.get("fullAddress") or c.get("address"))
            if target_city and city:
                in_target = city.casefold() == target_city.casefold()
            else:
                in_target = default_in_target
            # don't overwrite a positive with a negative — same alias может
            # появиться в обеих секциях, считаем «нашей», если хоть раз была.
            if alias in out:
                out[alias] = out[alias] or in_target
            else:
                out[alias] = in_target

    _walk(sp.get("clinics"), default_in_target=True)
    _walk(sp.get("additionalClinicsWithoutFilters"), default_in_target=False)
    _walk(sp.get("additionalParentLevelClinics"), default_in_target=False)
    return out


def review_belongs_to_city(
    raw: dict[str, Any],
    target_city: str | None,
    *,
    alias_map: dict[str, bool] | None = None,
) -> bool:
    """
    True — отзыв относится к target_city. Стратегия:
    1) alias_map (наши/чужие aliases по sp.clinics / additional*).
    2) fallback: clinic.fullAddress.city == target_city.
    3) если ни один источник ничего не сказал — оставляем (best-effort).
    """
    clinic = raw.get("clinic") if isinstance(raw.get("clinic"), dict) else {}
    alias = clinic.get("alias")
    if isinstance(alias, str) and alias and alias_map and alias in alias_map:
        return alias_map[alias]
    if not target_city:
        return True
    city = _clinic_city_from_address(clinic.get("fullAddress"))
    if city is None:
        return True
    return city.casefold() == target_city.casefold()


def _normalize_review(
    r: dict[str, Any],
    *,
    service_id: int | None,
    service_name: str,
    parent_service_name: str,
    service_url: str,
    category_direction: str | None,
) -> dict[str, Any]:
    clinic = r.get("clinic") if isinstance(r.get("clinic"), dict) else {}
    rating = r.get("rating") if isinstance(r.get("rating"), dict) else {}
    doctor = r.get("doctor") if isinstance(r.get("doctor"), dict) else {}
    doctor_id = r.get("doctorId")
    if doctor_id in (0, "0", None) and not doctor.get("value"):
        doctor_id = None
    doctor_name = doctor.get("label") or doctor.get("value") or doctor.get("name")
    if isinstance(doctor_name, str) and not doctor_name.strip():
        doctor_name = None
    clinic_city = _clinic_city_from_address(clinic.get("fullAddress"))
    return {
        "review_id": r.get("id"),
        "created": r.get("created"),
        "text": (r.get("text") or "").strip(),
        "answer": (r.get("answer") or "").strip(),
        "rating_clinic": r.get("ratingClinic"),
        "rating_label": rating.get("label"),
        "rating_value": rating.get("value"),
        "patient_public_name": r.get("publicName"),
        "clinic_name": clinic.get("name"),
        "clinic_alias": clinic.get("alias"),
        "clinic_city": clinic_city,
        "doctor_id": doctor_id if doctor_id not in (0, "0") else None,
        "doctor_name": doctor_name,
        "service_id": service_id,
        "service_name": service_name,
        "parent_service_name": parent_service_name,
        "category_direction_title": category_direction,
        "source_page_url": service_url,
    }


def _breadcrumbs(state: dict[str, Any]) -> list[dict[str, Any]]:
    sp = state.get("servicePage")
    if not isinstance(sp, dict):
        return []
    bc = sp.get("breadcrumbs")
    return bc if isinstance(bc, list) else []


def parse_service_page(html: str, page_url: str) -> dict[str, Any]:
    """Категория услуги: parent_service_name + хлебные крошки; врачи — bestDoctors."""
    nd = extract_next_data(html)
    if not nd:
        return {"page_kind": "service", "ok": False, "error": "no_next_data", "page_url": page_url}

    pp = nd.get("props", {}).get("pageProps", {})
    ps = pp.get("preloadedState")
    if not isinstance(ps, dict):
        return {"page_kind": "service", "ok": False, "error": "no_preloaded_state", "page_url": page_url}

    sp = ps.get("servicePage")
    if not isinstance(sp, dict):
        return {"page_kind": "service", "ok": False, "error": "no_service_page", "page_url": page_url}

    info = sp.get("serviceInfo") if isinstance(sp.get("serviceInfo"), dict) else {}
    service_id = info.get("id")
    service_name = str(info.get("name") or "")
    parent_service_name = str(info.get("parentServiceName") or "")
    root_hint = str(info.get("rootId") or "")

    crumbs = _breadcrumbs(ps)
    direction_title = None
    if crumbs:
        direction_title = str((crumbs[0] or {}).get("name") or "") or None

    target_city = target_city_name(ps, page_url)
    alias_map = build_clinic_alias_city_map(ps, target_city)

    reviews_raw = sp.get("reviews")
    reviews_list = reviews_raw if isinstance(reviews_raw, list) else []

    reviews_target: list[dict[str, Any]] = []
    reviews_dropped_other_city = 0
    for x in reviews_list:
        if not isinstance(x, dict):
            continue
        if not review_belongs_to_city(x, target_city, alias_map=alias_map):
            reviews_dropped_other_city += 1
            continue
        reviews_target.append(
            _normalize_review(
                x,
                service_id=int(service_id) if service_id is not None else None,
                service_name=service_name,
                parent_service_name=parent_service_name,
                service_url=page_url,
                category_direction=direction_title,
            )
        )

    # Клиники: берём только sp.clinics (= карточки в выдаче по услуге).
    # additionalClinicsWithoutFilters / additionalParentLevelClinics —
    # это «клиники в других городах», их давать в БД нельзя.
    clinics_out: list[dict[str, Any]] = []
    raw_clinic_block = sp.get("clinics") if isinstance(sp.get("clinics"), list) else []
    for row in raw_clinic_block:
        if not isinstance(row, dict):
            continue
        c = row.get("clinic") if isinstance(row.get("clinic"), dict) else row
        addr = c.get("fullAddress") or c.get("address")
        clinic_city = _clinic_city_from_address(addr)
        if target_city is not None and clinic_city is not None and clinic_city.casefold() != target_city.casefold():
            continue
        srv = row.get("service") if isinstance(row.get("service"), dict) else {}
        clinics_out.append(
            {
                "clinic_id": c.get("id"),
                "clinic_name": c.get("name"),
                "clinic_alias": c.get("alias"),
                "full_address": addr,
                "city": clinic_city,
                "in_price": row.get("inPrice"),
                "reviews_count": row.get("reviewsCount"),
                "reviews_url": row.get("reviewsUrl"),
                "service_card_name": srv.get("name"),
                "service_card_id": srv.get("id"),
            }
        )

    directions = sp.get("directions")
    directions_out = (
        [
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "title": d.get("title"),
            }
            for d in directions
            if isinstance(d, dict)
        ]
        if isinstance(directions, list)
        else []
    )

    city = _pick_geo_city(ps)
    base_url = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}/"
    doctors = normalize_best_doctors(
        sp.get("bestDoctors"),
        base_url=base_url,
        service_id=int(service_id) if service_id is not None else None,
        service_name=service_name,
        parent_service_name=parent_service_name,
    )

    return {
        "page_kind": "service",
        "ok": True,
        "page_url": page_url,
        "city_slug": (urlparse(page_url).netloc or "").split(".")[0] or None,
        "city": city,
        "service": {
            "id": service_id,
            "name": service_name,
            "parent_id": info.get("parentId"),
            "parent_service_name": parent_service_name,
            "root_id": info.get("rootId"),
            "avg_price": info.get("avgPrice"),
            "description_plain": html_to_plain(info.get("description")),
            "synonyms": info.get("synonyms") if isinstance(info.get("synonyms"), list) else [],
        },
        "directions": directions_out,
        "breadcrumbs": [
            {"name": b.get("name"), "url": b.get("url")} for b in crumbs if isinstance(b, dict)
        ],
        "clinics": clinics_out,
        "doctors": doctors,
        "reviews": reviews_target,
        "reviews_count_total": sp.get("reviewsCount"),
        "reviews_dropped_other_city": reviews_dropped_other_city,
        "target_city_name": target_city,
        "meta": {"root_hint": root_hint},
    }
