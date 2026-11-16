
#!/usr/bin/env python3
"""
S.I.B.I.L.L.A. - ICON-2I ItaliaMeteo
V1 - Acquisizione e verifica GRIB ufficiale.

Fonte: Agenzia ItaliaMeteo / MeteoHub
Licenza dati: CC BY 4.0
Elaborazioni: progetto S.I.B.I.L.L.A.

Questa versione diagnostica NON pubblica
previsioni e NON inventa coordinate.
"""

import json
import re
import tempfile
import urllib.parse
import urllib.request

from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from eccodes import (
    codes_get,
    codes_get_array,
    codes_grib_get_data,
    codes_grib_new_from_file,
    codes_release,
)

ROOT = Path(__file__).resolve().parents[1]

BASE = (
    "https://meteohub.agenziaitaliameteo.it"
    "/nwp/ICON-2I_SURFACE_PRESSURE_LEVELS/"
)

STATUS = ROOT / "data/icon_status.json"

HEADERS = {
    "User-Agent": "SIBILLA-Scientific-Research/1.0",
    "Accept": "text/html,application/octet-stream",
}

MAX_DOWNLOAD = 100 * 1024 * 1024

CREDIT = {
    "provider": "Agenzia ItaliaMeteo",
    "dataset": "ICON-2I",
    "license": "CC BY 4.0",
    "license_url": (
        "https://creativecommons.org/licenses/by/4.0/"
    ),
    "source_url": BASE,
    "modified": False,
}


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.links.append(value)


def utc_now():
    return datetime.now(timezone.utc)


def timestamp():
    return utc_now().isoformat().replace("+00:00", "Z")


def request(url, timeout=30):
    parsed = urllib.parse.urlparse(url)

    if (
        parsed.scheme != "https"
        or parsed.hostname
        != "meteohub.agenziaitaliameteo.it"
    ):
        raise ValueError("Origine HTTP non autorizzata")

    return urllib.request.Request(
        url,
        headers=HEADERS,
    )


def get_links(url):
    req = request(url)

    with urllib.request.urlopen(
        req, timeout=30
    ) as response:
        html = response.read(2_000_000)

    parser = LinkParser()
    parser.feed(html.decode("utf-8", "replace"))

    links = []

    for href in parser.links:
        if href.startswith("../"):
            continue

        absolute = urllib.parse.urljoin(url, href)

        if not absolute.startswith(BASE):
            continue

        links.append(absolute)

    return links


def find_cycles():
    links = get_links(BASE)
    cycles = []

    for link in links:
        name = link.rstrip("/").split("/")[-1]

        if re.fullmatch(r"\d{10}", name):
            cycle = datetime.strptime(
                name, "%Y%m%d%H"
            ).replace(tzinfo=timezone.utc)

            if cycle <= utc_now():
                cycles.append((cycle, link))

    if not cycles:
        raise RuntimeError(
            "Nessun ciclo ICON-2I disponibile"
        )

    cycles.sort(reverse=True)

    return cycles


def find_grib(cycle_url, parameter):
    directory = (
        cycle_url.rstrip("/")
        + "/"
        + parameter
        + "/"
    )

    links = get_links(directory)

    files = []

    for link in links:
        name = link.rsplit("/", 1)[-1].lower()

        if name.endswith((
            ".grib", ".grib2", ".grb", ".grb2",
            ".grib.bz2", ".grib2.bz2",
            ".grb.bz2", ".grb2.bz2",
        )):
            files.append(link)

    return sorted(files)


def download(url, destination):
    req = request(url, timeout=45)
    total = 0

    with urllib.request.urlopen(
        req, timeout=45
    ) as response:

        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)

                if not chunk:
                    break

                total += len(chunk)

                if total > MAX_DOWNLOAD:
                    raise RuntimeError(
                        "File superiore al limite "
                        "diagnostico di 100 MB"
                    )

                output.write(chunk)

    if total == 0:
        raise RuntimeError("Download vuoto")

    return total


def optional(gid, key):
    try:
        value = codes_get(gid, key)
        if isinstance(value, bytes):
            return value.decode(
                "utf-8", "replace"
            )
        return value
    except Exception:
        return None


