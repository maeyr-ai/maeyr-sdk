"""Readable incarnation names. UUID identity supplies uniqueness, not word lists."""

import hashlib
from uuid import UUID

SEASONS = ("Autumn", "Winter", "Spring", "Summer", "Monsoon", "Dawn", "Dusk", "Equinox")
ANIMALS = (
    "Falcon",
    "Lynx",
    "Otter",
    "Heron",
    "Wolf",
    "Raven",
    "Jaguar",
    "Eagle",
    "Panda",
    "Osprey",
    "Fox",
    "Crane",
    "Ibis",
    "Kestrel",
    "Orca",
    "Swift",
)
PLACES = (
    "Andes",
    "Baltic",
    "Kyoto",
    "Sahara",
    "Nordic",
    "Pacific",
    "Gobi",
    "Alps",
    "Himalayas",
    "Atlas",
    "Patagonia",
    "Arctic",
    "Cascades",
    "Serengeti",
    "Nile",
    "Borneo",
)


def worker_name(incarnation: str) -> str:
    identity = UUID(incarnation)
    digest = hashlib.sha256(identity.bytes).digest()
    return f"{SEASONS[digest[0] % len(SEASONS)]} {ANIMALS[digest[1] % len(ANIMALS)]} of {PLACES[digest[2] % len(PLACES)]}-{identity.hex}"
