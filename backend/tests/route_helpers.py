"""The routes an app serves, read through the walk FastAPI provides for them.

Since FastAPI 0.137.0 (fastapi#15745) `include_router` no longer copies a
router's routes into the app. `app.routes` holds one internal node per include,
and each included route stays on the router that declared it, without the
include's prefix or dependencies. A test reading `app.routes` directly finds no
paths at all: a check that a path is served fails, and one that no path matches
passes without looking at any.

`fastapi.routing.iter_route_contexts` (0.137.2) walks that tree. An included
APIRoute comes back with its full path, methods and dependencies. Any other
included route (a WebSocket route, a plain Starlette route, a mount) comes back
with an empty path (fastapi#16244), so its path and methods are read from the
`starlette_route` FastAPI serves it as. A route with no path to report fails
loudly, so no check built on these helpers can pass over a route it never saw.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.routing import RouteContext, iter_route_contexts


def served_routes(app: FastAPI) -> list[RouteContext]:
    """Every route `app` serves, one `RouteContext` each.

    An included route that is not an APIRoute reads an empty `path`, an empty
    `methods` and a `dependant` of None; take its path and methods from
    `served_endpoints`. An included WebSocket route's dependencies are on its
    `starlette_route.dependant`.
    """
    routes = list(iter_route_contexts(app.routes))
    assert routes, "the app serves no routes, so a check over them would pass on nothing"
    return routes


def served_endpoints(app: FastAPI) -> list[tuple[str, set[str]]]:
    """The full path and the methods of every route `app` serves, whatever its kind.

    A WebSocket route has no methods. A mount is one entry, at the path it is
    mounted on and with no methods; what it serves below that is not listed.
    """
    return [(_full_path(route), _methods(route)) for route in served_routes(app)]


def served_paths(app: FastAPI) -> set[str]:
    """The full path of every route `app` serves, whatever its kind."""
    return {path for path, _ in served_endpoints(app)}


def _served_as(route: RouteContext) -> Any:
    # Set only on an included route that is not an APIRoute, whose own context
    # reads an empty path (fastapi#16244). None for every other route.
    return getattr(route, "starlette_route", None)


def _full_path(route: RouteContext) -> str:
    served_as = _served_as(route)
    path = route.path if served_as is None else getattr(served_as, "path", None)
    assert path, f"a {type(route.original_route).__name__} has no path to report: {route.original_route!r}"
    return path


def _methods(route: RouteContext) -> set[str]:
    served_as = _served_as(route)
    methods = route.methods if served_as is None else getattr(served_as, "methods", None)
    return set(methods or ())
