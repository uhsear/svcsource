#!/usr/bin/env python
r"""Report the database, instance, dataset and portal item ID behind every
service on an ArcGIS Server site, from the Admin API.

A service is a copy of a map document plus a pointer to the data it reads. The
pointer is in the service manifest, which the Admin API will hand you per
service, and nowhere else. `/rest/services` does not carry it. Neither does the
service description every client reads. So the question "which of these two
hundred services still read the old geodatabase" has no answer you can put in a
spreadsheet, and a migration is a list of services you have to open one at a
time.

The portal item ID is the other half. Republish a service and the REST url can
come out identical while every cloned web map that referenced the layer by item
ID points at an item that no longer exists. The item ID is in the service's own
`portalProperties`, and that is the field this tool writes down before you
republish anything.

Why not the tools that already exist. ArcGIS Server Manager shows a service's
data source on its own page, and Pro's Analyze and the Share pane both read the
same manifest when they publish. Both are correct and both are per service. The
ArcGIS API for Python reads the catalog in two lines with
`server.services.list()`, and each service's properties with it, which is the
right tool for reading one site interactively. The gap is the flat file: one row
per dataset, every service on the site, with the database, the instance, the
version and the item ID in columns you can sort, diff against the new server
after cutover, and hand to somebody who does not have Pro.

    python svcsource.py --self-test
    python svcsource.py --server https://gis.example.com/arcgis --user gis_admin
    python svcsource.py --server https://gis.example.com:6443/arcgis --user gis_admin \
        --out services.csv --apply
    python svcsource.py --server https://gis.example.com/arcgis --user gis_admin \
        --portal https://portal.example.com/portal/sharing/rest \
        --public-rest-root https://gis.example.com/server/rest/services

The password comes from SVCSOURCE_PASSWORD or an unechoed prompt, never from
argv. Exit codes: 0 every service resolved, 1 at least one data source could
not be read, 2 the site could not be read, 64 usage error.
"""

from __future__ import print_function

import argparse
import csv
import getpass
import io
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Folders the walk skips. Both hold the services ArcGIS Server publishes for
# itself, none of which reads your data. Including them puts a page of
# PublishingTools rows in front of the ones a migration cares about.
SKIP_FOLDERS = ("System", "Utilities")

# Service types that legitimately have no data source at all. A GeometryServer
# projects coordinates and reads nothing, so an absent manifest is the correct
# answer for it and must not fail the run. Every other type that comes back
# without a manifest is reported unresolved, which is the safe direction: an
# unread data source is the one that breaks after cutover.
NO_DATASOURCE_TYPES = ("GeometryServer",)

# Seconds to wait for one Admin API call.
HTTP_TIMEOUT = 30

# Folders to walk before refusing. ArcGIS Server folders are one level deep,
# so this is a loop guard rather than a depth limit.
MAX_FOLDERS = 512

# Environment variables the two passwords may come from.
SECRET_ENV = "SVCSOURCE_PASSWORD"
PORTAL_SECRET_ENV = "SVCSOURCE_PORTAL_PASSWORD"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

REDACTED = "***"

# Row status. The gate reads this column and nothing else.
OK = "ok"
COPIED = "copied"              # data copied to the server, not registered
NODATA = "no-datasource"       # this service type reads nothing
UNRESOLVED = "unresolved"      # the data source could not be read

COLUMNS = ("folder", "service", "type", "path", "status", "source_item_id",
           "all_item_ids", "by_reference", "dataset", "server", "instance",
           "database", "db_user", "version", "capabilities", "extensions",
           "source_document", "note")

# Admin API service type -> portal item type, for the --portal search fallback.
# Extend as new service types appear; an unknown type skips the fallback rather
# than searching for an item type the portal has never heard of.
TYPE_TO_PORTAL_TYPE = {
    "MapServer": "Map Service",
    "FeatureServer": "Feature Service",
    "ImageServer": "Image Service",
    "GeocodeServer": "Geocoding Service",
    "GPServer": "Geoprocessing Service",
    "GeometryServer": "Geometry Service",
    "NAServer": "Network Analysis Service",
    "VectorTileServer": "Vector Tile Service",
    "SceneServer": "Scene Service",
    "StreamServer": "Stream Service",
}


# ----------------------------------------------------------------- pure core

