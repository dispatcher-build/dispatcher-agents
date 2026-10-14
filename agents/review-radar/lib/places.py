"""Google Places API (New) client: resolve a business lookup to a place_id,
then poll Place Details for rating, review count and the visible reviews."""

import hashlib

import requests

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
DETAILS_FIELDS = "id,displayName,rating,userRatingCount,googleMapsUri,reviews"
TIMEOUT = 10


class PlacesError(Exception):
    pass


def _headers(api_key: str, field_mask: str) -> dict:
    return {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
        "Content-Type": "application/json",
    }


def resolve(query: str, api_key: str) -> dict:
    """Text-search a business lookup ("Joe's Pizza, Brooklyn NY") to a place."""
    resp = requests.post(
        SEARCH_URL,
        json={"textQuery": query, "pageSize": 1},
        headers=_headers(api_key, "places.id,places.displayName,places.formattedAddress"),
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise PlacesError(f"search failed ({resp.status_code}): {resp.text[:200]}")
    places = resp.json().get("places") or []
    if not places:
        raise PlacesError(f"no place found for '{query}'")
    place = places[0]
    return {
        "place_id": place["id"],
        "name": (place.get("displayName") or {}).get("text", query),
        "address": place.get("formattedAddress", ""),
    }


def fetch_details(place_id: str, api_key: str) -> dict:
    resp = requests.get(
        DETAILS_URL.format(place_id=place_id),
        headers=_headers(api_key, DETAILS_FIELDS),
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise PlacesError(f"details failed ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    maps_url = data.get("googleMapsUri", "")
    reviews = []
    for review in data.get("reviews") or []:
        author = (review.get("authorAttribution") or {}).get("displayName", "anonymous")
        published = review.get("publishTime", "")
        text = ((review.get("text") or {}).get("text")
                or (review.get("originalText") or {}).get("text") or "")
        reviews.append({
            "id": review_hash(place_id, author, published),
            "author": author,
            "stars": int(review.get("rating") or 0),
            "text": text,
            "published": published,
            "url": review.get("googleMapsUri") or maps_url,
        })
    return {
        "name": (data.get("displayName") or {}).get("text", place_id),
        "rating": float(data.get("rating") or 0.0),
        "review_count": int(data.get("userRatingCount") or 0),
        "maps_url": maps_url,
        "reviews": reviews,
    }


def review_hash(place_id: str, author: str, published: str) -> str:
    return hashlib.sha1(f"{place_id}|{author}|{published}".encode()).hexdigest()[:16]
