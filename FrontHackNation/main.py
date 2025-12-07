"""Simple FastAPI backend powering the map frontend."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field


BASE_DIR = Path(__file__).resolve().parent
POINTS_CSV = BASE_DIR / "lokalzacja.csv"
DATABASE_CSV = BASE_DIR / "database.csv"


class Point(BaseModel):
    """Represents a point of interest."""

    id: str
    name: str
    lat: float
    lng: float
    description: str | None = None


class Route(BaseModel):
    """Represents a bidirectional connection between two points."""

    model_config = ConfigDict(populate_by_name=True)

    from_id: str = Field(..., alias="from_id")
    to_id: str = Field(..., alias="to_id")


class RouteRequest(BaseModel):
    """Request body for adding or removing routes."""

    from_id: str
    to_id: str


app = FastAPI(title="Points & Routes API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount(
    "/llm-html",
    StaticFiles(directory=BASE_DIR.parent / "llm-html"),
    name="llm-html",
)


@app.get("/lokalzacja.csv")
def get_lokalizacja_csv() -> FileResponse:
    """Serve lokalizacja CSV for the frontend fallback."""
    if not POINTS_CSV.exists():
        raise HTTPException(status_code=404, detail="lokalzacja.csv not found")
    return FileResponse(POINTS_CSV, media_type="text/csv")


@app.get("/database.csv")
def get_database_csv() -> FileResponse:
    """Serve database CSV for the frontend fallback."""
    if not DATABASE_CSV.exists():
        raise HTTPException(status_code=404, detail="database.csv not found")
    return FileResponse(DATABASE_CSV, media_type="text/csv")

points: Dict[str, Point] = {}
routes: List[Route] = []
route_index: Set[Tuple[str, str]] = set()
lokalizacja_sequence: List[str] = []
optimized_sequence: List[str] = []
full_route_geojson: Dict | None = None

OSRM_PROFILE = "walking"
OSRM_ROUTE_URL = f"https://router.project-osrm.org/route/v1/{OSRM_PROFILE}"


def normalize_route_ids(from_id: str, to_id: str) -> Tuple[str, str]:
    """Return a sorted tuple to represent a bidirectional route."""
    return tuple(sorted((from_id, to_id)))


def _add_route_internal(from_id: str, to_id: str) -> Route | None:
    """Internal helper to register routes without raising HTTP errors."""
    if from_id == to_id:
        return None
    if from_id not in points or to_id not in points:
        return None
    normalized = normalize_route_ids(from_id, to_id)
    if normalized in route_index:
        return None
    route = Route(from_id=normalized[0], to_id=normalized[1])
    routes.append(route)
    route_index.add(normalized)
    print(f"[_add_route_internal] added route {route.from_id} -> {route.to_id}")
    return route


def load_lokalizacja_points(file_path: Path) -> Tuple[Dict[str, Point], List[str]]:
    """Load sequential points from lokalzacja.csv."""
    if not file_path.exists():
        raise FileNotFoundError(f"CSV file not found: {file_path}")

    loaded_points: Dict[str, Point] = {}
    sequential_ids: List[str] = []

    with file_path.open(newline="", encoding="utf-8-sig") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=";")
        for idx, raw_row in enumerate(reader, start=1):
            normalized_row = {
                (key or "").strip().lstrip("\ufeff"): (value or "").strip()
                for key, value in raw_row.items()
            }
            point_id = normalized_row.get("ID")
            if not point_id:
                continue
            try:
                lng = float(normalized_row.get("x") or normalized_row.get("X"))  # x = longitude
                lat = float(normalized_row.get("y") or normalized_row.get("Y"))  # y = latitude
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid coordinates in row: {normalized_row}") from exc

            point = Point(
                id=point_id,
                name=normalized_row.get("Localization")
                or normalized_row.get("Localisation")
                or f"Localization {idx}",
                lat=lat,
                lng=lng,
            )
            loaded_points[point.id] = point
            sequential_ids.append(point.id)

    return loaded_points, sequential_ids


def load_database_points(file_path: Path, existing: Dict[str, Point]) -> Dict[str, Point]:
    """Load additional points from database.csv (with 'GPS ID')."""
    if not file_path.exists():
        return existing

    next_index = 1
    with file_path.open(newline="", encoding="utf-8-sig") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=";")
        for raw_row in reader:
            normalized_row = {
                (key or "").strip().lstrip("\ufeff"): (value or "").strip()
                for key, value in raw_row.items()
            }
            gps_value = normalized_row.get("GPS ID") or normalized_row.get("gps id")
            if not gps_value:
                continue
            cleaned = gps_value.strip()
            parts = [part.strip() for part in cleaned.replace(";", ",").split(",") if part.strip()]
            if len(parts) != 2:
                continue
            try:
                lat, lng = float(parts[0]), float(parts[1])
            except ValueError:
                continue

            point_id = normalized_row.get("id") or f"db_{next_index}"
            while point_id in existing:
                next_index += 1
                point_id = f"db_{next_index}"

            point = Point(
                id=point_id,
                name=normalized_row.get("Name") or normalized_row.get("name") or point_id,
                lat=lat,
                lng=lng,
                description=normalized_row.get("Description"),
            )
            existing[point.id] = point
            next_index += 1

    return existing


def build_path_connections(sequence: List[str]) -> List[Tuple[str, str]]:
    """Create simple path connections without returning to the start."""
    if len(sequence) < 2:
        return []
    return [(sequence[i], sequence[i + 1]) for i in range(len(sequence) - 1)]



def fetch_osrm_segment(start_point: Point, end_point: Point) -> Tuple[List[List[float]], float | None, float | None] | None:
    """Fetch a single OSRM segment between two coordinates."""
    url = (
        f"{OSRM_ROUTE_URL}/{start_point.lng},{start_point.lat};"
        f"{end_point.lng},{end_point.lat}?overview=full&geometries=geojson"
    )
    try:
        with urlopen(url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as error:
        print(f"[fetch_osrm_segment] Failed to call OSRM: {error}")
        return None

    routes_payload = payload.get("routes") or []
    if not routes_payload:
        print("[fetch_osrm_segment] OSRM returned no routes.")
        return None

    route_payload = routes_payload[0]
    geometry = route_payload.get("geometry")
    if not geometry or geometry.get("type") != "LineString":
        print("[fetch_osrm_segment] OSRM response missing geometry.")
        return None

    return geometry.get("coordinates", []), route_payload.get("distance"), route_payload.get("duration")


def fetch_osrm_route(sequence: List[str]) -> Tuple[Dict, List[str]] | None:
    """Call OSRM route API to connect all points in the given order."""
    coordinates: List[str] = []
    ordered_ids: List[str] = []

    for point_id in sequence:
        point = points.get(point_id)
        if not point:
            continue
        coordinates.append(f"{point.lng},{point.lat}")
        ordered_ids.append(point_id)

    if len(coordinates) < 2:
        return None

    joined = ";".join(coordinates)
    encoded = quote(joined, safe=";,")
    url = f"{OSRM_ROUTE_URL}/{encoded}?overview=full&geometries=geojson"

    try:
        with urlopen(url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as error:
        print(f"[fetch_osrm_route] Failed to call OSRM route: {error}")
        return None

    routes_payload = payload.get("routes") or []
    if not routes_payload:
        print("[fetch_osrm_route] OSRM returned no routes.")
        return None

    geometry = routes_payload[0].get("geometry")
    if not geometry:
        print("[fetch_osrm_route] Missing geometry in route result.")
        return None

    feature = {
        "type": "Feature",
        "geometry": geometry,
        "properties": {
            "via_points": ordered_ids,
            "source": "router.project-osrm.org-route",
            "distance_m": routes_payload[0].get("distance"),
            "duration_s": routes_payload[0].get("duration"),
        },
    }
    feature_collection = {"type": "FeatureCollection", "features": [feature]}
    return feature_collection, ordered_ids


def build_segment_geojson(sequence: List[str]) -> Dict | None:
    """Fallback: connect all points using individual OSRM route calls."""
    combined_coordinates: List[List[float]] = []
    total_distance = 0.0
    total_duration = 0.0

    for from_id, to_id in build_path_connections(sequence):
        start_point = points.get(from_id)
        end_point = points.get(to_id)
        if not start_point or not end_point:
            continue

        segment = fetch_osrm_segment(start_point, end_point)
        if not segment:
            continue

        coordinates, distance, duration = segment
        if not coordinates:
            continue

        if not combined_coordinates:
            combined_coordinates.extend(coordinates)
        else:
            if combined_coordinates[-1] == coordinates[0]:
                combined_coordinates.extend(coordinates[1:])
            else:
                combined_coordinates.extend(coordinates)

        if distance:
            total_distance += distance
        if duration:
            total_duration += duration

    if len(combined_coordinates) < 2:
        print("[build_segment_geojson] Failed to compose OSRM geometry.")
        return None

    feature = {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": combined_coordinates,
        },
        "properties": {
            "distance_m": total_distance or None,
            "duration_s": total_duration or None,
            "via_points": sequence,
            "source": "router.project-osrm.org-route",
        },
    }
    return {"type": "FeatureCollection", "features": [feature]}


def refresh_route_geometry(sequence: List[str]) -> Dict | None:
    """Fetch a walking route for lokalizacja points in the given order."""
    global optimized_sequence
    if len(sequence) < 2:
        optimized_sequence = sequence
        print("[refresh_route_geometry] Not enough points for OSRM route.")
        return None

    route_result = fetch_osrm_route(sequence)
    if route_result:
        full_geojson, ordered_ids = route_result
        optimized_sequence = ordered_ids
        print("[refresh_route_geometry] route order:", " -> ".join(optimized_sequence))
        return full_geojson

    optimized_sequence = sequence
    fallback = build_segment_geojson(sequence)
    if fallback:
        print("[refresh_route_geometry] using segment-based fallback geometry.")
    return fallback


def reload_points() -> List[Point]:
    """Reload global points and clear existing routes."""
    global points, routes, route_index, lokalizacja_sequence, full_route_geojson
    points, sequential_ids = load_lokalizacja_points(POINTS_CSV)
    lokalizacja_sequence = sequential_ids
    points = load_database_points(DATABASE_CSV, points)
    full_route_geojson = refresh_route_geometry(lokalizacja_sequence)
    active_order = optimized_sequence if optimized_sequence else lokalizacja_sequence
    default_connections = build_path_connections(active_order)
    routes = []
    route_index = set()
    for from_id, to_id in default_connections:
        _add_route_internal(from_id, to_id)
    print(f"[reload_points] total routes={len(routes)}")
    return list(points.values())


# Initial load
try:
    reload_points()
except Exception as error:  # noqa: BLE001
    print(f"Failed to load points on startup: {error}")


@app.get("/points", response_model=List[Point])
def get_points() -> List[Point]:
    """Return all available points."""
    return list(points.values())


@app.get("/routes", response_model=List[Route])
def get_routes() -> List[Route]:
    """Return all existing routes."""
    return routes


@app.get("/route-geojson")
def get_route_geojson() -> Dict:
    """Return the OSRM-backed GeoJSON route following lokalizacja points."""
    if not full_route_geojson:
        raise HTTPException(
            status_code=404,
            detail={"error": "Route geometry not available – try reloading later."},
        )
    return full_route_geojson


@app.post("/routes", response_model=Route, status_code=201)
def add_route(route_request: RouteRequest) -> Route:
    """Create a new route between two points."""
    route = _add_route_internal(route_request.from_id, route_request.to_id)
    if route is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid route (duplicate or unknown points)"},
        )
    return route


@app.delete("/routes")
def delete_route(route_request: RouteRequest) -> Dict[str, str]:
    """Remove an existing route."""
    normalized = normalize_route_ids(route_request.from_id, route_request.to_id)
    if normalized not in route_index:
        raise HTTPException(
            status_code=404, detail={"error": "Route not found between points"}
        )

    route_index.remove(normalized)
    for idx, route in enumerate(routes):
        if normalize_route_ids(route.from_id, route.to_id) == normalized:
            routes.pop(idx)
            break

    return {"message": "Route removed successfully"}


@app.post("/reload-points", response_model=List[Point])
def reload_points_endpoint() -> List[Point]:
    """Reload points from CSV and clear routes."""
    try:
        return reload_points()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail={"error": str(exc)}) from exc


@app.get("/api/route-config")
def get_route_config() -> Dict[str, Dict[str, str]]:
    """Provide a simple route configuration for the frontend."""
    route: Route | None = routes[0] if routes else None

    if route is None:
        if len(points) < 2:
            raise HTTPException(
                status_code=404, detail={"error": "Not enough points to suggest a route"}
            )
        point_ids = list(points.keys())
        normalized = normalize_route_ids(point_ids[0], point_ids[1])
    else:
        normalized = normalize_route_ids(route.from_id, route.to_id)

    start_point = points.get(normalized[0])
    end_point = points.get(normalized[1])
    if not start_point or not end_point:
        raise HTTPException(
            status_code=404, detail={"error": "Points for the route could not be found"}
        )

    return {
        "start": {"id": start_point.id, "name": start_point.name},
        "end": {"id": end_point.id, "name": end_point.name},
    }


@app.get("/", include_in_schema=False)
def serve_index() -> FileResponse:
    """Serve the frontend index file."""
    index_path = BASE_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail={"error": "index.html not found"})
    return FileResponse(index_path)


@app.get("/lokalizacja.csv", include_in_schema=False)
def serve_points_csv() -> FileResponse:
    """Expose the source CSV."""
    if not POINTS_CSV.exists():
        raise HTTPException(status_code=404, detail={"error": "CSV file not found"})
    return FileResponse(POINTS_CSV)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