def inspect_grib(path):
    with path.open("rb") as stream:
        gid = codes_grib_new_from_file(stream)

        if gid is None:
            raise RuntimeError(
                "Nessun messaggio GRIB leggibile"
            )

        try:
            keys = (
                "shortName",
                "name",
                "gridType",
                "numberOfPoints",
                "numberOfGridUsed",
                "uuidOfHGrid",
                "dataDate",
                "dataTime",
                "step",
                "stepUnits",
                "validityDate",
                "validityTime",
                "units",
            )

            info = {
                key: optional(gid, key)
                for key in keys
            }

            values = codes_get_array(
                gid, "values"
            )

            info["decoded_values"] = len(values)

            if (
                info["numberOfPoints"] is not None
                and len(values)
                != int(info["numberOfPoints"])
            ):
                raise RuntimeError(
                    "Numero di valori GRIB incoerente"
                )

            try:
                lat = codes_get_array(
                    gid, "latitudes"
                )
                lon = codes_get_array(
                    gid, "longitudes"
                )

                info["coordinate_method"] = (
                    "direct_arrays"
                )

            except Exception as first:
                try:
                    lat, lon, _ = (
                        codes_grib_get_data(gid)
                    )

                    info["coordinate_method"] = (
                        "eccodes_iterator"
                    )

                except Exception as second:
                    info["coordinates_available"] = False
                    info["coordinate_error"] = (
                        type(first).__name__
                        + ": " + str(first)
                        + " | "
                        + type(second).__name__
                        + ": " + str(second)
                    )

                    return info

            if not (
                len(lat) == len(lon) == len(values)
            ):
                raise RuntimeError(
                    "Coordinate e valori non allineati"
                )

            info["coordinates_available"] = True
            info["coordinate_count"] = len(lat)

            return info

        finally:
            codes_release(gid)


def save_status(data):
    STATUS.parent.mkdir(
        parents=True, exist_ok=True
    )

    STATUS.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )


def main():
    report = {
        "model": "ICON-2I",
        "status": "checking",
        "generated_at": timestamp(),
        "source": CREDIT,
        "forecast_ready": False,
        "test_parameter": "CAPE_ML",
    }

    try:
        cycles = find_cycles()

        report["available_cycles"] = [
            item[0].isoformat()
            for item in cycles[:5]
        ]

        selected = None

        for cycle, url in cycles[:4]:
            files = find_grib(url, "CAPE_ML")

            if files:
                selected = (cycle, files[0])
                break

        if selected is None:
            raise RuntimeError(
                "Nessun file CAPE_ML trovato "
                "nei quattro cicli più recenti"
            )

        cycle, url = selected

        report["cycle"] = cycle.isoformat()
        report["sample_url"] = url

        print("ICON-2I cycle:", cycle)
        print("GRIB source:", url)

        with tempfile.TemporaryDirectory() as folder:
            destination = (
                Path(folder) / "sample.grib"
            )

            size = download(
                url, destination
            )

            report["download_bytes"] = size

            # I file compressi vengono riconosciuti
            # dai primi byte, senza indovinare
            # il formato dal solo nome.
            magic = destination.read_bytes()[:4]

            if magic.startswith(b"BZh"):
                import bz2

                unpacked = (
                    Path(folder) / "unpacked.grib"
                )

                with bz2.open(
                    destination, "rb"
                ) as source:
                    with unpacked.open("wb") as target:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            target.write(block)

                destination = unpacked

            report["grib"] = inspect_grib(
                destination
            )

        if report["grib"].get(
            "coordinates_available"
        ):
            report["status"] = (
                "sample_decoded"
            )
            report["note"] = (
                "GRIB georeferenziato. "
                "Previsioni non ancora generate."
            )
        else:
            report["status"] = (
                "grid_coordinates_missing"
            )
            report["note"] = (
                "Valori GRIB letti, ma coordinate "
                "non disponibili: serve griglia "
                "ufficiale verificata."
            )

    except Exception as exc:
        report["status"] = "failed"
        report["error"] = (
            type(exc).__name__ + ": " + str(exc)
        )

    finally:
        save_status(report)

        print(
            json.dumps(
                report,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )

    if report["status"] != "sample_decoded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
    