def dict_entries(value):
    """The object entries of a JSON array, for a field a server may fill badly.

    `value or []` covers a field that is null and a field that is absent. It
    does not cover a field holding a number or a string, which is what a
    reverse proxy answering with its own error page looks like once json.loads
    has read it, and what a hand-edited service record looks like. Those used
    to leave the walk with a TypeError out of a list comprehension rather than
    this tool's own message and exit code.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def admin_root(url):
    """The site root an Admin API url is built on.

    People paste what their browser is showing, which is the REST catalog, the
    admin catalog or Manager. All three carry the site root in front of them
    and every one of them ends up here.
    """
    text = (url or "").strip().rstrip("/")
    for tail in ("/admin/services", "/rest/services", "/admin", "/manager"):
        if text.lower().endswith(tail):
            text = text[:-len(tail)].rstrip("/")
    return text


def is_http_url(url):
    """True when urllib will actually open this url.

    A url typed without its scheme is the common mistake, and urllib answers it
    by raising an exception that quotes the whole url back, token included.
    """
    return bool(url) and url.lower().startswith(("http://", "https://"))


def service_path(folder, service, typ):
    """The Admin API path of one service: Folder/Name.Type, or Name.Type."""
    name = (service or "").strip("/")
    if folder:
        return "%s/%s.%s" % (folder.strip("/"), name, typ)
    return "%s.%s" % (name, typ)


def rest_path(folder, service, typ):
    """The REST url tail of one service: Folder/Name/Type.

    The admin catalog spells a service Name.Type and the REST catalog spells it
    Name/Type. The portal records the REST spelling on its items, so the item
    search has to be given that one.
    """
    name = (service or "").strip("/")
    if folder:
        return "%s/%s/%s" % (folder.strip("/"), name, typ)
    return "%s/%s" % (name, typ)


def catalog_records(listing, folder=""):
    """The services in one catalog listing, as {folder, service, type}.

    A listing inside a folder reports serviceName either bare or already
    carrying its folder, depending on the version of ArcGIS Server answering.
    Joining the folder on blindly produced Transport/Transport/Rail.MapServer,
    and every manifest for that site came back unreadable.
    """
    out = []
    for svc in dict_entries(listing.get("services")):
        name = svc.get("serviceName") or svc.get("name") or ""
        typ = svc.get("type") or ""
        if not name or not typ:
            continue
        own, _sep, bare = name.strip("/").rpartition("/")
        out.append({"folder": (folder or own).strip("/"),
                    "service": bare, "type": typ})
    return out


def catalog_folders(listing, skip=SKIP_FOLDERS):
    """The folders of a catalog listing, minus the ones the walk skips."""
    lowered = tuple(s.lower() for s in skip)
    return [f for f in (listing.get("folders") or [])
            if f and f.strip("/").lower() not in lowered]


def parse_conn(text):
    """Parse an ArcGIS connection string into the five fields a row reports.

    Only those five are returned, and the parser is an allowlist rather than a
    denylist for that reason. The string can also carry ENCRYPTED_PASSWORD, the
    saved password of the connection it was published from, and a CSV is a file
    people mail around.
    """
    parsed = {}
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        parsed[key.strip().upper()] = value.strip().strip('"')
    instance = parsed.get("INSTANCE", "")
    # sde:sqlserver:DBHOST1 -> DBHOST1. The host is the last field of the
    # connection form, and it keeps whatever follows it: a named instance
    # (HOST\SQL2019), a port (HOST,1433) or an Oracle service name are all part
    # of the machine's address and dropping them merges two different servers.
    host = instance.rsplit(":", 1)[-1] if instance else parsed.get("SERVER", "")
    return {
        "instance": instance,
        "server": host,
        "database": parsed.get("DATABASE", ""),
        "db_user": parsed.get("USER", parsed.get("USERNAME", "")),
        "version": parsed.get("VERSION", ""),
    }


def connection_string(database):
    """The connection string of one manifest database entry.

    onServerConnectionString is what the server itself uses and is the one to
    report. A service published against a data store registered only on the
    publisher's machine carries the onPremise form alone.
    """
    if not isinstance(database, dict):
        return ""
    text = (database.get("onServerConnectionString")
            or database.get("onPremiseConnectionString") or "")
    # Coerced rather than returned as it arrived. parse_conn is the only reader
    # and it splits the value, so a manifest spelling this field as a number
    # reached it as one and ended the run in an AttributeError.
    return text if isinstance(text, str) else ""


def item_ids(service_json):
    """Every portal item ID backing this service, as [(type, id)].

    A map service shared as a feature service has two portal items, one per
    type, and both are in portalProperties. Reading only the first is how a
    republish preserves the map service's item and silently orphans every web
    map that referenced the feature layer.
    """
    if not isinstance(service_json, dict):
        return []
    props = service_json.get("portalProperties")
    items = (props or {}).get("portalItems") if isinstance(props, dict) else None
    out = []
    for entry in dict_entries(items):
        # itemID is the documented spelling. itemId turns up on older sites and
        # in hand-edited service JSON, and it is the same field.
        ident = entry.get("itemID") or entry.get("itemId") or ""
        if ident:
            out.append((entry.get("type") or "", ident))
    return out


def primary_item_id(pairs, typ):
    """The item ID to preserve for a service of this type.

    The item whose type matches the service is the one whose url the service
    answers on. When nothing matches, the first is reported, because an item ID
    that needs checking is more use than a blank cell.
    """
    for kind, ident in pairs:
        if kind and typ and kind.lower() == typ.lower():
            return ident
    return pairs[0][1] if pairs else ""


def format_item_ids(pairs):
    """Render every item ID for the CSV cell, as Type=id;Type=id."""
    return ";".join("%s=%s" % (kind or "?", ident) for kind, ident in pairs)


def resource_document(manifest):
    """The source document a service was published from, or ''.

    The manifest records the .mxd or .aprx path on the publisher's machine.
    That path is what somebody has to open to republish the service, and it is
    the one thing nobody writes down.
    """
    if not isinstance(manifest, dict):
        return ""
    for res in dict_entries(manifest.get("resources")):
        path = res.get("onPremisePath") or res.get("clientName") or ""
        if path:
            return path
    return ""


def by_reference_text(database):
    """The byReference flag of a manifest database, as a cell.

    False means the data was copied to the server at publish time, so the
    service reads the server's own managed geodatabase. Moving the enterprise
    database it was copied FROM does nothing to it, which is the migration
    surprise this column exists for.
    """
    if not isinstance(database, dict) or "byReference" not in database:
        return ""
    return "true" if database.get("byReference") else "false"


def service_settings(service_json):
    """The service JSON fields the inventory reports beside the data source."""
    if not isinstance(service_json, dict):
        return {"capabilities": "", "extensions": ""}
    return {
        "capabilities": service_json.get("capabilities", "") or "",
        # Enabled extensions are not recreated by a plain republish, so they
        # belong in the parity diff. A disabled extension is not a capability
        # the new service has to match, so only the enabled ones are listed.
        "extensions": ";".join(
            e.get("typeName", "")
            for e in dict_entries(service_json.get("extensions"))
            if str(e.get("enabled")).lower() == "true"),
    }


def rows_for_service(record, service_json, manifest, portal_item_id=""):
    """Every row for one service. Pure: no network, no file.

    One row per dataset, because one service reads several and a row per
    service would have to pick one of them. A service with no dataset still
    gets a row, so that the service count in the CSV is the service count on
    the server.
    """
    folder = record.get("folder", "") or ""
    name = record.get("service", "") or ""
    typ = record.get("type", "") or ""
    pairs = item_ids(service_json)
    chosen = primary_item_id(pairs, typ)
    notes = []
    if not chosen and portal_item_id:
        # The fallback answers for a service whose portalProperties are empty,
        # which is what an unfederated site or a hand-published service looks
        # like. It is a search result rather than the service's own record, so
        # the row says where the ID came from.
        chosen = portal_item_id
        notes.append("item ID found by portal search, not in portalProperties")

    base = dict((column, "") for column in COLUMNS)
    base.update({
        "folder": folder,
        "service": name,
        "type": typ,
        "path": service_path(folder, name, typ),
        "source_item_id": chosen,
        "all_item_ids": format_item_ids(pairs),
    })
    base.update(service_settings(service_json))
    base["source_document"] = resource_document(manifest)

    if not isinstance(manifest, dict):
        row = dict(base)
        if typ in NO_DATASOURCE_TYPES:
            row["status"] = NODATA
            notes.append("a %s reads no data" % typ)
        else:
            row["status"] = UNRESOLVED
            notes.append("no manifest reachable")
        row["note"] = "; ".join(notes)
        return [row]

    databases = dict_entries(manifest.get("databases"))
    if not databases:
        row = dict(base)
        if typ in NO_DATASOURCE_TYPES:
            row["status"] = NODATA
            notes.append("a %s reads no data" % typ)
        else:
            row["status"] = UNRESOLVED
            # The key list is the diagnosis. A manifest holding only resources
            # is a service published from a document whose layers were all
            # copied, and that reads differently from a manifest that failed.
            notes.append("no databases in the manifest; keys=%s"
                         % ",".join(sorted(manifest.keys())))
        row["note"] = "; ".join(notes)
        return [row]

    out = []
    for database in databases:
        conn = parse_conn(connection_string(database))
        reference = by_reference_text(database)
        db_notes = list(notes)
        status = OK
        if reference == "false":
            status = COPIED
            db_notes.append("data copied to the server at publish time")
        datasets = dict_entries(database.get("datasets"))
        for dataset in (datasets or [{}]):
            row = dict(base)
            row.update(conn)
            row["status"] = status
            row["by_reference"] = reference
            row["dataset"] = (dataset.get("onServerName")
                              or dataset.get("onPremisePath") or "")
            row["note"] = "; ".join(db_notes)
            out.append(row)
    return out


def summarize(rows):
    """Counts the summary prints and the gate reads."""
    services = []
    seen = set()
    for row in rows:
        key = (row.get("folder", ""), row.get("service", ""), row.get("type", ""))
        if key not in seen:
            seen.add(key)
            services.append(row)
    sources = {}
    for row in rows:
        if row.get("status") in (OK, COPIED) and (row.get("database")
                                                  or row.get("server")):
            key = (row.get("database", ""), row.get("server", ""))
            sources[key] = sources.get(key, 0) + 1
    unresolved = [r for r in rows if r.get("status") == UNRESOLVED]
    copied = set((r.get("folder"), r.get("service"), r.get("type"))
                 for r in rows if r.get("status") == COPIED)
    with_item = set((r.get("folder"), r.get("service"), r.get("type"))
                    for r in rows if r.get("source_item_id"))
    return {
        "services": len(services),
        "rows": len(rows),
        "sources": sorted(((db, host, n) for (db, host), n in sources.items()),
                          key=lambda t: (-t[2], t[0], t[1])),
        "unresolved": unresolved,
        "copied": len(copied),
        "with_item_id": len(with_item),
        "without_item_id": len(services) - len(with_item),
    }


def describe(summary, sample=10):
    """Render a summary as the lines the CLI prints."""
    out = ["services: %d" % summary["services"],
           "rows: %d" % summary["rows"]]
    if summary["sources"]:
        out.append("")
        out.append("data sources:")
        for database, host, count in summary["sources"][:sample]:
            out.append("  %-28s %-24s %4d row(s)"
                       % (database or "(none)", host or "(none)", count))
        if len(summary["sources"]) > sample:
            out.append("  ...and %d more" % (len(summary["sources"]) - sample))
    out.append("")
    out.append("portal item IDs: %d present, %d missing"
               % (summary["with_item_id"], summary["without_item_id"]))
    if summary["copied"]:
        out.append("data copied to the server: %d service(s). Moving the "
                   "source geodatabase does not move these."
                   % summary["copied"])
    if summary["unresolved"]:
        out.append("")
        out.append("unresolved data sources: %d" % len(summary["unresolved"]))
        for row in summary["unresolved"][:sample]:
            out.append("  %s: %s" % (row.get("path", ""), row.get("note", "")))
        if len(summary["unresolved"]) > sample:
            out.append("  ...and %d more"
                       % (len(summary["unresolved"]) - sample))
    return out


def exit_code(summary):
    """0 when every data source was read, 1 when any was not."""
    return 1 if summary["unresolved"] else 0


def redact(text, *secrets):
    """Remove secrets from anything about to be printed or written.

    generateToken is a POST and urllib repeats the request in some of its
    exceptions, so an unredacted error message is a password in a log file. A
    token is worse: it travels as a query parameter, and urllib answers a url
    it cannot open by quoting that whole url back.
    """
    out = "%s" % (text,)
    for secret in secrets:
        if secret:
            # Coerced, because this runs inside an exception handler where a
            # secret that arrived as anything but a string used to raise
            # TypeError and throw the redaction away with it.
            out = out.replace("%s" % (secret,), REDACTED)
    return out


# ------------------------------------------------------------------ server io

def _opener(insecure):
    """Build a urllib opener, optionally without certificate verification.

    Verification is on. The harvested version of this script disabled it at
    import time for an internal self-signed certificate, which is a sensible
    thing to do on one site and an indefensible default in a published tool:
    the flag makes the decision visible in the command that made it.
    """
    if not insecure:
        return urllib.request.build_opener()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context))


def _call(endpoint, params, insecure=False, post=False, secret=None,
          timeout=HTTP_TIMEOUT):
    """One Admin API call returning parsed JSON, with the site's errors raised.

    ArcGIS answers HTTP 200 with an error object in the body, so the status
    code proves nothing and the body has to be read every time.
    """
    params = dict(params)
    params["f"] = "json"
    data = urllib.parse.urlencode(params).encode("utf-8")
    token = params.get("token")
    password = params.get("password")
    try:
        opener = _opener(insecure)
        if post:
            response = opener.open(endpoint, data, timeout=timeout)
        else:
            response = opener.open("%s?%s" % (endpoint, data.decode("utf-8")),
                                   timeout=timeout)
        body = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise RuntimeError(redact("%s: %s" % (endpoint, exc), secret, token,
                                  password))
    if not isinstance(body, dict):
        # A proxy error page, a captive portal and a load balancer notice can
        # all parse as valid JSON without being an ArcGIS response, and every
        # caller below goes straight to body.get().
        raise RuntimeError("%s: answered with a JSON %s, not an object, so "
                           "this is not an ArcGIS response"
                           % (endpoint, type(body).__name__))
    if "error" in body:
        error = body["error"] if isinstance(body["error"], dict) else {}
        raise RuntimeError(redact(
            "%s: %s %s" % (endpoint, error.get("code"),
                           error.get("message") or body["error"]),
            secret, token, password))
    return body


def generate_token(root, user, secret, insecure=False, portal=False):
    """Exchange a username and password for a short-lived token."""
    endpoint = ("%s/generateToken" % root.rstrip("/") if portal
                else "%s/admin/generateToken" % root)
    body = _call(endpoint, {"username": user, "password": secret,
                            "client": "requestip", "expiration": 60},
                 insecure=insecure, post=True, secret=secret)
    token = body.get("token")
    if not token:
        raise RuntimeError("generateToken returned no token")
    return token


def json_getter(token, insecure=False, timeout=HTTP_TIMEOUT):
    """Build the get_json callable the walk and the inventory drive."""
    def get_json(url):
        return _call(url, {"token": token} if token else {}, insecure=insecure,
                     secret=token, timeout=timeout)
    return get_json


def walk_catalog(get_json, root, max_folders=MAX_FOLDERS):
    """Every service on the site, root folder first.

    A catalog listing that cannot be read raises. A half-read catalog is worse
    than no catalog: the services it missed are exactly the ones nobody knows
    about, and they are the reason somebody ran an inventory.
    """
    base = "%s/admin/services" % root
    records = catalog_records(get_json(base))
    seen = set()
    queue = catalog_folders(get_json(base))
    while queue:
        folder = queue.pop(0)
        key = folder.strip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > max_folders:
            raise RuntimeError(
                "stopped after %d folders. The catalog is either enormous or "
                "it points back at itself, and a partial inventory would "
                "report the services it never reached as absent."
                % max_folders)
        listing = get_json("%s/%s" % (base, folder.strip("/")))
        records.extend(catalog_records(listing, folder.strip("/")))
        queue.extend(catalog_folders(listing))
    return records


def portal_searcher(portal, token, public_rest_root, insecure=False,
                    timeout=HTTP_TIMEOUT):
    """Build the item-ID fallback, or None when it was not asked for.

    Best effort by design. It returns '' on any lookup failure rather than
    raising, so that a portal whose token security is configured differently
    never stops the inventory the operator actually asked for.
    """
    if not (portal and token and public_rest_root):
        return None

    def search(record):
        portal_type = TYPE_TO_PORTAL_TYPE.get(record.get("type", ""), "")
        if not portal_type:
            return ""
        url = "%s/%s" % (public_rest_root.rstrip("/"),
                         rest_path(record.get("folder", ""),
                                   record.get("service", ""),
                                   record.get("type", "")))
        query = 'type:"%s" AND url:"%s"' % (portal_type, url)
        try:
            body = _call("%s/search" % portal.rstrip("/"),
                         {"q": query, "num": 1, "token": token},
                         insecure=insecure, secret=token, timeout=timeout)
        except RuntimeError:
            return ""
        results = body.get("results") or []
        if not results or not isinstance(results[0], dict):
            return ""
        return results[0].get("id", "") or ""
    return search


def inventory(get_json, root, records, search=None, echo=None,
              dump_manifest=None):
    """Build every row for every service. One Admin API call each, plus one.

    A service whose own JSON or manifest will not answer is reported with a
    note rather than dropped. The catalog is the list of services on the site,
    and a service missing from the CSV reads as a service that is not there.
    """
    rows = []
    for record in records:
        path = service_path(record.get("folder", ""), record.get("service", ""),
                            record.get("type", ""))
        if echo:
            echo("  %s" % path)
        try:
            service_json = get_json("%s/admin/services/%s" % (root, path))
        except RuntimeError:
            service_json = None
        try:
            manifest = get_json(
                "%s/admin/services/%s/iteminfo/manifest/manifest.json"
                % (root, path))
        except RuntimeError:
            manifest = None
        if dump_manifest is not None and manifest is not None:
            dump_manifest(path, manifest)
        fallback = ""
        if search and not item_ids(service_json):
            fallback = search(record)
        rows.extend(rows_for_service(record, service_json, manifest, fallback))
    return rows


def write_csv(path, rows):
    """Write the inventory. The header is written even for no rows.

    A site with nothing on it produces a file with one header line, not an
    empty file. An empty file is indistinguishable from a run that died, and
    the difference matters when the file is the cutover evidence.
    """
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    # newline="" is not decoration. Without it the csv module's carriage return
    # meets the one the text layer adds on Windows, and every other line of the
    # file is blank, which Excel reads as an empty row between services.
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS),
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict((c, row.get(c, "")) for c in COLUMNS))
    return path


def write_manifests(path, dumps):
    """Write the raw manifests --dump-manifest collected, as one JSON file."""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with io.open(path, "w", encoding="utf-8") as handle:
        json.dump(dumps, handle, indent=1, sort_keys=True)
    return path


def read_secret(user, env=SECRET_ENV, prompt=None):
    """The password, from the environment or an unechoed prompt. Never argv."""
    secret = os.environ.get(env)
    if secret:
        return secret
    return getpass.getpass(prompt or "password for %s (not echoed): " % user)


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core and the whole command line.

    No server, no portal, no network, no credentials. The Admin API is answered
    inside this process, so the walk, the manifest read, the token exchange and
    every failure they can return are exercised without a socket.
    """
    import contextlib
    import shutil
    import tempfile

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, kind=RuntimeError):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("svcsource self-test: no server, no portal, no network, no token")
    print("-" * 70)

    # ---- the site root, from whatever the operator had in the address bar
    check(admin_root("https://gis.example.com/arcgis")
          == "https://gis.example.com/arcgis", "a site root is left alone")
    check(admin_root("https://gis.example.com/arcgis/")
          == "https://gis.example.com/arcgis", "a trailing slash is trimmed")
    check(admin_root("https://gis.example.com/arcgis/rest/services")
          == "https://gis.example.com/arcgis",
          "the REST catalog url people paste is trimmed to the site root")
    check(admin_root("https://gis.example.com/arcgis/admin/services/")
          == "https://gis.example.com/arcgis",
          "the admin catalog url is trimmed to the site root")
    check(admin_root("https://gis.example.com/arcgis/admin")
          == "https://gis.example.com/arcgis", "a bare /admin is trimmed")
    check(admin_root("https://gis.example.com/arcgis/manager")
          == "https://gis.example.com/arcgis",
          "the Manager url is trimmed to the site root")
    check(admin_root("https://gis.example.com:6443/arcgis")
          == "https://gis.example.com:6443/arcgis",
          "the direct 6443 port is kept, it is a different door to the site")
    check(admin_root("  https://gis.example.com/arcgis  ")
          == "https://gis.example.com/arcgis", "surrounding space is trimmed")
    check(admin_root("") == "" and admin_root(None) == "",
          "a missing server url trims to nothing rather than raising")
    check(admin_root("https://gis.example.com/arcgis/REST/Services")
          == "https://gis.example.com/arcgis",
          "the catalog tail is trimmed whatever case the browser showed it in")
    check(is_http_url("https://gis.example.com/arcgis") is True,
          "an https url is openable")
    check(is_http_url("http://gis.example.com/arcgis") is True,
          "a plain http site is openable too, because an internal site on 6080 "
          "is the case --insecure exists for")
    check(is_http_url("HTTPS://GIS.EXAMPLE.COM/arcgis") is True,
          "a scheme typed in capitals is still a scheme")
    check(is_http_url("ftp://gis.example.com/arcgis") is False,
          "a scheme urllib will not open for this tool is refused")
    check(is_http_url("gis.example.com/arcgis") is False,
          "a url with no scheme is refused before urllib quotes it back"
          "  <-- pinned defect")
    check(is_http_url("") is False, "an empty server url is not openable")

    # ---- the two spellings of one service
    check(service_path("Transport", "Roads", "MapServer")
          == "Transport/Roads.MapServer",
          "the admin path of a service in a folder is Folder/Name.Type")
    check(service_path("", "Roads", "MapServer") == "Roads.MapServer",
          "the admin path of a root service is Name.Type")
    check(rest_path("Transport", "Roads", "MapServer")
          == "Transport/Roads/MapServer",
          "the REST path of the same service is Folder/Name/Type"
          "  <-- pinned defect")
    check(rest_path("", "Roads", "MapServer") == "Roads/MapServer",
          "the REST path of a root service is Name/Type")
    check(service_path("/Transport/", "/Roads/", "MapServer")
          == "Transport/Roads.MapServer",
          "stray slashes on a folder or a name do not reach the path")

    # ---- the catalog listing, in both shapes ArcGIS Server answers it
    root_listing = {"folders": ["Transport", "System", "Utilities"],
                    "services": [{"serviceName": "Parcels",
                                  "type": "FeatureServer"},
                                 {"serviceName": "Roads", "type": "MapServer"}]}
    records = catalog_records(root_listing)
    check(len(records) == 2, "both root services are read from the catalog")
    check(records[0] == {"folder": "", "service": "Parcels",
                         "type": "FeatureServer"},
          "a root service is recorded with no folder")
    folded = catalog_records({"services": [{"serviceName": "Transport/Rail",
                                            "type": "MapServer"}]},
                             "Transport")
    check(folded == [{"folder": "Transport", "service": "Rail",
                      "type": "MapServer"}],
          "a serviceName that already carries its folder is not doubled"
          "  <-- pinned defect")
    bare = catalog_records({"services": [{"serviceName": "Rail",
                                          "type": "MapServer"}]}, "Transport")
    check(bare == [{"folder": "Transport", "service": "Rail",
                    "type": "MapServer"}],
          "a bare serviceName inside a folder is recorded with that folder")
    check(catalog_records({"services": [{"serviceName": "Transport/Rail",
                                         "type": "MapServer"}]})
          == [{"folder": "Transport", "service": "Rail",
               "type": "MapServer"}],
          "a folder carried on the name is read when the listing gives none")
    check(catalog_records({"services": [{"serviceName": "Other/Rail",
                                         "type": "MapServer"}]}, "Transport")
          == [{"folder": "Transport", "service": "Rail",
               "type": "MapServer"}],
          "the folder the walk asked for wins over a different one on the "
          "name, because that is the folder the manifest answers under")
    check(catalog_records({"services": [{"name": "Roads",
                                         "type": "MapServer"}]})
          == [{"folder": "", "service": "Roads", "type": "MapServer"}],
          "a listing spelling the key name rather than serviceName is read")
    check(catalog_records({"services": [{"serviceName": "Roads"},
                                        {"type": "MapServer"}]}) == [],
          "a listing entry with no type or no name is skipped, not guessed at")
    check(catalog_records({}) == [] and catalog_records({"services": None}) == [],
          "a catalog with no services reads as no services")

    # ---- every JSON array the site answers with, filled with something else.
    # A reverse proxy answering with its own error page, or a hand-edited
    # service record, used to end the walk in a TypeError with a traceback
    # instead of this tool's message, its redaction and its exit 2.
    check(dict_entries([{"a": 1}, None, 3, "x", []]) == [{"a": 1}],
          "the entries of a JSON array that are objects are the ones read")
    check(dict_entries(None) == [] and dict_entries("an error page") == []
          and dict_entries(7) == [] and dict_entries({"a": 1}) == [],
          "an array field holding anything but an array reads as empty")
    check(catalog_records({"services": [None, 3, "x"]}) == [],
          "a catalog whose service entries are not objects is read as empty, "
          "not as a traceback  <-- pinned defect")
    check(catalog_records({"services": "<html>502 Bad Gateway</html>"}) == [],
          "and neither is an error page a proxy returned in its place"
          "  <-- pinned defect")
    check(item_ids({"portalProperties": {"portalItems": 7}}) == [],
          "portalItems that is not an array reports no item ID")
    check(resource_document({"resources": 3}) == "",
          "resources that is not an array reports no source document")
    check(service_settings({"extensions": 5})["extensions"] == "",
          "extensions that is not an array lists no extension")
    check(rows_for_service({"service": "Odd", "type": "MapServer"}, {},
                           {"databases": [{"datasets": 7,
                                           "onServerConnectionString":
                                           "DATABASE=gisdb"}]}
                           )[0]["dataset"] == "",
          "datasets that is not an array still reports the database it is on"
          "  <-- pinned defect")
    check(rows_for_service({"service": "Odd", "type": "MapServer"}, {},
                           {"databases": "nonsense"})[0]["status"] == UNRESOLVED,
          "databases that is not an array is unresolved, not an empty success")
    check(catalog_folders(root_listing) == ["Transport"],
          "System and Utilities are not walked")
    check(catalog_folders({"folders": ["system", "UTILITIES", "Water"]})
          == ["Water"],
          "the skipped folder names are matched whatever their case")
    check(catalog_folders(root_listing, skip=()) ==
          ["Transport", "System", "Utilities"],
          "an empty skip list walks every folder")
    check(catalog_folders({}) == [], "a catalog with no folders reads as none")

    # ---- the connection string, which is where the migration answer lives
    sde = ('SERVER=DBHOST1;INSTANCE=sde:sqlserver:DBHOST1;DBCLIENT=sqlserver;'
           'DB_CONNECTION_PROPERTIES=DBHOST1;DATABASE=gisdb;USER=sdeowner;'
           'VERSION=sde.DEFAULT;AUTHENTICATION_MODE=DBMS')
    conn = parse_conn(sde)
    check(conn["database"] == "gisdb", "the database name is read")
    check(conn["server"] == "DBHOST1",
          "sde:sqlserver:HOST reports HOST as the machine")
    check(conn["instance"] == "sde:sqlserver:DBHOST1",
          "the whole instance string is kept beside it")
    check(conn["db_user"] == "sdeowner", "the database user is read")
    check(conn["version"] == "sde.DEFAULT",
          "the geodatabase version is read, because a service on a child "
          "version reads different rows")
    check(sorted(conn) == ["database", "db_user", "instance", "server",
                           "version"],
          "the parser reports five fields and nothing else")
    secret_conn = parse_conn(
        'SERVER=DBHOST1;INSTANCE=sde:sqlserver:DBHOST1;DATABASE=gisdb;'
        'USER=sdeowner;PASSWORD=hunter2;ENCRYPTED_PASSWORD=00022e59ab;'
        'VERSION=sde.DEFAULT')
    check(secret_conn["database"] == "gisdb"
          and secret_conn["db_user"] == "sdeowner"
          and not any("hunter2" in v or "00022e59ab" in v
                      for v in secret_conn.values()),
          "a saved password in the connection string reaches no column"
          "  <-- pinned defect")
    named = parse_conn("INSTANCE=sde:sqlserver:DBHOST1\\SQL2019;DATABASE=GIS")
    check(named["server"] == "DBHOST1\\SQL2019",
          "a named SQL Server instance stays attached to its host"
          "  <-- pinned defect")
    ported = parse_conn("INSTANCE=sde:sqlserver:10.0.0.5,1433;DATABASE=GIS")
    check(ported["server"] == "10.0.0.5,1433",
          "a host carrying a port stays whole, two ports are two servers")
    check(parse_conn("INSTANCE=sde:oracle11g:orcl")["server"] == "orcl",
          "an Oracle service name is read as the machine's address")
    check(parse_conn("INSTANCE=sde:postgresql:pgbox;DATABASE=gis")["server"]
          == "pgbox", "a PostgreSQL instance reports its host")
    check(parse_conn("SERVER=DBHOST1;DATABASE=gisdb")["server"] == "DBHOST1",
          "a connection string with no INSTANCE falls back to SERVER")
    check(parse_conn("DATABASE=C:\\geodata\\parcels.gdb")["database"]
          == "C:\\geodata\\parcels.gdb",
          "a file geodatabase reports its path, and its host is blank"
          "  <-- pinned defect")
    check(parse_conn("DATABASE=C:\\geodata\\parcels.gdb")["server"] == "",
          "a file geodatabase names no server, and the cell is left empty")
    check(parse_conn('DATABASE="GIS Data";USER="gis admin"')["database"]
          == "GIS Data",
          "a quoted value with a space in it is unquoted")
    check(parse_conn("database=geodata;Instance=sde:sqlserver:h")["database"]
          == "geodata", "the keys are read whatever their case")
    check(parse_conn(" DATABASE = gisdb ")["database"] == "gisdb",
          "space around a key or a value is trimmed")
    check(parse_conn("USERNAME=dbuser")["db_user"] == "dbuser",
          "the USERNAME spelling is read as the database user")
    check(parse_conn("")["instance"] == "" and parse_conn(None)["database"] == "",
          "an empty connection string parses to empty fields, not an error")
    check(parse_conn("GARBAGE")["database"] == "",
          "a connection string with no key at all parses to empty fields")
    check(parse_conn("DATABASE=a=b")["database"] == "a=b",
          "a value containing an equals sign is not split twice")

    check(connection_string({"onServerConnectionString": "A",
                             "onPremiseConnectionString": "B"}) == "A",
          "the server's own connection string wins over the publisher's")
    check(connection_string({"onPremiseConnectionString": "B"}) == "B",
          "the publisher's connection string is read when it is the only one"
          "  <-- pinned defect")
    check(connection_string({"onServerConnectionString": ""}) == "",
          "a blank connection string is blank, not None")
    check(connection_string(None) == "" and connection_string([]) == "",
          "a database entry that is not an object reads as no connection")
    check(connection_string({"onServerConnectionString": 5}) == ""
          and parse_conn(connection_string({"onServerConnectionString": 5}))
          == parse_conn(""),
          "a connection string spelled as a number reads as no connection, "
          "because parse_conn splits it  <-- pinned defect")

    # ---- the portal item IDs, the field that survives a republish or does not
    two_items = {"portalProperties": {"portalItems": [
        {"itemID": "aaa11111111111111111111111111111", "type": "MapServer"},
        {"itemID": "bbb22222222222222222222222222222",
         "type": "FeatureServer"}]}}
    pairs = item_ids(two_items)
    check(len(pairs) == 2, "both portal items of one service are read")
    check(primary_item_id(pairs, "MapServer")
          == "aaa11111111111111111111111111111",
          "the item ID reported is the one whose type matches the service")
    check(primary_item_id(pairs, "FeatureServer")
          == "bbb22222222222222222222222222222",
          "the same service as a feature service reports the other item ID"
          "  <-- pinned defect")
    check(format_item_ids(pairs) == ("MapServer=aaa11111111111111111111111111111;"
                                     "FeatureServer=bbb22222222222222222222222222222"),
          "every item ID is listed, so a republish preserves both")
    check(primary_item_id(pairs, "ImageServer")
          == "aaa11111111111111111111111111111",
          "an item ID that matches no type is still reported for checking")
    # The lowercase spelling is deliberately second. First, a case-sensitive
    # comparison would match nothing, fall through to "the first item" and
    # return the same ID anyway, so the assertion would pass either way.
    check(primary_item_id([("MapServer", "eee55555555555555555555555555555"),
                           ("featureserver", "ddd44444444444444444444444444444")],
                          "FeatureServer")
          == "ddd44444444444444444444444444444",
          "the type match ignores case, because portalProperties is "
          "hand-edited often enough to spell it featureserver")
    check(item_ids({"portalProperties": {"portalItems": [
        {"itemId": "ccc33333333333333333333333333333"}]}})
          == [("", "ccc33333333333333333333333333333")],
          "the itemId spelling is read as the same field  <-- pinned defect")
    check(item_ids({"portalProperties": {"portalItems": []}}) == [],
          "a service shared with nobody reports no item ID")
    check(item_ids({"portalProperties": {}}) == [],
          "portalProperties with no items reports no item ID")
    check(item_ids({}) == [], "an unfederated service reports no item ID")
    check(item_ids(None) == [], "a service whose JSON never arrived reports none")
    check(item_ids({"portalProperties": None}) == [],
          "a null portalProperties reports no item ID rather than raising")
    check(item_ids({"portalProperties": {"portalItems": [{"itemID": ""},
                                                         {"type": "MapServer"}]}})
          == [], "an item entry with a blank ID is not an item ID")
    check(item_ids({"portalProperties": {"portalItems": ["notadict"]}}) == [],
          "an item entry that is not an object is skipped")
    check(item_ids({"portalProperties": "a string"}) == [],
          "portalProperties that is not an object reports no item ID")
    check(primary_item_id([], "MapServer") == "",
          "no items means no item ID to preserve")
    check(format_item_ids([("", "abc")]) == "?=abc",
          "an item ID whose type is absent is listed under a question mark")

    # ---- the rest of the service JSON
    svc = {"capabilities": "Map,Query,Data",
           "extensions": [{"typeName": "FeatureServer", "enabled": "true"},
                          {"typeName": "WFSServer", "enabled": "false"},
                          {"typeName": "WMSServer", "enabled": True}]}
    settings = service_settings(svc)
    check(settings["capabilities"] == "Map,Query,Data",
          "the capabilities string is read")
    check(settings["extensions"] == "FeatureServer;WMSServer",
          "only the enabled extensions are listed  <-- pinned defect")
    check(service_settings({})["extensions"] == "",
          "a service with no extensions lists none")
    check(service_settings(None) == {"capabilities": "", "extensions": ""},
          "a service whose JSON never arrived reports empty settings")
    check(service_settings({"extensions": [None, "x"]})["extensions"] == "",
          "an extension entry that is not an object is skipped")

    check(by_reference_text({"byReference": True}) == "true",
          "a registered data source reports byReference true")
    check(by_reference_text({"byReference": False}) == "false",
          "a copied data source reports byReference false")
    check(by_reference_text({}) == "",
          "a manifest that does not say leaves the cell empty rather than "
          "guessing that the data was registered")
    check(by_reference_text(None) == "",
          "a database entry that is not an object leaves the cell empty")

    check(resource_document({"resources": [
        {"clientName": "GISDESK", "onPremisePath": "C:\\maps\\parcels.aprx"}]})
        == "C:\\maps\\parcels.aprx",
        "the document a service was published from is reported")
    check(resource_document({"resources": [{"clientName": "GISDESK"}]})
          == "GISDESK",
          "a resource with no path reports the machine it was published from")
    check(resource_document({"resources": []}) == "",
          "a manifest with no resources reports no source document")
    check(resource_document({}) == "" and resource_document(None) == "",
          "an absent manifest reports no source document")
    check(resource_document({"resources": [None, {"onPremisePath": "m.mxd"}]})
          == "m.mxd", "a resource entry that is not an object is skipped")
    check(resource_document({"resources": [{"clientName": ""},
                                           {"onPremisePath": "m.mxd"}]})
          == "m.mxd",
          "a resource naming nothing is stepped over to reach the one that "
          "does")

    # ---- one service to its rows, which is the whole tool
    roads = {"folder": "Transport", "service": "Roads", "type": "MapServer"}
    two_db = {
        "databases": [
            {"byReference": True,
             "onServerConnectionString":
                 "SERVER=DBHOST1;INSTANCE=sde:sqlserver:DBHOST1;"
                 "DATABASE=gisdb;USER=sdeowner;VERSION=sde.DEFAULT",
             "datasets": [{"onServerName": "gisdb.sdeowner.Roads"}]},
            {"byReference": True,
             "onServerConnectionString":
                 "SERVER=DBHOST2;INSTANCE=sde:sqlserver:DBHOST2;"
                 "DATABASE=addressdb;USER=sdeowner;VERSION=sde.DEFAULT",
             "datasets": [{"onServerName": "addressdb.sdeowner.AddressPoints"}]}],
        "resources": [{"onPremisePath": "C:\\maps\\roads.mxd"}]}
    rows = rows_for_service(roads, dict(svc, **two_items), two_db)
    check(len(rows) == 2,
          "a service reading two databases produces two rows  <-- pinned defect")
    check([r["database"] for r in rows] == ["gisdb", "addressdb"],
          "each row names its own database, in manifest order")
    check([r["server"] for r in rows] == ["DBHOST1", "DBHOST2"],
          "each row names its own server")
    check(rows[0]["dataset"] == "gisdb.sdeowner.Roads",
          "the dataset is the name the server sees")
    check(rows[0]["path"] == "Transport/Roads.MapServer",
          "every row carries the admin path of its service")
    check(all(r["status"] == OK for r in rows),
          "a registered data source that parsed is ok")
    # The four status words spelled out. They are cell values in a published
    # file, so a gate written as `status == "unresolved"` in somebody's
    # spreadsheet breaks if one is renamed, and a comparison phrased against
    # the constants would not notice.
    check((OK, COPIED, NODATA, UNRESOLVED)
          == ("ok", "copied", "no-datasource", "unresolved"),
          "the status column says ok, copied, no-datasource or unresolved, "
          "which is what the README documents and what a filter matches on")
    check(all(r["source_item_id"] == "aaa11111111111111111111111111111"
              for r in rows),
          "the item ID to preserve is repeated on every row of the service")
    check(all(r["source_document"] == "C:\\maps\\roads.mxd" for r in rows),
          "the source document is repeated on every row of the service")
    check(all(r["capabilities"] == "Map,Query,Data" for r in rows),
          "the capabilities are repeated on every row of the service")
    check(sorted(rows[0]) == sorted(COLUMNS),
          "a row carries exactly the columns the CSV has")

    multi_ds = {"databases": [{
        "byReference": True,
        "onServerConnectionString": "INSTANCE=sde:sqlserver:DBHOST1;"
                                    "DATABASE=gisdb",
        "datasets": [{"onServerName": "gisdb.sdeowner.Roads"},
                     {"onServerName": "gisdb.sdeowner.Bridges"},
                     {"onPremisePath": "C:\\geodata\\hydrants.gdb\\Hydrants"}]}]}
    rows = rows_for_service(roads, {}, multi_ds)
    check(len(rows) == 3, "one database holding three datasets makes three rows")
    check(rows[2]["dataset"] == "C:\\geodata\\hydrants.gdb\\Hydrants",
          "a dataset with no server name reports its path instead")
    check(len(set(r["database"] for r in rows)) == 1,
          "every dataset of one database repeats that database")

    no_ds = {"databases": [{"byReference": True,
                            "onServerConnectionString":
                                "INSTANCE=sde:sqlserver:DBHOST1;"
                                "DATABASE=gisdb"}]}
    rows = rows_for_service(roads, {}, no_ds)
    check(len(rows) == 1 and rows[0]["database"] == "gisdb",
          "a database listing no dataset still reports the database")
    check(rows[0]["dataset"] == "",
          "and its dataset cell is empty rather than absent")

    copied = {"databases": [{
        "byReference": False,
        "onServerConnectionString": "SERVER=MAPSRV1;"
                                    "INSTANCE=sde:sqlserver:MAPSRV1;"
                                    "DATABASE=managed",
        "datasets": [{"onServerName": "managed.dbo.Roads"}]}]}
    rows = rows_for_service(roads, {}, copied)
    check(rows[0]["status"] == COPIED,
          "data copied to the server is reported as copied, not as ok"
          "  <-- pinned defect")
    check(rows[0]["by_reference"] == "false",
          "and the byReference flag is in its own column")
    check("copied to the server" in rows[0]["note"],
          "the note says the source geodatabase is not the one it reads")

    no_db = {"resources": [{"onPremisePath": "C:\\maps\\cached.mxd"}],
             "operationalLayers": []}
    rows = rows_for_service(roads, {}, no_db)
    check(len(rows) == 1, "a manifest with no databases still makes one row")
    check(rows[0]["status"] == UNRESOLVED,
          "a manifest with no databases is unresolved, not silently ok"
          "  <-- pinned defect")
    check("keys=operationalLayers,resources" in rows[0]["note"],
          "and the note lists the keys the manifest did have")
    check(rows[0]["source_document"] == "C:\\maps\\cached.mxd",
          "the source document is still reported for an unresolved service")

    rows = rows_for_service(roads, dict(svc, **two_items), None)
    check(len(rows) == 1 and rows[0]["status"] == UNRESOLVED,
          "a service whose manifest never answered is one unresolved row"
          "  <-- pinned defect")
    check(rows[0]["note"] == "no manifest reachable",
          "and the note says the manifest could not be read")
    check(rows[0]["source_item_id"] == "aaa11111111111111111111111111111",
          "an unresolved service still reports the item ID it must preserve")

    geom = {"folder": "Utilities", "service": "Geometry",
            "type": "GeometryServer"}
    rows = rows_for_service(geom, {}, None)
    check(rows[0]["status"] == NODATA,
          "a GeometryServer with no manifest is expected, not a failure"
          "  <-- pinned defect")
    check(rows[0]["note"] == "a GeometryServer reads no data",
          "and the note says why it has no data source")
    check(rows_for_service(geom, {}, {"resources": []})[0]["status"] == NODATA,
          "a GeometryServer whose manifest holds no database is expected too")

    rows = rows_for_service(roads, {}, two_db,
                            portal_item_id="ddd44444444444444444444444444444")
    check(rows[0]["source_item_id"] == "ddd44444444444444444444444444444",
          "the portal search fills in an item ID portalProperties did not have")
    check("portal search" in rows[0]["note"],
          "and the row says the ID came from a search, not from the service")
    rows = rows_for_service(roads, two_items, two_db,
                            portal_item_id="ddd44444444444444444444444444444")
    check(rows[0]["source_item_id"] == "aaa11111111111111111111111111111",
          "the service's own record wins over a search result")
    check("portal search" not in rows[0]["note"],
          "and no note is added when the fallback was not needed")

    rows = rows_for_service({"folder": "", "service": "Parcels",
                             "type": "FeatureServer"}, None, None)
    check(rows[0]["path"] == "Parcels.FeatureServer" and rows[0]["folder"] == "",
          "a root service whose JSON never arrived is still one row")
    check(rows[0]["capabilities"] == "",
          "and its settings columns are empty rather than absent")
    check(rows_for_service({}, {}, {"databases": [None, "x"]})[0]["status"]
          == UNRESOLVED,
          "a databases list holding no object is unresolved, not a crash")
    check(rows_for_service({}, {}, {"databases": [
        {"onServerConnectionString": "DATABASE=GIS", "datasets": [None]}]}
    )[0]["dataset"] == "",
          "a dataset entry that is not an object is skipped, not a crash")

    # ---- the summary and the gate a scheduled job reads
    site_rows = (rows_for_service(roads, dict(svc, **two_items), two_db)
                 + rows_for_service({"folder": "", "service": "Parcels",
                                     "type": "FeatureServer"}, {}, two_db)
                 + rows_for_service({"folder": "", "service": "Cached",
                                     "type": "MapServer"}, {}, None)
                 + rows_for_service(geom, {}, None)
                 + rows_for_service({"folder": "", "service": "Managed",
                                     "type": "FeatureServer"}, {}, copied))
    summary = summarize(site_rows)
    check(summary["services"] == 5,
          "the summary counts services, not rows  <-- pinned defect")
    check(summary["rows"] == len(site_rows), "and reports the row count beside it")
    check(summary["sources"][0] == ("addressdb", "DBHOST2", 2),
          "the data sources are ranked by how many rows read them")
    check(len(summary["unresolved"]) == 1,
          "the cached service is the only unresolved one")
    check(summary["unresolved"][0]["service"] == "Cached",
          "and the summary keeps the row so the report can name it")
    check(summary["copied"] == 1, "the copied service is counted once")
    check((summary["with_item_id"], summary["without_item_id"]) == (1, 4),
          "the services with no item ID to preserve are counted")
    check(exit_code(summary) == 1,
          "one unreadable data source fails the run  <-- pinned defect")
    check(exit_code(summarize([r for r in site_rows
                               if r["service"] != "Cached"])) == 0,
          "a site whose every data source was read passes")
    check(exit_code(summarize([])) == 0,
          "a site with no services at all passes, it has nothing unresolved")
    check(summarize([])["services"] == 0, "and reports no services")
    check(summarize([])["sources"] == [], "and lists no data sources")
    # A file geodatabase source has a path in database and nothing in server.
    # Requiring both fields dropped every file-geodatabase service out of the
    # data source ranking, which is where a migration reads its scope.
    gdb_row = rows_for_service(
        {"folder": "", "service": "Aerials", "type": "MapServer"}, {},
        {"databases": [{"onServerConnectionString":
                        "DATABASE=D:\\gisdata\\aerials.gdb",
                        "byReference": True,
                        "datasets": [{"onServerName": "Imagery2024"}]}]})
    check(summarize(gdb_row)["sources"]
          == [("D:\\gisdata\\aerials.gdb", "", 1)],
          "a file geodatabase with no host is still counted as a data source"
          "  <-- pinned defect")
    mixed = gdb_row + rows_for_service(
        {"folder": "", "service": "Gone", "type": "MapServer"}, {}, None)
    check(summarize(mixed)["sources"] == [("D:\\gisdata\\aerials.gdb", "", 1)]
          and len(summarize(mixed)["unresolved"]) == 1,
          "an unresolved service is listed as unresolved and not as a source")
    # The status decides, not whether the row happens to carry a database. A
    # half-read manifest can leave both, and counting it would report a data
    # source the tool never confirmed anything reads.
    half_read = [{"folder": "", "service": "Half", "type": "MapServer",
                  "status": UNRESOLVED, "database": "gisdb",
                  "server": "DBHOST1"}]
    check(summarize(half_read)["sources"] == []
          and len(summarize(half_read)["unresolved"]) == 1,
          "a row that names a database but did not resolve is counted as "
          "unresolved only  <-- pinned defect")

    dup = summarize(site_rows + site_rows)
    check(dup["services"] == 5,
          "the same service twice is still one service in the count")
    check(dup["copied"] == 1, "and one copied service, not two")

    lines = describe(summary)
    check(lines[0] == "services: 5", "the report opens with the service count")
    check(any(l.strip().startswith("addressdb") for l in lines),
          "the report lists each database with its host")
    check(any("portal item IDs: 1 present, 4 missing" in l for l in lines),
          "the report says how many item IDs exist to preserve")
    check(any("data copied to the server: 1 service" in l for l in lines),
          "the report names the copied services as a migration risk")
    check(any("unresolved data sources: 1" in l for l in lines),
          "the report counts the unresolved data sources")
    check(any("Cached.MapServer: no manifest reachable" in l for l in lines),
          "and names each one with the reason it could not be read")
    many = []
    for i in range(12):
        many.extend(rows_for_service(
            {"folder": "", "service": "S%d" % i, "type": "MapServer"}, {},
            {"databases": [{"byReference": True,
                            "onServerConnectionString":
                                "INSTANCE=sde:sqlserver:H%d;DATABASE=D%d"
                                % (i, i)}]}))
    lines = describe(summarize(many))
    check(sum(1 for l in lines if l.startswith("  D")) == 10,
          "the report samples ten data sources")
    check(any(l == "  ...and 2 more" for l in lines),
          "and counts the ones it did not print")
    unresolved_many = []
    for i in range(12):
        unresolved_many.extend(rows_for_service(
            {"folder": "", "service": "U%d" % i, "type": "MapServer"}, {}, None))
    lines = describe(summarize(unresolved_many))
    check(lines.count("  ...and 2 more") == 1,
          "the unresolved list is sampled the same way")
    check(not any("data copied" in l for l in describe(summarize(many))),
          "a site with nothing copied says nothing about copied data")
    check(not any("unresolved" in l for l in describe(summarize(many))),
          "a site with nothing unresolved says nothing about unresolved data")

    # ---- redaction, which is what keeps a token out of an error message
    # The replacement is spelled out rather than written as REDACTED. An
    # assertion phrased against the constant passes unchanged if the constant
    # is emptied, which turns redaction into a no-op that still reports green.
    check(redact("opening https://x?token=SECRET failed", "SECRET")
          == "opening https://x?token=*** failed",
          "a token is removed from an error before it is printed"
          "  <-- pinned defect")
    check(redact("a", None, "") == "a",
          "redaction with nothing to redact changes nothing")
    check(redact(ValueError("password=hunter2"), "hunter2")
          == "password=***",
          "an exception object is redacted, not just a string")
    check(redact("token 12345", 12345) == "token ***",
          "a secret that is not a string is redacted too  <-- pinned defect")
    check("SECRET" not in redact("token=SECRET&f=json", "SECRET")
          and REDACTED != "",
          "the secret itself is gone, and the marker left behind is not empty"
          "  <-- pinned defect")

    # ---- the site, answered in process. No socket is opened and no credential
    # is real. _opener is swapped for a stand-in that answers the paths an
    # ArcGIS Server site answers, including the failures that matter: a 200
    # carrying an error envelope, which is how ArcGIS reports a dead token, and
    # an exception out of urllib that quotes the failing url back.

    class FakeResponse(object):
        def __init__(self, body, text=None):
            self.body = body
            self.text = text

        def read(self):
            if self.text is not None:
                return self.text.encode("utf-8")
            return json.dumps(self.body).encode("utf-8")

    class Site(object):
        """An ArcGIS Server site and a portal, answering in process."""

        def __init__(self, folders=None, services=None, manifests=None,
                     token="TESTTOKEN", secret="hunter2", items=None,
                     open_error=None, not_json=False, fail_after=None):
            self.folders = folders or {}
            self.services = services or {}
            self.manifests = manifests or {}
            self.token = token
            self.secret = secret
            self.items = items or {}
            self.open_error = open_error
            self.not_json = not_json
            self.fail_after = fail_after
            self.targets = []
            self.posted = []
            self.insecure = None
            self.timeouts = []

        def __call__(self, insecure):        # stands in for _opener(insecure)
            self.insecure = insecure
            return self

        def open(self, target, data=None, timeout=None):
            self.timeouts.append(timeout)
            if data is None:
                path, _sep, query = target.partition("?")
            else:
                path, query = target, data.decode("utf-8")
                self.posted.append(query)
            self.targets.append(target)
            if self.open_error is not None:
                raise self.open_error(target)
            if self.not_json:
                return FakeResponse(None, text="[1, 2, 3]")
            params = dict(urllib.parse.parse_qsl(query))
            return FakeResponse(self._body(path, params))

        def _body(self, path, params):
            if path.endswith("/generateToken"):
                if params.get("password") != self.secret:
                    return {"error": {"code": 400,
                                      "message": "Invalid username or "
                                                 "password."}}
                return {"token": self.token, "expires": 9999999999999}
            if path.endswith("/search"):
                return {"results": [{"id": self.items[q]}
                                    for q in [params.get("q", "")]
                                    if q in self.items]}
            if params.get("token") != self.token:
                return {"error": {"code": 499, "message": "Token Required"}}
            if self.fail_after is not None:
                if self.fail_after <= 0:
                    return {"error": {"code": 498, "message": "Invalid token."}}
                self.fail_after -= 1
            marker = "/admin/services"
            if marker not in path:
                return {"error": {"code": 404, "message": "unhandled " + path}}
            tail = path.split(marker, 1)[1].strip("/")
            if tail.endswith("/iteminfo/manifest/manifest.json"):
                name = tail[:-len("/iteminfo/manifest/manifest.json")]
                if name in self.manifests:
                    return self.manifests[name]
                return {"error": {"code": 404, "message": "no manifest"}}
            if tail in self.services:
                return self.services[tail]
            if tail in self.folders:
                return self.folders[tail]
            return {"error": {"code": 404, "message": "no resource " + tail}}

    def serving(site, fn):
        """Run fn with that site answering, then put _opener back."""
        saved = globals()["_opener"]
        globals()["_opener"] = site
        try:
            return fn()
        finally:
            globals()["_opener"] = saved

    class Console(io.StringIO):
        """A stdout that reports an encoding, the way a real console does."""
        encoding = "utf-8"

    def captured(fn):
        """Run fn with stdout and stderr collected. Returns (result, text)."""
        console = Console()
        saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = console, console
        try:
            outcome = fn()
        finally:
            sys.stdout, sys.stderr = saved
        return outcome, console.getvalue()

    ROOT = "https://gis.example.com/arcgis"
    SITE_FOLDERS = {
        "": {"folders": ["Transport", "System"],
             "services": [{"serviceName": "Parcels", "type": "FeatureServer"},
                          {"serviceName": "Geometry",
                           "type": "GeometryServer"}]},
        "Transport": {"folders": [],
                      "services": [{"serviceName": "Transport/Roads",
                                    "type": "MapServer"}]},
        "System": {"folders": [],
                   "services": [{"serviceName": "System/PublishingTools",
                                 "type": "GPServer"}]},
    }
    SITE_SERVICES = {
        "Parcels.FeatureServer": {
            "capabilities": "Query,Create,Update",
            "portalProperties": {"portalItems": [
                {"itemID": "aaa11111111111111111111111111111",
                 "type": "FeatureServer"}]}},
        "Transport/Roads.MapServer": {
            "capabilities": "Map,Query",
            "extensions": [{"typeName": "FeatureServer", "enabled": "true"},
                           {"typeName": "WFSServer", "enabled": "false"}],
            "portalProperties": {"portalItems": [
                {"itemID": "bbb22222222222222222222222222222",
                 "type": "MapServer"},
                {"itemID": "ccc33333333333333333333333333333",
                 "type": "FeatureServer"}]}},
        "Geometry.GeometryServer": {"capabilities": ""},
    }
    SITE_MANIFESTS = {
        "Parcels.FeatureServer": {
            "databases": [{
                "byReference": True,
                "onServerConnectionString":
                    "SERVER=DBHOST1;INSTANCE=sde:sqlserver:DBHOST1;"
                    "DATABASE=gisdb;USER=sdeowner;VERSION=sde.DEFAULT",
                "datasets": [{"onServerName": "gisdb.sdeowner.Parcels"}]}],
            "resources": [{"onPremisePath": "C:\\maps\\parcels.aprx"}]},
        "Transport/Roads.MapServer": {
            "databases": [
                {"byReference": True,
                 "onServerConnectionString":
                     "SERVER=DBHOST1;INSTANCE=sde:sqlserver:DBHOST1;"
                     "DATABASE=gisdb;USER=sdeowner;VERSION=sde.DEFAULT",
                 "datasets": [{"onServerName": "gisdb.sdeowner.Roads"},
                              {"onServerName": "gisdb.sdeowner.Bridges"}]},
                {"byReference": False,
                 "onServerConnectionString":
                     "SERVER=MAPSRV1;INSTANCE=sde:sqlserver:MAPSRV1;"
                     "DATABASE=managed;USER=sde",
                 "datasets": [{"onServerName": "managed.dbo.Labels"}]}],
            "resources": [{"onPremisePath": "C:\\maps\\roads.mxd"}]},
    }

    def site(**kw):
        opts = {"folders": SITE_FOLDERS, "services": SITE_SERVICES,
                "manifests": SITE_MANIFESTS}
        opts.update(kw)
        return Site(**opts)

    # ---- the opener itself, which is what the stand-in below replaces
    def handler_context(opener):
        for handler in opener.handlers:
            context = getattr(handler, "_context", None)
            if context is not None:
                return context

    check(handler_context(_opener(False)).verify_mode == ssl.CERT_REQUIRED
          and handler_context(_opener(False)).check_hostname is True,
          "the default opener verifies the certificate against the system "
          "trust store  <-- pinned defect")
    check(handler_context(_opener(True)).verify_mode == ssl.CERT_NONE,
          "--insecure builds an opener that verifies nothing")
    check(handler_context(_opener(True)).check_hostname is False,
          "and does not check the host name either, which is the other half "
          "of an internal self-signed certificate")

    # ---- the token exchange
    deployment = site()
    token = serving(deployment,
                    lambda: generate_token(ROOT, "gis_admin", "hunter2"))
    check(token == "TESTTOKEN", "a username and password produce a token")
    check("/admin/generateToken" in deployment.targets[0],
          "the token comes from the site's own admin endpoint")
    check("password=hunter2" in deployment.posted[0],
          "the password is posted, never put in the url  <-- pinned defect")
    check(deployment.insecure is False,
          "the token exchange itself runs over a verified connection"
          "  <-- pinned defect")
    serving(site(), lambda: generate_token(ROOT, "gis_admin", "hunter2",
                                           insecure=True))
    bad = site()
    raises(lambda: serving(bad, lambda: generate_token(ROOT, "gis_admin",
                                                       "wrong")),
           "a wrong password raises rather than returning a blank token")
    try:
        serving(bad, lambda: generate_token(ROOT, "gis_admin", "wrong"))
    except RuntimeError as exc:
        check("wrong" not in "%s" % exc,
              "and the password is not in the message  <-- pinned defect")
        check("Invalid username" in "%s" % exc,
              "while the site's own reason survives")
    raises(lambda: serving(site(token=None),
                           lambda: generate_token(ROOT, "u", "hunter2")),
           "a token endpoint that answers without a token raises")
    portal_site = site()
    serving(portal_site,
            lambda: generate_token("https://portal.example.com/portal/"
                                   "sharing/rest", "gis_admin", "hunter2",
                                   portal=True))
    check(portal_site.targets[0].endswith("/sharing/rest/generateToken"),
          "a portal token comes from the portal's own endpoint, with no "
          "/admin in front of it  <-- pinned defect")

    # ---- the call layer's failures
    raises(lambda: serving(site(), lambda: _call("%s/admin/services" % ROOT,
                                                 {"token": "STALE"},
                                                 secret="STALE")),
           "an expired token raises instead of reading as an empty catalog"
           "  <-- pinned defect")
    try:
        serving(site(), lambda: _call("%s/admin/services" % ROOT,
                                      {"token": "STALE"}, secret="STALE"))
    except RuntimeError as exc:
        check("STALE" not in "%s" % exc,
              "and the dead token is in no part of the message")
        check("499" in "%s" % exc and "/admin/services" in "%s" % exc,
              "while the site's error code and the endpoint survive")
    raises(lambda: serving(site(), lambda: _call("%s/rest/services" % ROOT,
                                                {"token": "TESTTOKEN"})),
           "the stand-in site refuses a path it does not implement, so a test "
           "that drifts fails rather than passing against a stub")
    raises(lambda: serving(site(not_json=True),
                           lambda: _call("%s/admin/services" % ROOT, {})),
           "a JSON list where an object belongs raises, because every caller "
           "reads it as an object  <-- pinned defect")

    def boom(target):
        return urllib.error.URLError("no route to %s" % target)
    raises(lambda: serving(site(open_error=boom),
                           lambda: _call("%s/admin/services" % ROOT,
                                         {"token": "TESTTOKEN"},
                                         secret="TESTTOKEN")),
           "a transport failure raises")
    try:
        serving(site(open_error=boom),
                lambda: _call("%s/admin/services" % ROOT,
                              {"token": "TESTTOKEN"}, secret="TESTTOKEN"))
    except RuntimeError as exc:
        check("TESTTOKEN" not in "%s" % exc,
              "and the url urllib quoted back carries no token"
              "  <-- pinned defect")
    timed = site()
    serving(timed, lambda: _call("%s/admin/services" % ROOT,
                                 {"token": "TESTTOKEN"}, timeout=7))
    check(timed.timeouts == [7], "the timeout reaches the opener")

    # ---- the catalog walk
    walked = site()
    records = serving(walked, lambda: walk_catalog(
        json_getter("TESTTOKEN"), ROOT))
    check(len(records) == 3,
          "the walk finds the root services and the ones in folders")
    check({r["service"] for r in records} == {"Parcels", "Geometry", "Roads"},
          "System is not walked, so PublishingTools is not in the inventory")
    check({"folder": "Transport", "service": "Roads", "type": "MapServer"}
          in records, "a service in a folder is recorded with its folder")
    check(all("token=TESTTOKEN" in t for t in walked.targets),
          "every catalog call carries the token")
    raises(lambda: serving(site(fail_after=0),
                           lambda: walk_catalog(json_getter("TESTTOKEN"),
                                                ROOT)),
           "a catalog that will not answer raises rather than reporting an "
           "empty site  <-- pinned defect")
    raises(lambda: serving(site(fail_after=1),
                           lambda: walk_catalog(json_getter("TESTTOKEN"),
                                                ROOT)),
           "a token that dies part way through the walk raises too")
    loop = site(folders={"": {"folders": ["A"], "services": []},
                         "A": {"folders": ["A", "B"], "services": []},
                         "B": {"folders": ["A"], "services": []}})
    check(serving(loop, lambda: walk_catalog(json_getter("TESTTOKEN"), ROOT))
          == [], "a folder that lists itself is visited once, not for ever")
    deep = dict((str(i), {"folders": [str(i + 1)], "services": []})
                for i in range(40))
    deep[""] = {"folders": ["0"], "services": []}
    raises(lambda: serving(site(folders=deep),
                           lambda: walk_catalog(json_getter("TESTTOKEN"),
                                                ROOT, max_folders=5)),
           "a catalog deeper than max_folders refuses instead of walking on")

    # ---- the inventory, service by service
    collected = site()
    rows = serving(collected, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    summary = summarize(rows)
    check(summary["services"] == 3, "every service on the site is inventoried")
    check(len(rows) == 5,
          "and the three services produce five rows, one per dataset")
    roads_rows = [r for r in rows if r["service"] == "Roads"]
    check(len(roads_rows) == 3,
          "the map service reading two databases and three datasets has three "
          "rows")
    check([r["dataset"] for r in roads_rows]
          == ["gisdb.sdeowner.Roads", "gisdb.sdeowner.Bridges",
              "managed.dbo.Labels"],
          "the datasets are reported in manifest order")
    check(roads_rows[2]["status"] == COPIED,
          "the copied database of that service is flagged")
    check(roads_rows[0]["source_item_id"]
          == "bbb22222222222222222222222222222",
          "the map service reports the item ID of its map service item")
    check("FeatureServer=ccc33333333333333333333333333333"
          in roads_rows[0]["all_item_ids"],
          "and its feature service item is in the column beside it"
          "  <-- pinned defect")
    check(roads_rows[0]["extensions"] == "FeatureServer",
          "the enabled extension is reported for the parity diff")
    geom_rows = [r for r in rows if r["type"] == "GeometryServer"]
    check(geom_rows[0]["status"] == NODATA,
          "the GeometryServer, which has no manifest, does not fail the run")
    check(exit_code(summary) == 0, "this site resolves every data source")
    check(sum(1 for t in collected.targets if "manifest.json" in t) == 3,
          "one manifest call per service, and no more")

    missing = site(manifests={})
    rows = serving(missing, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    check(exit_code(summarize(rows)) == 1,
          "a site whose manifests will not answer fails the run")
    check(sum(1 for r in rows if r["status"] == UNRESOLVED) == 2,
          "and both data-bearing services are reported unresolved")
    check(sum(1 for r in rows if r["status"] == NODATA) == 1,
          "while the GeometryServer is still expected to have none")

    broken = site(services={})
    rows = serving(broken, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    check(len(rows) == 5,
          "a service whose own JSON will not answer is still inventoried"
          "  <-- pinned defect")
    check(all(r["source_item_id"] == "" for r in rows),
          "it just has no item ID and no capabilities to report")
    check([r for r in rows if r["service"] == "Roads"][0]["database"]
          == "gisdb",
          "and its data source is still read from the manifest")

    echoed = []
    serving(site(), lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        [{"folder": "", "service": "Parcels", "type": "FeatureServer"}],
        echo=echoed.append))
    check(echoed == ["  Parcels.FeatureServer"],
          "the walk names each service as it reads it")
    check(serving(site(), lambda: inventory(json_getter("TESTTOKEN"), ROOT,
                                            [])) == [],
          "an empty catalog inventories nothing rather than raising")

    # ---- the portal item-ID fallback
    REST_ROOT = "https://gis.example.com/server/rest/services"
    PORTAL = "https://portal.example.com/portal/sharing/rest"
    query = ('type:"Feature Service" AND url:"%s/Parcels/FeatureServer"'
             % REST_ROOT)
    found = site(items={query: "eee55555555555555555555555555555"})
    search = portal_searcher(PORTAL, "PORTALTOKEN", REST_ROOT)
    record = {"folder": "", "service": "Parcels", "type": "FeatureServer"}
    check(serving(found, lambda: search(record))
          == "eee55555555555555555555555555555",
          "the portal search finds the item backing a service")
    check(serving(found, lambda: search({"folder": "Transport",
                                         "service": "Roads",
                                         "type": "MapServer"})) == "",
          "a service with no matching item searches to a blank ID")
    check(serving(site(open_error=boom), lambda: search(record)) == "",
          "a portal that will not answer returns a blank ID rather than "
          "stopping the inventory  <-- pinned defect")
    check(serving(found, lambda: search({"folder": "", "service": "X",
                                         "type": "WeirdServer"})) == "",
          "a service type the portal has no item type for is not searched")
    check(portal_searcher("", "t", REST_ROOT) is None,
          "no --portal means no fallback at all")
    check(portal_searcher(PORTAL, "t", "") is None,
          "no --public-rest-root means no fallback either")
    check(portal_searcher(PORTAL, None, REST_ROOT) is None,
          "and no portal token means no fallback")
    searched = site(items={query: "eee55555555555555555555555555555"},
                    services={})
    rows = serving(searched, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        [record, {"folder": "Transport", "service": "Roads",
                  "type": "MapServer"}],
        search=portal_searcher(PORTAL, "PORTALTOKEN", REST_ROOT)))
    check(rows[0]["source_item_id"] == "eee55555555555555555555555555555",
          "the inventory fills a blank item ID from the portal")
    with_props = site(items={query: "eee55555555555555555555555555555"})
    rows = serving(with_props, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT, [record],
        search=portal_searcher(PORTAL, "PORTALTOKEN", REST_ROOT)))
    check(rows[0]["source_item_id"] == "aaa11111111111111111111111111111",
          "a service that knows its own item ID is not searched for")
    check(not any("/search" in t for t in with_props.targets),
          "and no search call is made at all  <-- pinned defect")

    # ---- real files on disk. Everything below writes into one temp directory
    # and deletes it again. No network, no site, no credentials.
    tmp = tempfile.mkdtemp(prefix="svcsource-selftest-")

    def read_back(path):
        with io.open(path, "r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle)), handle

    out_path = os.path.join(tmp, "services.csv")
    rows = serving(site(), lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    write_csv(out_path, rows)
    back, _ = read_back(out_path)
    check(len(back) == 5, "every row reaches the file")
    check(list(back[0].keys()) == list(COLUMNS),
          "the file carries every column, in order")
    check(back[0]["database"] == "gisdb",
          "a cell survives the round trip to disk")
    with io.open(out_path, "r", encoding="utf-8", newline="") as handle:
        raw = handle.read()
    check("\r\r\n" not in raw,
          "no doubled carriage return, which Excel reads as a blank row"
          "  <-- pinned defect")
    empty_path = os.path.join(tmp, "empty.csv")
    write_csv(empty_path, [])
    with io.open(empty_path, "r", encoding="utf-8") as handle:
        text = handle.read()
    # The header is written out in full rather than rebuilt from COLUMNS. It is
    # the file's contract: somebody's filter, pivot or diff is keyed on these
    # names in this order, and a comparison against COLUMNS would accept any
    # reordering of them silently.
    check(text.strip() ==
          "folder,service,type,path,status,source_item_id,all_item_ids,"
          "by_reference,dataset,server,instance,database,db_user,version,"
          "capabilities,extensions,source_document,note",
          "a site with no services writes a header and no rows"
          "  <-- pinned defect")
    check(text != "", "and never an empty file, which reads as a crashed run")
    nested = os.path.join(tmp, "a", "b", "services.csv")
    write_csv(nested, rows)
    check(os.path.isfile(nested), "a missing output directory is created")
    extra = dict(rows[0])
    extra["surprise"] = "x"
    write_csv(os.path.join(tmp, "extra.csv"), [extra])
    check(read_back(os.path.join(tmp, "extra.csv"))[0][0].get("surprise")
          is None, "a row carrying an unknown key does not break the writer")
    sparse = os.path.join(tmp, "sparse.csv")
    write_csv(sparse, [{"service": "Only"}])
    check(read_back(sparse)[0][0]["service"] == "Only",
          "a row missing most columns is written with empty cells")
    dumps_path = os.path.join(tmp, "raw", "manifests.json")
    write_manifests(dumps_path, {"Parcels.FeatureServer": {"databases": []}})
    check(os.path.isfile(dumps_path),
          "a missing directory is created for the manifest dump as well")
    with io.open(dumps_path, "r", encoding="utf-8") as handle:
        check(json.load(handle)["Parcels.FeatureServer"] == {"databases": []},
              "the raw manifests are written as JSON when they are asked for")

    # ---- the password, which never comes from argv
    # The variable names are spelled out. They are what the README tells an
    # operator to export, so renaming one is a published change, not a rename.
    check((SECRET_ENV, PORTAL_SECRET_ENV)
          == ("SVCSOURCE_PASSWORD", "SVCSOURCE_PORTAL_PASSWORD"),
          "the two password variables are named as the README documents them")
    os.environ["SVCSOURCE_PASSWORD"] = "hunter2"
    check(read_secret("gis_admin") == "hunter2",
          "the password comes from the environment")
    os.environ.pop(SECRET_ENV, None)
    os.environ[PORTAL_SECRET_ENV] = "portal-pw"
    check(read_secret("gis_admin", env=PORTAL_SECRET_ENV) == "portal-pw",
          "the portal password has its own variable")
    os.environ.pop(PORTAL_SECRET_ENV, None)
    saved_getpass = getpass.getpass
    try:
        getpass.getpass = lambda prompt="": "typed:%s" % prompt
        check(read_secret("gis_admin")
              == "typed:password for gis_admin (not echoed): ",
              "with no variable set the password is prompted for, unechoed")
        check(read_secret("padmin", env=PORTAL_SECRET_ENV,
                          prompt="portal password for padmin: ")
              == "typed:portal password for padmin: ",
              "and the portal prompt says which password it wants")
        os.environ[SECRET_ENV] = "hunter2"
        check(read_secret("gis_admin") == "hunter2",
              "the environment wins, so a scheduled run is never left at a "
              "prompt nobody can answer  <-- pinned defect")
    finally:
        getpass.getpass = saved_getpass
        os.environ.pop(SECRET_ENV, None)

    # ---- the command line
    check(_parse(["--server", ROOT, "--user", "gis_admin"]).apply is False,
          "--apply is off by default, so nothing is written  <-- pinned defect")
    check(_parse(["--server", ROOT, "--user", "gis_admin"]).insecure is False,
          "--insecure is off by default, so certificates are verified"
          "  <-- pinned defect")
    check(_parse(["--server", ROOT, "--user", "u"]).dump_manifest is None,
          "--dump-manifest is off by default, so no raw manifest is written"
          "  <-- pinned defect")
    check(_parse(["--server", ROOT, "--user", "u"]).server == ROOT,
          "--server is read")
    check(_parse(["--server", ROOT, "--user", "u"]).user == "u",
          "--user is read")
    check(_parse(["--server", ROOT, "--user", "u"]).out
          == "services_inventory.csv", "--out has a default name")
    check(_parse(["--server", ROOT, "--user", "u", "--out", "x.csv"]).out
          == "x.csv", "--out is read")
    check(_parse(["--server", ROOT, "--user", "u", "--apply"]).apply is True,
          "--apply is read")
    check(_parse(["--server", ROOT, "--user", "u", "--insecure"]).insecure
          is True, "--insecure is read")
    check(_parse(["--server", ROOT, "--user", "u", "--portal", PORTAL]).portal
          == PORTAL, "--portal is read")
    check(_parse(["--server", ROOT, "--user", "u", "--public-rest-root",
                  REST_ROOT]).public_rest_root == REST_ROOT,
          "--public-rest-root is read")
    check(_parse(["--server", ROOT, "--user", "u", "--portal-user", "p"]
                 ).portal_user == "p", "--portal-user is read")
    check(_parse(["--server", ROOT, "--user", "u", "--dump-manifest", "m.json"]
                 ).dump_manifest == "m.json", "--dump-manifest is read")
    check(_parse(["--server", ROOT, "--user", "u", "--timeout", "5"]).timeout
          == 5, "--timeout is read")
    # 30, not HTTP_TIMEOUT. Both sides of a comparison written against the
    # constant move together, so the assertion would pass at any default.
    check(_parse(["--server", ROOT, "--user", "u"]).timeout == 30,
          "--timeout defaults to the 30 seconds the README documents")
    check(_parse(["--self-test"]).self_test is True,
          "--self-test needs no server at all")
    with contextlib.redirect_stderr(io.StringIO()):
        raises(lambda: _parse(["--nonexistent-flag"]),
               "an unknown flag stops the run rather than being ignored",
               kind=SystemExit)
    check(_parse([ROOT, "--user", "u"]).server == ROOT,
          "the server may be given positionally, for a script tool")
    check(_parse([ROOT, "--server", "https://other/arcgis", "--user", "u"]
                 ).server == "https://other/arcgis",
          "the named server wins when both forms are given")

    def run(argv):
        """main() with its output captured, so the self-test stays readable."""
        return captured(lambda: main(argv))

    os.environ[SECRET_ENV] = "hunter2"
    code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                              "gis_admin"]))
    check(code == 0, "a site that resolves every data source exits 0")
    check("services: 3" in text, "the run prints the service count")
    check("gisdb" in text and "DBHOST1" in text,
          "and names the database and the host behind the services")
    check("portal item IDs: 2 present, 1 missing" in text,
          "and how many item IDs there are to preserve")
    check("data copied to the server: 1 service" in text,
          "and warns about the service whose data was copied")
    check("was not written" in text,
          "--out without --apply writes nothing and says so"
          "  <-- pinned defect")
    check(not os.path.exists("services_inventory.csv"),
          "and no file appears in the working directory")

    applied = os.path.join(tmp, "applied.csv")
    code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                              "gis_admin", "--out", applied,
                                              "--apply"]))
    check(code == 0 and os.path.isfile(applied), "--apply writes the CSV")
    check("wrote %s" % applied in text, "and the run names the file it wrote")
    written, _ = read_back(applied)
    check(len(written) == 5, "the file holds every row the summary counted")
    check("hunter2" not in io.open(applied, encoding="utf-8").read()
          and "TESTTOKEN" not in io.open(applied, encoding="utf-8").read(),
          "no credential reaches the file on disk  <-- pinned defect")

    dumped = os.path.join(tmp, "raw-manifests.json")
    code, text = serving(site(), lambda: run(
        ["--server", ROOT, "--user", "gis_admin", "--out",
         os.path.join(tmp, "d.csv"), "--dump-manifest", dumped, "--apply"]))
    check(code == 0 and os.path.isfile(dumped),
          "--dump-manifest writes the raw manifests")
    with io.open(dumped, encoding="utf-8") as handle:
        raw_manifests = json.load(handle)
    check(sorted(raw_manifests) == ["Parcels.FeatureServer",
                                    "Transport/Roads.MapServer"],
          "one entry per service that had a manifest")
    check(serving(site(), lambda: run(
        ["--server", ROOT, "--user", "gis_admin", "--dump-manifest",
         os.path.join(tmp, "never.json")]))[0] == 0,
          "--dump-manifest without --apply runs")
    check(not os.path.exists(os.path.join(tmp, "never.json")),
          "and writes nothing  <-- pinned defect")

    code, text = serving(site(manifests={}),
                         lambda: run(["--server", ROOT, "--user", "gis_admin"]))
    check(code == 1, "a site with an unreadable data source exits 1")
    check("unresolved data sources: 2" in text, "and says how many")
    check("Parcels.FeatureServer: no manifest reachable" in text,
          "and names each service that could not be resolved")

    code, text = serving(site(fail_after=0),
                         lambda: run(["--server", ROOT, "--user", "gis_admin"]))
    check(code == 2,
          "a catalog that cannot be read exits 2, not the gate's 1"
          "  <-- pinned defect")
    check("portal item IDs" not in text and "rows:" not in text,
          "and no inventory at all is printed  <-- pinned defect")
    os.environ[SECRET_ENV] = "wrong"
    code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                              "gis_admin"]))
    check(code == 2, "a password the site refuses exits 2")
    check("wrong" not in text, "and the password is not printed back")
    os.environ[SECRET_ENV] = "hunter2"

    check(serving(site(), lambda: run(["--user", "u"]))[0] == 64,
          "a run with no --server is a usage error")
    check(serving(site(), lambda: run(["--server", ROOT]))[0] == 64,
          "a run with no --user is a usage error  <-- pinned defect")
    code, text = serving(site(), lambda: run(["--server",
                                              "gis.example.com/arcgis",
                                              "--user", "u"]))
    check(code == 64 and "https://" in text,
          "a server url with no scheme is a usage error, not a traceback")
    check(serving(site(), lambda: run(["--server", ROOT, "--user", "u",
                                       "--portal", PORTAL]))[0] == 64,
          "--portal without --public-rest-root is a usage error")
    check(serving(site(), lambda: run(["--server", ROOT, "--user", "u",
                                       "--public-rest-root", REST_ROOT]))[0]
          == 64, "--public-rest-root without --portal is a usage error")
    check(serving(site(), lambda: run(["--server", ROOT, "--user", "u",
                                       "--timeout", "0"]))[0] == 64,
          "a timeout of zero is a usage error")
    check(serving(site(), lambda: run(
        ["--server", ROOT, "--user", "u", "--out",
         os.path.join(tmp, "x.csv"), "--apply", "--dump-manifest",
         os.path.join(tmp, "x.csv")]))[0] == 64,
        "writing the CSV and the manifests to one path is a usage error")
    check(serving(site(), lambda: run(["--server", ROOT, "--user", "gis_admin",
                                       "--out", tmp, "--apply"]))[0] == 2,
          "a write that fails exits 2, not the gate's 1  <-- pinned defect")

    os.environ[PORTAL_SECRET_ENV] = "hunter2"
    fallback = site(items={query: "eee55555555555555555555555555555"},
                    services={})
    code, text = serving(fallback, lambda: run(
        ["--server", ROOT, "--user", "gis_admin", "--portal", PORTAL,
         "--public-rest-root", REST_ROOT, "--out",
         os.path.join(tmp, "portal.csv"), "--apply"]))
    check(code == 0,
          "a site whose service JSON will not answer still resolves every "
          "data source, because the manifest is a separate resource")
    check("portal item IDs: 1 present" in text,
          "and the item ID the portal search found is counted")
    found_rows, _ = read_back(os.path.join(tmp, "portal.csv"))
    check(any(r["source_item_id"] == "eee55555555555555555555555555555"
              for r in found_rows),
          "the searched item ID reaches the file")
    check(any("portal search" in r["note"] for r in found_rows),
          "with a note saying where it came from")
    other_user = site(items={query: "eee55555555555555555555555555555"})
    serving(other_user, lambda: run(
        ["--server", ROOT, "--user", "gis_admin", "--portal", PORTAL,
         "--public-rest-root", REST_ROOT, "--portal-user", "padmin"]))
    check(any("username=padmin" in posted for posted in other_user.posted),
          "--portal-user signs in to the portal as somebody else")
    check(any("username=gis_admin" in posted for posted in other_user.posted),
          "while the site is still read as the server administrator")
    os.environ.pop(PORTAL_SECRET_ENV, None)

    insecure_site = site()
    serving(insecure_site, lambda: run(["--server", ROOT, "--user", "gis_admin",
                                        "--insecure"]))
    check(insecure_site.insecure is True,
          "--insecure reaches the opener that builds the connection")
    verified = site()
    serving(verified, lambda: run(["--server", ROOT, "--user", "gis_admin"]))
    check(verified.insecure is False,
          "and without it every connection is verified  <-- pinned defect")

    trimmed = site()
    serving(trimmed, lambda: run(["--server", "%s/rest/services" % ROOT,
                                  "--user", "gis_admin"]))
    check(any(t.startswith("%s/admin/services?" % ROOT)
              for t in trimmed.targets),
          "a REST catalog url is trimmed before the admin call is built")

    argv_before = sys.argv
    try:
        sys.argv = ["svcsource.py", "--server", ROOT, "--user", "gis_admin"]
        check(serving(site(), lambda: run(None))[0] == 0,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before
    os.environ.pop(SECRET_ENV, None)

    shutil.rmtree(tmp, ignore_errors=True)

    # ---- the harness itself, which is the control every assertion above
    # depends on. Deliberate failures are fed to the real check() and raises(),
    # with the output swallowed, and then removed from the tally again.
    before = len(failed)
    with contextlib.redirect_stdout(io.StringIO()):
        check(False, "a false check must be recorded")
        raises(lambda: None, "a call that raises nothing must be recorded")
        raises(lambda: [][1], "a call raising IndexError must be recorded")
        raises(lambda: {}["k"], "a call raising KeyError must be recorded")
    recorded = len(failed) - before
    del failed[before:]
    check(recorded == 4,
          "the harness records a false check, a missing exception and two "
          "wrong exceptions as four failures, so a broken tool turns this "
          "self-test red  <-- pinned defect")

    print("-" * 70)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="svcsource.py",
        description="Report the data source and portal item ID behind every "
                    "service on an ArcGIS Server site.",
        epilog="The password comes from %s or an unechoed prompt, never from "
               "argv. Nothing is written without --apply." % SECRET_ENV)
    ap.add_argument("server_positional", nargs="?", metavar="SERVER",
                    help="the site url, as a positional argument so an ArcGIS "
                         "script tool can pass it")
    ap.add_argument("--server", help="the same site url, named. This wins over "
                                     "the positional form when both are given")
    ap.add_argument("--user", help="ArcGIS Server administrator to sign in as")
    ap.add_argument("--out", default="services_inventory.csv",
                    help="path for the inventory CSV (default "
                         "services_inventory.csv)")
    ap.add_argument("--portal", default="",
                    help="portal sharing/rest root, which enables the item-ID "
                         "search for services whose portalProperties are empty")
    ap.add_argument("--portal-user", dest="portal_user", default="",
                    help="portal user for that search (defaults to --user)")
    ap.add_argument("--public-rest-root", dest="public_rest_root", default="",
                    help="the site's public REST services root, required by "
                         "--portal because that is the url portal items record")
    ap.add_argument("--dump-manifest", dest="dump_manifest",
                    help="also write every raw manifest to this JSON file, for "
                         "a data source this tool did not understand")
    ap.add_argument("--timeout", type=int, default=HTTP_TIMEOUT,
                    help="seconds to wait for one Admin API call (default %d)"
                         % HTTP_TIMEOUT)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification, for a site behind an internal "
                         "CA. Off by default.")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    args = ap.parse_args(argv)
    # A script tool passes parameters positionally; a shell user usually names
    # them. Accept both and let the named one win.
    if not args.server and getattr(args, "server_positional", None):
        args.server = args.server_positional
    return args


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.server:
        print("error: --server is required. Use --self-test to verify the "
              "tool without a site.", file=sys.stderr)
        return 64
    if not is_http_url(args.server):
        print("error: --server must start with https:// or http://, got %r."
              % (args.server,), file=sys.stderr)
        return 64
    if not args.user:
        # No default administrator name. The harvested version defaulted to the
        # one this site happened to use, which is a fact about that site and
        # not about ArcGIS Server.
        print("error: --user is required. It is the ArcGIS Server "
              "administrator the inventory signs in as.", file=sys.stderr)
        return 64
    if bool(args.portal) != bool(args.public_rest_root):
        print("error: --portal and --public-rest-root go together. The search "
              "matches on the service url the portal recorded.",
              file=sys.stderr)
        return 64
    if args.timeout < 1:
        print("error: --timeout must be at least 1 second.", file=sys.stderr)
        return 64
    if args.dump_manifest and args.out and os.path.abspath(args.dump_manifest) \
            == os.path.abspath(args.out):
        print("error: --dump-manifest and --out cannot be the same file.",
              file=sys.stderr)
        return 64

    root = admin_root(args.server)
    dumps = {} if args.dump_manifest else None

    try:
        secret = read_secret(args.user)
        token = generate_token(root, args.user, secret, args.insecure)
        get_json = json_getter(token, args.insecure, args.timeout)
        search = None
        if args.portal:
            portal_user = args.portal_user or args.user
            portal_secret = read_secret(
                portal_user, env=PORTAL_SECRET_ENV,
                prompt="portal password for %s (not echoed): " % portal_user)
            portal_token = generate_token(args.portal, portal_user,
                                          portal_secret, args.insecure,
                                          portal=True)
            search = portal_searcher(args.portal, portal_token,
                                     args.public_rest_root, args.insecure,
                                     args.timeout)
        records = walk_catalog(get_json, root)
        rows = inventory(get_json, root, records, search=search,
                         dump_manifest=(None if dumps is None
                                        else dumps.__setitem__))
    except RuntimeError as exc:
        # Exit 2 rather than 1. A site that could not be read is a different
        # fact from a site whose data sources could not all be resolved, and
        # only the exit code tells a scheduled job which one happened.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    summary = summarize(rows)
    for line in describe(summary):
        print(line)

    if args.apply:
        try:
            print("\nwrote %s" % write_csv(args.out, rows))
            if args.dump_manifest:
                print("wrote %s" % write_manifests(args.dump_manifest, dumps))
        except (IOError, OSError, ValueError) as exc:
            print("error: could not write: %s" % exc, file=sys.stderr)
            return 2
    else:
        print("\nRead only. %s was not written. Re-run with --apply."
              % args.out)

    code = exit_code(summary)
    print("\n%s" % ("PASS: every data source was read." if code == 0 else
                    "FAIL: at least one data source could not be read. The "
                    "inventory is incomplete."))
    return code


if __name__ == "__main__":
    sys.exit(main())
