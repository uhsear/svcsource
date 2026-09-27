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

A geocode service has a third trap. Its locator is either read in place from a
registered folder or copied to the server at publish time, and a nightly
locator rebuild reaches only the first kind. The copy goes on answering with
the old addresses and nothing reports an error. The tool reads the locator
folder from the service's own properties. A folder in the server's arcgisinput
directory is a copy, and a manifest entry for that folder carries its own
byReference flag. Anything else is UNKNOWN, with the registered folder it is
in, if any, named in the note.

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
not be read, 2 the site could not be read, 3 every source was read and at least
one geocode service serves a copied locator, 64 usage error.
"""

from __future__ import print_function

import argparse
import csv
import getpass
import io
import json
import os
import posixpath
import re
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

# Service types whose data source is a locator rather than a dataset. When the
# manifest lists no database for one of these, the locator path is read from
# the service's own properties and checked against the registered folders.
LOCATOR_TYPES = ("GeocodeServer",)

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
# PASSWORD=, ENCRYPTED_PASSWORD= and ENCRYPTED_PASSWORD_UTF8= in a connection
# string, each up to the next ;KEY= or the end. Stopping at the first
# semicolon left the tail of a password that holds one.
PASSWORD_FIELD = re.compile(r"(\b\w*PASSWORD\w*\s*=).*?(?=;\s*\w+\s*=|$)",
                            re.I | re.S)

# Row status. The gate reads this column and nothing else.
OK = "ok"
COPIED = "copied"              # data copied to the server, not registered
NODATA = "no-datasource"       # this service type reads nothing
UNRESOLVED = "unresolved"      # the data source could not be read

# The Admin API search that lists the folders registered with the site. Only
# asked for once per run, and only when a locator-backed service needs it.
FIND_ITEMS = "/admin/data/findItems"
FOLDER_QUERY = {"parentPath": "/fileShares", "types": "folder"}

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
    services = listing.get("services")
    # Esri documents services in the root listing and in every folder
    # listing, and an empty folder answers an empty array. So a listing with
    # no services array is not a catalog: {} from a proxy or an SSO layer
    # used to read as a clean, empty site that exited 0.
    if services is None:
        raise RuntimeError(unreadable_catalog(
            folder, "has no services array, so it is not an ArcGIS catalog"))
    # An entry this cannot read raises rather than being skipped. A skipped
    # entry is a service missing from the inventory, and a service missing
    # from the inventory reads as a service that is not on the site.
    if not isinstance(services, list):
        raise RuntimeError(unreadable_catalog(
            folder, "answered services as a JSON %s, not an array"
            % type(services).__name__))
    out = []
    for svc in services:
        name = typ = None
        if isinstance(svc, dict):
            name = svc.get("serviceName") or svc.get("name")
            typ = svc.get("type")
        if not (isinstance(name, str) and name.strip("/")
                and isinstance(typ, str) and typ):
            raise RuntimeError(unreadable_catalog(
                folder, "lists a service with no serviceName or no type as "
                "text"))
        own, _sep, bare = name.strip("/").rpartition("/")
        out.append({"folder": (folder or own).strip("/"),
                    "service": bare, "type": typ})
    return out


def unreadable_catalog(folder, what):
    """The message for a catalog listing that cannot be read whole."""
    return ("the catalog listing of %s %s. Stopping, because an inventory "
            "that skipped it would report what it missed as absent."
            % ("folder %s" % folder if folder else "the root folder", what))


def catalog_folders(listing, skip=SKIP_FOLDERS, folder=""):
    """The folders of a catalog listing, minus the ones the walk skips."""
    folders = listing.get("folders")
    if folders is None:
        return []
    if not isinstance(folders, list) or not all(isinstance(f, str)
                                                for f in folders):
        # A string here used to be walked one character at a time.
        raise RuntimeError(unreadable_catalog(
            folder, "answered folders as something other than an array of "
            "names"))
    lowered = tuple(s.lower() for s in skip)
    return [f for f in folders
            if f.strip("/") and f.strip("/").lower() not in lowered]


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
    # sde:sqlserver:DBHOST1 -> DBHOST1. The host is everything after the
    # sde:<dbms>: prefix, and it keeps whatever follows it: a named instance
    # (HOST\SQL2019), a port (HOST,1433) or an Oracle Easy Connect string
    # (HOST:1521/orcl) are all part of the machine's address, and dropping
    # them merges two different servers. Splitting at the last colon read an
    # Easy Connect string as its port and service name. An INSTANCE with no
    # sde: prefix is a 3-tier port such as 5151, so the machine is SERVER.
    fields = instance.split(":", 2)
    if len(fields) == 3 and fields[0].strip().lower() == "sde":
        host = fields[2]
    else:
        host = parsed.get("SERVER", "") or instance
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
        ident = entry.get("itemID") or entry.get("itemId")
        kind = entry.get("type")
        # Only text is an item ID or a type. primary_item_id compares the
        # type in lower case, and a type spelled as a number ended the run.
        if isinstance(ident, str) and ident:
            out.append((kind if isinstance(kind, str) else "", ident))
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
        path = res.get("onPremisePath") or res.get("clientName")
        if isinstance(path, str) and path:
            return path
    return ""


def by_reference_text(database):
    """The byReference flag of a manifest database, as a cell.

    False means the data was copied to the server at publish time, so the
    service reads the server's own managed geodatabase. Moving the enterprise
    database it was copied FROM does nothing to it, which is the migration
    surprise this column exists for.

    Esri documents the flag as a boolean. The text spellings are read too,
    as every other ArcGIS boolean in this file is, because truthiness read the
    string "false" as true. Anything else, null included, is '' and the row
    says UNKNOWN rather than guessing.
    """
    value = database.get("byReference") if isinstance(database, dict) else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower()
    return ""


def manifest_databases(manifest):
    """The database entries of a manifest, or [] for a manifest that is not
    an object or lists none."""
    if not isinstance(manifest, dict):
        return []
    return dict_entries(manifest.get("databases"))


def locator_source(service_json):
    """The locator a geocode service reads, as (folder, connection, name).

    These are the three GeocodeServer properties the Admin API documents:
    locatorWorkspacePath for a locator file in a folder,
    locatorWorkspaceConnectionString for a locator in a geodatabase, and
    locator for its name. A manifest need not list the locator under
    databases, so without this the tool had nothing to say about a geocode
    service except that it was unresolved.
    """
    props = (service_json.get("properties")
             if isinstance(service_json, dict) else None)
    if not isinstance(props, dict):
        return ("", "", "")

    def text(key):
        value = props.get(key)
        return value.strip() if isinstance(value, str) else ""
    return (text("locatorWorkspacePath"),
            text("locatorWorkspaceConnectionString"), text("locator"))


def is_windows_path(path):
    """True for a drive-letter or backslash path, which Windows compares
    without regard to case."""
    return "\\" in path or (path[1:2] == ":" and path[:1].isalpha())


def norm_path(path):
    """A path in one spelling: forward slashes, no dot segments, no trailing
    slash. A UNC path keeps its two leading slashes."""
    text = posixpath.normpath(path.strip().replace("\\", "/"))
    return "" if text == "/" else text


def path_within(child, parent):
    """True when child is parent or lies below it.

    Compared on whole path segments, so D:\\locators2 is not inside
    D:\\locators. A raw prefix comparison calls that a match, and a copied
    locator in a sibling folder would read as referenced.
    """
    if not child or not parent:
        return False
    fold = is_windows_path(child) or is_windows_path(parent)
    kid, top = norm_path(child), norm_path(parent)
    if fold:
        kid, top = kid.lower(), top.lower()
    return kid == top or kid.startswith(top + "/")


def same_path(one, other):
    """True when two spellings name the same folder."""
    return path_within(one, other) and path_within(other, one)


def is_copy_location(path):
    """True for a path inside the folder ArcGIS Server extracts a service
    definition into: arcgissystem/arcgisinput/<service>.<type>/extracted.

    This is positive evidence of a copy. Esri documents that folder as where a
    published service definition is decompressed and where copied data lands,
    under the system directory nobody may edit by hand, so no locator rebuild
    writes there. Matched on the arcgisinput and extracted segments rather
    than the whole path, because the system directory can be moved.
    """
    parts = norm_path(path).lower().split("/")
    if "arcgisinput" not in parts:
        return False
    return "extracted" in parts[parts.index("arcgisinput") + 1:]


def registered_folders(body):
    """The folders registered with the site, as [(server path, managed)].

    body is the answer to data/findItems under /fileShares. info.path is the
    path the server reads; a replicated folder's clientPath is the
    publisher's copy and is deliberately ignored. None stands for a lookup that
    failed, which is a different answer from a site with no folders. An
    answer with no items array is a failed lookup too: {} and an error page
    are not a site saying it has no folders.
    """
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    out = []
    for item in dict_entries(items):
        info = item.get("info")
        if not isinstance(info, dict):
            continue
        path = info.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        managed = (str(info.get("isManaged")).lower() == "true"
                   or str(info.get("dataStoreConnectionType")).lower()
                   in ("server", "serveronly"))
        out.append((path.strip(), managed))
    return out


def locator_verdict(workspace, connection, folders, flag=""):
    """Whether a geocode service reads its locator by reference, as
    ('true' | 'false' | '', note), in the by_reference column's words.

    Esri documents two ways to share a locator: reference registered data,
    where the service reads the locator where it is, and copy all data, where
    publishing copies it to the server. Two things settle it, in this order:
    a locator folder inside the server's own arcgisinput directory is a copy,
    and a manifest entry for that same folder carries its own byReference
    flag. Anything else is '' and reported as UNKNOWN, never guessed. The
    registered folders only choose the note. A registered folder is not
    evidence of a reference: Esri documents that copy all data copies
    registered data too, and documents locatorWorkspacePath as the folder
    the locator was published from, which a copy can share. Being in no
    registered folder is not evidence of a copy either: the same share
    registered under another spelling matches nothing here.
    """
    if not workspace:
        if connection:
            return "", ("locator copy or reference UNKNOWN: the locator is in "
                        "a geodatabase, and a connection string does not say "
                        "which")
        return "", ("locator copy or reference UNKNOWN: the service names no "
                    "locator folder")
    if is_copy_location(workspace):
        # Checked before the registered folders. A registered C:\ or
        # /home/arcgis holds the server's own directories too, and a rule
        # that a registered folder holds everything below it called this copy
        # a reference.
        return "false", ("locator copied to the server at publish time: it is "
                         "in the server's arcgisinput directory, so rebuilding "
                         "the source locator does not update this service. "
                         "Overwrite the service.")
    if flag == "true":
        return "true", ("locator read in place: the manifest entry for its "
                        "folder says byReference true")
    if flag == "false":
        return "false", ("locator copied to the server at publish time: the "
                         "manifest entry for its folder says byReference "
                         "false. Overwrite the service.")
    if folders is None:
        return "", ("locator copy or reference UNKNOWN: the registered "
                    "folders could not be read")
    managed = ""
    for path, is_managed in folders:
        if path_within(workspace, path):
            if is_managed:
                managed = path
                continue
            # This returned "true" and the run passed. A locator published by
            # copy from this very share reads the same, so the rule passed
            # the stale copies the tool exists to find.
            return "", ("locator copy or reference UNKNOWN: it is inside "
                        "registered folder %s, but Esri documents that copy "
                        "all data copies registered data too, so the folder "
                        "alone does not show which" % path)
    if managed:
        return "", ("locator copy or reference UNKNOWN: the locator is in %s, "
                    "a folder ArcGIS Server manages itself" % managed)
    return "", ("locator copy or reference UNKNOWN: its folder is not in the "
                "server's arcgisinput directory and not inside a registered "
                "folder as the site spells it. A share registered under "
                "another name, such as a full host name, a mapped drive or a "
                "DFS path, does not match")


def locator_entries(workspace, databases):
    """The manifest database entries whose connection names the locator
    folder itself, split from the rest as (own, other)."""
    own, other = [], []
    for database in databases:
        path = parse_conn(connection_string(database))["database"]
        if workspace and same_path(path, workspace):
            own.append(database)
        else:
            other.append(database)
    return own, other


def locator_flag(entries):
    """The byReference flag the locator's own manifest entries agree on, or
    '' when there are none or they disagree."""
    flags = set(by_reference_text(entry) for entry in entries)
    return flags.pop() if len(flags) == 1 else ""


def service_settings(service_json):
    """The service JSON fields the inventory reports beside the data source."""
    if not isinstance(service_json, dict):
        return {"capabilities": "", "extensions": ""}
    capabilities = service_json.get("capabilities")
    return {
        "capabilities": capabilities if isinstance(capabilities, str) else "",
        # Enabled extensions are not recreated by a plain republish, so they
        # belong in the parity diff. A disabled extension is not a capability
        # the new service has to match, so only the enabled ones are listed.
        # A typeName that is not text is not a name, and joining it raised.
        "extensions": ";".join(
            e.get("typeName")
            for e in dict_entries(service_json.get("extensions"))
            if str(e.get("enabled")).lower() == "true"
            and isinstance(e.get("typeName"), str)),
    }


def locator_row(base, notes, service_json, folders, flag=""):
    """The row of a geocode service's locator."""
    workspace, connection, name = locator_source(service_json)
    reference, note = locator_verdict(workspace, connection, folders, flag)
    row = dict(base)
    if workspace:
        row["database"] = workspace
    else:
        # parse_conn is the allowlist, so a saved password in the locator's
        # connection string reaches no column here either.
        row.update(parse_conn(connection))
    row["dataset"] = name
    row["by_reference"] = reference
    row["status"] = {"true": OK, "false": COPIED}.get(reference, UNRESOLVED)
    row["note"] = "; ".join(list(notes) + [note])
    return row


def database_rows(base, notes, databases):
    """One row per dataset of each manifest database entry."""
    out = []
    for database in databases:
        conn = parse_conn(connection_string(database))
        reference = by_reference_text(database)
        db_notes = list(notes)
        if not (conn["database"] or conn["server"] or conn["instance"]):
            # Nothing names what this service reads, so it is not a data
            # source that was read, whatever its flag says.
            status = UNRESOLVED
            db_notes.append("the manifest entry has no connection string this "
                            "tool can read")
        elif reference == "true":
            status = OK
        elif reference == "false":
            status = COPIED
            db_notes.append("data copied to the server at publish time")
        else:
            status = UNRESOLVED
            db_notes.append("copy or reference UNKNOWN: the manifest entry "
                            "has no byReference flag")
        datasets = dict_entries(database.get("datasets"))
        for dataset in (datasets or [{}]):
            row = dict(base)
            row.update(conn)
            row["status"] = status
            row["by_reference"] = reference
            label = dataset.get("onServerName") or dataset.get("onPremisePath")
            row["dataset"] = label if isinstance(label, str) else ""
            row["note"] = "; ".join(db_notes)
            out.append(row)
    return out


def rows_for_service(record, service_json, manifest, portal_item_id="",
                     folders=None):
    """Every row for one service. Pure: no network, no file.

    One row per dataset, because one service reads several and a row per
    service would have to pick one of them. A service with no dataset still
    gets a row, so that the service count in the CSV is the service count on
    the server.

    folders is registered_folders() for the site, or None when it was not
    read. Only a locator-backed service consults it.
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

    databases = manifest_databases(manifest)
    if typ in LOCATOR_TYPES:
        workspace, connection, _name = locator_source(service_json)
        if workspace or connection or databases:
            # The locator row comes first and always carries the
            # copy-or-reference verdict. Esri does not document that a
            # geocode manifest lists the locator under databases, so an
            # entry is taken as the locator's only when its connection names
            # the locator folder itself. Every other entry is its own row.
            own, other = locator_entries(workspace, databases)
            return ([locator_row(base, notes, service_json, folders,
                                 locator_flag(own))]
                    + database_rows(base, notes, other))

    if databases:
        return database_rows(base, notes, databases)
    row = dict(base)
    if typ in NO_DATASOURCE_TYPES:
        row["status"] = NODATA
        notes.append("a %s reads no data" % typ)
    elif not isinstance(manifest, dict):
        row["status"] = UNRESOLVED
        notes.append("no manifest reachable")
    else:
        row["status"] = UNRESOLVED
        # The key list is the diagnosis. A manifest holding only resources
        # is a service published from a document whose layers were all
        # copied, and that reads differently from a manifest that failed.
        notes.append("no databases in the manifest; keys=%s"
                     % ",".join(sorted(manifest.keys())))
    row["note"] = "; ".join(notes)
    return [row]


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
    # A copied locator is a locator file the server holds, not data in a
    # geodatabase, so it is counted on its own line and not on the
    # geodatabase one. rows_for_service puts a geocode service's locator row
    # first, so that row is the locator and any later one is a database.
    first = {}
    for row in services:
        first[(row.get("folder", ""), row.get("service", ""),
               row.get("type", ""))] = row
    copied_locators = set()
    copied = set()
    for row in rows:
        if row.get("status") != COPIED:
            continue
        key = (row.get("folder", ""), row.get("service", ""),
               row.get("type", ""))
        if row.get("type") in LOCATOR_TYPES and first[key] is row:
            copied_locators.add(key)
        else:
            copied.add(key)
    with_item = set((r.get("folder"), r.get("service"), r.get("type"))
                    for r in rows if r.get("source_item_id"))
    return {
        "copied_locators": len(copied_locators),
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
    if summary["copied_locators"]:
        out.append("locators copied to the server: %d geocode service(s). "
                   "Rebuilding the source locator does not update these; "
                   "overwrite the service." % summary["copied_locators"])
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
    """1 when any data source was not read, else 3 when a geocode service
    serves a copied locator, else 0.

    A copied locator is the finding a scheduled job has to see: the nightly
    rebuild succeeds and the service keeps serving the old copy. Exit 0 hid
    it from every job that reads only the code. An unread source still wins,
    because the inventory is then incomplete.
    """
    if summary["unresolved"]:
        return 1
    return 3 if summary["copied_locators"] else 0


def verdict_line(summary):
    """The last line the CLI prints. It says what the exit code says."""
    code = exit_code(summary)
    if code == 1:
        return ("FAIL: at least one data source could not be read. The "
                "inventory is incomplete.")
    if code == 3:
        return ("COPIED LOCATORS: every data source was read, and %d geocode "
                "service(s) serve a locator copied to the server. A rebuild "
                "of the source locator does not reach them."
                % summary["copied_locators"])
    if summary["copied"]:
        return ("PASS: every data source was read. %d service(s) read data "
                "copied to the server." % summary["copied"])
    return "PASS: every data source was read."


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
            # Also in the form urlencode put it in the url. A token holding
            # + / or = is percent-encoded there, and urllib quotes that url
            # back in its errors, so the raw form alone missed it.
            text = "%s" % (secret,)
            out = out.replace(text, REDACTED).replace(
                urllib.parse.quote_plus(text), REDACTED)
    return out


# ------------------------------------------------------------------ server io

def _opener(insecure):
    """Build a urllib opener, optionally without certificate verification.

    Verification is on. The harvested version of this script disabled it at
    import time for an internal self-signed certificate, which is a sensible
    thing to do on one site and an indefensible default in a published tool:
    the flag makes the decision visible in the command that made it.

    The verified context is built here rather than left to urllib. Python
    3.9's default handler carries no context until it connects, so the
    self-test could not see what it would verify, and crashed there.
    """
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), RefuseRedirect())


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    The token travels in the query string, and a proxy that redirects with
    the query intact hands it to whatever host the Location names, over
    plain http if that is what it says. The inventory would then describe a
    site the operator never named. The error names the new address so that
    it can be given as --server.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(
            "the site answered %d, a redirect to %s. Redirects are not "
            "followed, because the token would go with it. Give that address "
            "as --server if it is the site." % (code, newurl))


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
    if body.get("status") in ("error", "failed"):
        # The Admin API's own failure shape, documented under Operation
        # Response: {"status": "error", "messages": [...], "code": 498}. Read
        # as a success, an expired token made a manifest look empty.
        raise RuntimeError(redact(
            "%s: %s %s" % (endpoint, body.get("code"),
                           body.get("messages") or body["status"]),
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
    def get_json(url, params=None):
        query = dict(params or {})
        if token:
            query["token"] = token
        return _call(url, query, insecure=insecure, secret=token,
                     timeout=timeout)
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
        queue.extend(catalog_folders(listing, folder=folder.strip("/")))
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
        results = body.get("results")
        if not (isinstance(results, list) and results
                and isinstance(results[0], dict)):
            return ""
        ident = results[0].get("id")
        return ident if isinstance(ident, str) else ""
    return search


def inventory(get_json, root, records, search=None, echo=None,
              dump_manifest=None):
    """Build every row for every service. One Admin API call each, plus one.

    A service whose own JSON or manifest will not answer is reported with a
    note rather than dropped. The catalog is the list of services on the site,
    and a service missing from the CSV reads as a service that is not there.
    """
    rows = []
    folders = []            # [] not yet asked, [None] failed, [list] answered

    def site_folders():
        """The registered folders, asked for once and only when needed.

        A failed lookup is None, never an empty list. An empty list says no
        folder is registered, and the note would then give the wrong reason.
        """
        if not folders:
            try:
                folders.append(registered_folders(get_json(
                    "%s%s" % (root, FIND_ITEMS), FOLDER_QUERY)))
            except RuntimeError:
                folders.append(None)
        return folders[0]

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
        known = None
        workspace = locator_source(service_json)[0]
        # Asked for only when nothing else settles the locator: a folder in
        # arcgisinput is a copy, and a manifest entry for the folder carries
        # its own flag.
        if (record.get("type") in LOCATOR_TYPES and workspace
                and not is_copy_location(workspace)
                and not locator_flag(locator_entries(
                    workspace, manifest_databases(manifest))[0])):
            known = site_folders()
        rows.extend(rows_for_service(record, service_json, manifest, fallback,
                                     known))
    return rows


def csv_text(rows):
    """The inventory as CSV text. The header is written even for no rows.

    A site with nothing on it produces a file with one header line, not an
    empty file. An empty file is indistinguishable from a run that died, and
    the difference matters when the file is the cutover evidence.
    """
    # newline="" is not decoration. Without it the csv module's carriage return
    # meets the one the text layer adds on Windows, and every other line of the
    # file is blank, which Excel reads as an empty row between services.
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=list(COLUMNS),
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(dict((c, row.get(c, "")) for c in COLUMNS))
    return handle.getvalue()


def mask_passwords(value, secret=False):
    """A copy of parsed JSON with every saved password masked.

    A manifest's connection strings carry ENCRYPTED_PASSWORD, which can be
    reversed, and the dump is the file people attach to an issue. Every
    string is masked, not only the two documented connection string keys,
    because a missed key would be a credential on disk. Below a key naming a
    password every value is masked, whatever its type: a list or a number
    there used to reach the dump as it came.
    """
    if isinstance(value, dict):
        return dict((k, mask_passwords(
            v, secret or "password" in ("%s" % k).lower()))
            for k, v in value.items())
    if isinstance(value, list):
        return [mask_passwords(v, secret) for v in value]
    if secret:
        return REDACTED
    if isinstance(value, str):
        return PASSWORD_FIELD.sub(r"\1" + REDACTED, value)
    return value


def manifest_text(dumps):
    """The manifests --dump-manifest collected, as JSON, passwords masked."""
    return json.dumps(mask_passwords(dumps), indent=1, sort_keys=True)


def replace_file(path, text):
    """Write text to path whole, or leave what was there.

    The text goes to path.partial first and then replaces path in one step.
    Writing in place truncated the operator's last good inventory, and a
    failure part way left a CSV holding some rows and no sign that it was
    incomplete. A character UTF-8 cannot hold, such as a lone surrogate in a
    manifest, is written escaped rather than ending the write.
    """
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    data = text.encode("utf-8", "backslashreplace")
    partial = path + ".partial"
    try:
        with io.open(partial, "wb") as handle:
            handle.write(data)
        os.replace(partial, path)
    finally:
        # Gone after a successful replace; left behind by a failed one.
        if os.path.exists(partial):
            os.remove(partial)
    return path


def read_secret(user, env=SECRET_ENV, prompt=None):
    """The password, from the environment or an unechoed prompt. Never argv."""
    secret = os.environ.get(env)
    if secret:
        return secret
    try:
        return getpass.getpass(prompt or "password for %s (not echoed): "
                               % user)
    except EOFError:
        # A scheduled job has no terminal, and getpass reads end of file.
        # That is a run that could not sign in, which is exit 2, not the
        # gate's 1.
        raise RuntimeError("no password: %s is not set and there is no "
                           "terminal to prompt on" % env)


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core and the whole command line.

    No real site, no portal, no remote host, no credentials. The Admin API is
    answered inside this process, so the walk, the manifest read, the token
    exchange and every failure they can return are exercised without a
    socket. The command line is then run once more against the same site
    served by http.server on 127.0.0.1, so the real opener is exercised too.
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

    def tally(ok, bad):
        """Print the footer for ok passes and the bad labels. 0 when green."""
        total = ok + len(bad)
        if bad:
            print("%d assertions, %d failed" % (total, len(bad)))
            for label in bad:
                print("  FAILED: %s" % label)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    def raises(fn, label, kind=RuntimeError):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("svcsource self-test: no real site, no portal, no token; "
          "loopback only")
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
    raises(lambda: catalog_records({"services": [{"serviceName": "Roads"}]}),
           "a listing entry with no type stops the walk rather than dropping "
           "the service from the inventory  <-- pinned defect")
    raises(lambda: catalog_records({"services": [{"type": "MapServer"}]}),
           "and so does one with no name")
    raises(lambda: catalog_records({"services": [{"serviceName": 5,
                                                  "type": "MapServer"}]}),
           "a serviceName that is a number stops the walk with a message, not "
           "a traceback  <-- pinned defect")
    raises(lambda: catalog_records({"services": [{"serviceName": "R",
                                                  "type": 5}]}),
           "and so does a type that is a number")
    check(catalog_records({"services": []}) == [],
          "a folder with an empty services array reads as no services")
    raises(lambda: catalog_records({}),
           "a listing with no services key is not a catalog, so it stops the "
           "walk rather than reading as an empty site  <-- pinned defect")
    raises(lambda: catalog_records({"services": None}),
           "and so does a null services array")
    try:
        catalog_records({"services": 7}, "Transport")
    except RuntimeError as exc:
        check("folder Transport" in "%s" % exc and "JSON int" in "%s" % exc,
              "the message names the folder and what arrived in its place")

    # ---- every JSON array the site answers with, filled with something else.
    # A reverse proxy answering with its own error page, or a hand-edited
    # service record, used to end the walk in a TypeError with a traceback
    # instead of this tool's message, its redaction and its exit 2.
    check(dict_entries([{"a": 1}, None, 3, "x", []]) == [{"a": 1}],
          "the entries of a JSON array that are objects are the ones read")
    check(dict_entries(None) == [] and dict_entries("an error page") == []
          and dict_entries(7) == [] and dict_entries({"a": 1}) == [],
          "an array field holding anything but an array reads as empty")
    raises(lambda: catalog_records({"services": [None, 3, "x"]}),
           "a catalog whose service entries are not objects stops the walk "
           "with a message, not a traceback  <-- pinned defect")
    raises(lambda: catalog_records({"services": "<html>502 Bad Gateway</html>"}),
           "and so does an error page a proxy returned in its place, rather "
           "than reading as a site with no services  <-- pinned defect")
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
    check(catalog_folders({}) == [] and catalog_folders({"folders": None}) == [],
          "a catalog with no folders reads as none")
    check(catalog_folders({"folders": ["", "/", "Water"]}) == ["Water"],
          "a blank folder name is not walked")
    raises(lambda: catalog_folders({"folders": "<html>"}),
           "a folders field that is a string stops the walk rather than being "
           "walked one character at a time  <-- pinned defect")
    raises(lambda: catalog_folders({"folders": 5}),
           "and so does one that is a number")
    raises(lambda: catalog_folders({"folders": ["Water", 5]}),
           "and a folder entry that is not text  <-- pinned defect")

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
    easy = [parse_conn("INSTANCE=sde:oracle11g:%s:1521/orcl;DATABASE=" % h)
            for h in ("dbhostA", "dbhostB")]
    check([c["server"] for c in easy] == ["dbhostA:1521/orcl",
                                          "dbhostB:1521/orcl"],
          "an Oracle Easy Connect instance keeps its host, port and service "
          "name, so two servers on one port stay two  <-- pinned defect")
    oracle_rows = []
    for h in ("dbhostA", "dbhostB"):
        oracle_rows.extend(rows_for_service(
            {"service": h, "type": "MapServer"}, {}, {"databases": [{
                "byReference": True, "onServerConnectionString":
                "INSTANCE=sde:oracle11g:%s:1521/orcl;DATABASE=" % h}]}))
    check([s[1] for s in summarize(oracle_rows)["sources"]]
          == ["dbhostA:1521/orcl", "dbhostB:1521/orcl"],
          "and the summary lists them as two data sources, not one"
          "  <-- pinned defect")
    check(parse_conn("SERVER=machine;INSTANCE=5151")["server"] == "machine"
          and parse_conn("INSTANCE=5151")["server"] == "5151",
          "a 3-tier port is not a machine, so SERVER names it when it is "
          "there  <-- pinned defect")
    check(parse_conn("INSTANCE=sde:oracle;SERVER=h")["server"] == "h",
          "an instance too short to carry a host falls back to SERVER")
    check(parse_conn("SERVER=DBHOST1;INSTANCE=DBHOST1:1521:ORCL")["server"]
          == "DBHOST1",
          "an instance with colons but no sde: prefix is not split for a "
          "host, so SERVER names the machine  <-- pinned defect")
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
    typed = item_ids({"portalProperties": {"portalItems": [
        {"itemID": "abc", "type": 7}, {"itemID": 9, "type": "MapServer"}]}})
    check(typed == [("", "abc")]
          and primary_item_id(typed, "MapServer") == "abc",
          "an item type that is a number reads as no type, and an item ID "
          "that is a number is not an ID, rather than a crash"
          "  <-- pinned defect")
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
    check(service_settings({"capabilities": ["Map"], "extensions": [
        {"typeName": 5, "enabled": True},
        {"typeName": "WMSServer", "enabled": True}]})
          == {"capabilities": "", "extensions": "WMSServer"},
          "an extension name that is a number is skipped rather than ending "
          "the run, and capabilities that are not text are blank"
          "  <-- pinned defect")

    check(by_reference_text({"byReference": True}) == "true",
          "a registered data source reports byReference true")
    check(by_reference_text({"byReference": False}) == "false",
          "a copied data source reports byReference false")
    check(by_reference_text({"byReference": "false"}) == "false"
          and by_reference_text({"byReference": " TRUE "}) == "true",
          "byReference spelled as text is read as its word, not its "
          "truthiness, so the string false is not true  <-- pinned defect")
    check(by_reference_text({"byReference": None}) == ""
          and by_reference_text({"byReference": 1}) == ""
          and by_reference_text({"byReference": "yes"}) == "",
          "a null, a number or another word is no flag at all rather than a "
          "guess  <-- pinned defect")
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
    check(resource_document({"resources": [{"onPremisePath": 5},
                                           {"onPremisePath": "m.mxd"}]})
          == "m.mxd", "a resource path that is not text is stepped over too")

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
    check(summarize(rows)["sources"] == [("managed", "MAPSRV1", 1)],
          "and the copy it reads is still ranked among the data sources")

    unflagged = rows_for_service(roads, {}, {"databases": [{
        "onServerConnectionString": "INSTANCE=sde:sqlserver:DBHOST1;"
                                    "DATABASE=gisdb",
        "datasets": [{"onServerName": "gisdb.sdeowner.Roads"}]}]})
    check(unflagged[0]["status"] == UNRESOLVED
          and unflagged[0]["by_reference"] == ""
          and "UNKNOWN" in unflagged[0]["note"]
          and unflagged[0]["database"] == "gisdb",
          "a database entry with no byReference flag is UNKNOWN and "
          "unresolved, not ok, and still names its database"
          "  <-- pinned defect")
    check(rows_for_service(roads, {}, {"databases": [{
        "byReference": "false", "onServerConnectionString": "DATABASE=gisdb"}]}
    )[0]["status"] == COPIED,
          "a byReference of the string false is reported copied"
          "  <-- pinned defect")
    for blank in (5, "", "URL=https://x.example;CONNECTION_FILE=y"):
        unnamed = rows_for_service(roads, {}, {"databases": [{
            "byReference": True, "onServerConnectionString": blank,
            "datasets": [{"onServerName": "T"}]}]})
        check(unnamed[0]["status"] == UNRESOLVED
              and "no connection string" in unnamed[0]["note"]
              and exit_code(summarize(unnamed)) == 1,
              "a database entry whose connection is %r names nothing, so it "
              "is unresolved and fails the gate  <-- pinned defect"
              % (blank,))
    check(rows_for_service(roads, {}, {"databases": [{
        "byReference": True, "onServerConnectionString": "DATABASE=gisdb",
        "datasets": [{"onServerName": 7}]}]})[0]["dataset"] == "",
          "a dataset name that is not text is blank")

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

    # ---- a geocode service: is its locator a copy or a reference?
    # A nightly rebuild writes the locator in its folder. A service that reads
    # it there by reference serves the rebuild; a service that was given a
    # copy at publish time goes on serving the old one, with no error.
    COPY_DIR = ("C:\\arcgisserver\\directories\\arcgissystem\\arcgisinput\\"
                "Locators\\Address.GeocodeServer\\extracted\\p30")
    REG_DIR = "\\\\fileserver\\locators"
    check(locator_source({"properties": {
        "locatorWorkspacePath": REG_DIR, "locator": "Address",
        "locatorWorkspaceConnectionString": ""}})
          == (REG_DIR, "", "Address"),
          "the locator folder and name are read from the service properties")
    check(locator_source({"properties": {
        "locatorWorkspaceConnectionString": "DATABASE=gisdb",
        "locator": "gisdb.sde.Streets"}})
          == ("", "DATABASE=gisdb", "gisdb.sde.Streets"),
          "a locator in a geodatabase is read by its connection string")
    check(locator_source({"properties": {"locatorWorkspacePath": "  D:\\loc  ",
                                         "locator": 5}}) == ("D:\\loc", "", ""),
          "space around a property is trimmed and a name that is not text is "
          "blank")
    check(locator_source({}) == ("", "", "")
          and locator_source(None) == ("", "", "")
          and locator_source({"properties": "x"}) == ("", "", ""),
          "a service JSON with no usable properties names no locator")

    check(is_windows_path("C:\\loc") and is_windows_path("c:/loc")
          and is_windows_path("\\\\fileserver\\loc"),
          "a drive letter or a backslash is a Windows path")
    check(not is_windows_path("/srv/loc") and not is_windows_path(""),
          "a POSIX path and an empty one are not")
    check(norm_path("D:\\loc\\") == "D:/loc"
          and norm_path("\\\\fileserver\\loc") == "//fileserver/loc"
          and norm_path("/srv/a/../loc/") == "/srv/loc"
          and norm_path("/") == "",
          "a path is normalised to one spelling, keeping a UNC's two slashes")

    check(path_within("D:\\loc\\Address", "D:\\loc"),
          "a locator in a subfolder of a registered folder is within it")
    check(path_within("D:\\loc", "D:\\loc\\"),
          "the registered folder itself is within it, trailing slash or not")
    check(not path_within("D:\\loc2\\Address", "D:\\loc"),
          "a sibling folder that shares the prefix is not within it"
          "  <-- pinned defect")
    check(not path_within("D:\\loc\\..\\copies\\Address", "D:\\loc"),
          "a path that climbs out with .. is not within it  <-- pinned defect")
    check(path_within("d:\\LOC\\Address", "D:\\loc"),
          "Windows paths match whatever their case")
    check(not path_within("/srv/LOC/Address", "/srv/loc"),
          "POSIX paths do not, because Linux folders are case sensitive")
    check(path_within("\\\\fileserver\\loc\\Address", "//fileserver/loc"),
          "a UNC path matches its forward-slash spelling")
    check(path_within("/srv/loc", "/"),
          "a registered root holds everything below it")
    check(not path_within("", "D:\\loc") and not path_within("D:\\loc", ""),
          "an empty path is within nothing and holds nothing")
    check(path_within("", "") is False,
          "and two empty paths are not one folder, although both normalise "
          "to a dot  <-- pinned defect")
    check(same_path("D:\\loc\\", "d:/LOC") and not same_path("D:\\loc\\A",
                                                              "D:\\loc"),
          "two spellings of one folder are the same path, a subfolder is not")

    LINUX_COPY = ("/home/arcgis/arcgis/server/usr/directories/arcgissystem/"
                  "arcgisinput/Locators/Address.GeocodeServer/extracted/p30")
    check(is_copy_location(COPY_DIR) and is_copy_location(LINUX_COPY),
          "a folder under arcgisinput and extracted is where the server "
          "copies a published locator, on Windows and on Linux")
    check(is_copy_location("D:\\SYS\\ArcGISInput\\L\\A.GeocodeServer"
                           "\\Extracted\\v101"),
          "a moved system directory and any case still read as the copy "
          "location")
    check(not is_copy_location(REG_DIR + "\\streets")
          and not is_copy_location("D:\\arcgisinput\\streets")
          and not is_copy_location("D:\\extracted\\arcgisinput\\streets"),
          "a folder that merely shares one of the names is not")

    documented = {"items": [{
        "path": "/fileShares/dlsDataStore_shared", "type": "folder",
        "id": "469b09033859489a9506871215d6505a",
        "info": {"isManaged": False, "dataStoreConnectionType": "shared",
                 "path": "\\\\machine\\dlsDataStore"}}]}
    check(registered_folders(documented) == [("\\\\machine\\dlsDataStore",
                                              False)],
          "the folder item in Esri's documented findItems answer is read")
    check(registered_folders({"items": [{"info": {
        "dataStoreConnectionType": "replicated", "path": "E:\\server\\loc",
        "clientPath": "C:\\desk\\loc"}}]}) == [("E:\\server\\loc", False)],
          "a replicated folder is the server's path, not the publisher's"
          "  <-- pinned defect")
    check([m for _p, m in registered_folders({"items": [
        {"info": {"path": "a", "isManaged": True}},
        {"info": {"path": "b", "isManaged": "true"}},
        {"info": {"path": "c", "dataStoreConnectionType": "serverOnly"}},
        {"info": {"path": "d", "dataStoreConnectionType": "server"}}]})]
          == [True, True, True, True],
          "a folder the server manages is marked managed, however it is spelt")
    check(registered_folders({"items": [
        None, {"info": None}, {"info": {"path": ""}}, {"info": {"path": 5}},
        {"info": {"path": "  "}}, {"info": {}}]}) == [],
          "an item with no usable path is skipped, not a crash")
    check(registered_folders({"items": []}) == [],
          "a site with no registered folders answers an empty list")
    check(registered_folders(None) is None
          and registered_folders([1, 2]) is None,
          "a failed lookup is None, never the empty list a site with no "
          "folders gives  <-- pinned defect")
    check(all(registered_folders(body) is None
              for body in ({}, {"status": "success"}, {"items": None},
                           {"items": "<html>error</html>"})),
          "an answer with no items array is a failed lookup too, not a site "
          "with no folders  <-- pinned defect")

    reg = [(REG_DIR, False)]
    in_reg = locator_verdict(REG_DIR + "\\Address", "", reg)
    check(in_reg[0] == "" and "UNKNOWN" in in_reg[1],
          "a locator in a registered folder is UNKNOWN, because a copy "
          "published from that folder reads the same  <-- pinned defect")
    check("registered folder \\\\fileserver\\locators" in in_reg[1]
          and "copies registered data too" in in_reg[1],
          "and the note names the folder and why it does not settle it")
    verdict = locator_verdict(COPY_DIR, "", reg)
    check(verdict[0] == "false",
          "a locator in the server's arcgisinput directory is a copy"
          "  <-- pinned defect")
    check("does not update this service" in verdict[1]
          and "Overwrite the service" in verdict[1],
          "and the note says a rebuild will not reach it and what will")
    check(locator_verdict(COPY_DIR, "", [("C:\\arcgisserver", False)])[0]
          == "false"
          and locator_verdict(COPY_DIR, "", [("C:\\", False)])[0] == "false"
          and locator_verdict(LINUX_COPY, "", [("/home/arcgis", False)])[0]
          == "false",
          "a registered folder that holds the server's own directories does "
          "not turn the copy into a reference  <-- pinned defect")
    check(locator_verdict(COPY_DIR, "", None)[0] == "false",
          "the copy location settles it with no folder list at all")
    unmatched = locator_verdict(REG_DIR + "\\streets", "", [])
    check(unmatched[0] == "" and "UNKNOWN" in unmatched[1]
          and "Overwrite" not in unmatched[1],
          "a locator in no registered folder is UNKNOWN, not called a copy "
          "on missing evidence  <-- pinned defect")
    check("not inside a registered folder" in unmatched[1]
          and "could not be read" not in unmatched[1],
          "a site with no registered folders gives that reason, not a folder "
          "list that could not be read  <-- pinned defect")
    check(locator_verdict(REG_DIR + "\\streets", "",
                          [("\\\\fileserver.corp.example\\locators", False),
                           ("L:\\locators", False)])[0] == "",
          "the same share registered under a full host name or a drive "
          "letter is UNKNOWN too  <-- pinned defect")
    check(locator_verdict(REG_DIR, "", None)[0] == ""
          and "UNKNOWN" in locator_verdict(REG_DIR, "", None)[1],
          "registered folders that could not be read give UNKNOWN, not a "
          "guess  <-- pinned defect")
    check(locator_verdict("", "DATABASE=gisdb", reg)[0] == ""
          and "geodatabase" in locator_verdict("", "DATABASE=gisdb", reg)[1],
          "a locator in a geodatabase is UNKNOWN, because a connection "
          "string does not say")
    check(locator_verdict("", "", reg)[0] == ""
          and "no locator folder" in locator_verdict("", "", reg)[1],
          "a service naming no locator folder is UNKNOWN")
    check(locator_verdict(REG_DIR, "", [(REG_DIR, True)])[0] == ""
          and "manages itself" in locator_verdict(REG_DIR, "",
                                                  [(REG_DIR, True)])[1],
          "a locator in a server-managed folder is UNKNOWN, not referenced")
    after_managed = locator_verdict(REG_DIR + "\\A", "", [(REG_DIR, True),
                                                        (REG_DIR, False)])
    check(after_managed[0] == ""
          and "inside registered folder" in after_managed[1]
          and "manages itself" not in after_managed[1],
          "a user-registered folder is the one named even after a managed one")
    check(locator_verdict(REG_DIR, "", None, "true")[0] == "true"
          and locator_verdict(REG_DIR, "", [], "false")[0] == "false"
          and "Overwrite" in locator_verdict(REG_DIR, "", [], "false")[1],
          "the manifest's flag for the locator folder settles it when the "
          "path does not")
    check(locator_verdict(COPY_DIR, "", reg, "true")[0] == "false",
          "and the copy location outranks a flag that says otherwise")

    own_entry = {"byReference": True,
                 "onServerConnectionString": "DATABASE=" + REG_DIR}
    other_entry = {"byReference": True,
                   "onServerConnectionString": "DATABASE=gisdb"}
    check(locator_entries(REG_DIR + "\\", [own_entry, other_entry])
          == ([own_entry], [other_entry]),
          "a manifest entry is the locator's only when it names the locator "
          "folder itself")
    check(locator_entries("", [own_entry]) == ([], [own_entry]),
          "with no locator folder no entry is the locator's")
    check(not same_path(REG_DIR, REG_DIR + "\\streets")
          and not same_path(REG_DIR + "\\streets", REG_DIR),
          "a folder and its parent are not the same path, in either order"
          "  <-- pinned defect")
    parent_rows = rows_for_service(
        {"folder": "Locators", "service": "Streets", "type": "GeocodeServer"},
        {"properties": {"locatorWorkspacePath": REG_DIR + "\\streets",
                        "locator": "Streets"}},
        {"databases": [own_entry]}, folders=[])
    check([(r["status"], r["by_reference"]) for r in parent_rows]
          == [(UNRESOLVED, ""), (OK, "true")]
          and exit_code(summarize(parent_rows)) == 1,
          "a manifest entry for the PARENT of the locator folder does not "
          "settle the locator, so an unregistered locator is not passed"
          "  <-- pinned defect")
    child_rows = rows_for_service(
        {"folder": "Locators", "service": "Streets", "type": "GeocodeServer"},
        {"properties": {"locatorWorkspacePath": REG_DIR + "\\streets",
                        "locator": "Streets"}},
        {"databases": [{"byReference": True,
                        "onServerConnectionString":
                            "DATABASE=" + REG_DIR + "\\streets"},
                       {"byReference": False,
                        "onServerConnectionString":
                            "DATABASE=" + REG_DIR + "\\streets\\archive",
                        "datasets": [{"onServerName": "old"}]}]},
        folders=[(REG_DIR, False)])
    check([(r["status"], r["by_reference"]) for r in child_rows]
          == [(OK, "true"), (COPIED, "false")],
          "a manifest entry for a SUBFOLDER of the locator folder does not "
          "turn a locator read in place into a copy  <-- pinned defect")
    check(path_within("\\\\FS\\Locators\\streets", "//fs/locators")
          and path_within("//FS/Locators/streets", "\\\\fs\\locators")
          and "registered folder //fs/locators" in locator_verdict(
              "\\\\FS\\Locators\\streets", "", [("//fs/locators", False)])[1],
          "one side spelled with backslashes is enough to compare without "
          "regard to case  <-- pinned defect")
    check(locator_flag([own_entry]) == "true" and locator_flag([]) == ""
          and locator_flag([own_entry, dict(own_entry, byReference=False)])
          == "",
          "the locator's flag is the one its entries agree on, or none")

    geo = {"folder": "Locators", "service": "Address", "type": "GeocodeServer"}
    geo_json = {"properties": {"locatorWorkspacePath": COPY_DIR,
                               "locator": "Address"}}
    geo_manifest = {"resources": [{"onPremisePath": "C:\\desk\\Address.loc"}]}
    rows = rows_for_service(geo, geo_json, geo_manifest, folders=reg)
    check(len(rows) == 1 and rows[0]["status"] == COPIED,
          "a geocode service reading a copied locator is reported copied, "
          "not unresolved  <-- pinned defect")
    check(rows[0]["by_reference"] == "false"
          and rows[0]["database"] == COPY_DIR
          and rows[0]["dataset"] == "Address",
          "and the row names the locator folder and the locator")
    check(rows[0]["source_document"] == "C:\\desk\\Address.loc",
          "and still reports the source document from the manifest")
    check(sorted(rows[0]) == sorted(COLUMNS),
          "a locator row carries exactly the columns the CSV has")
    ref_json = {"properties": {"locatorWorkspacePath": REG_DIR,
                               "locator": "Address"}}
    rows = rows_for_service(geo, ref_json, geo_manifest, folders=reg)
    check(rows[0]["status"] == UNRESOLVED and rows[0]["by_reference"] == ""
          and exit_code(summarize(rows)) == 1,
          "a geocode service whose locator is only in a registered folder is "
          "unresolved, not ok, and fails the gate  <-- pinned defect")
    fqdn = rows_for_service(geo, {"properties": {
        "locatorWorkspacePath": "\\\\fs\\locators\\streets",
        "locator": "Streets"}}, {"databases": [{
            "byReference": False, "onServerConnectionString":
                "DATABASE=\\\\fs.corp.example\\locators\\streets"}]},
        folders=[("\\\\fs\\locators", False)])
    check([r["status"] for r in fqdn] == [UNRESOLVED, COPIED]
          and exit_code(summarize(fqdn)) == 1
          and "PASS" not in verdict_line(summarize(fqdn)),
          "a byReference false entry spelling the share by its full host name "
          "leaves the locator UNKNOWN and the run failed, never PASS"
          "  <-- pinned defect")
    rows = rows_for_service(geo, ref_json, geo_manifest)
    check(rows[0]["status"] == UNRESOLVED and rows[0]["by_reference"] == ""
          and "UNKNOWN" in rows[0]["note"],
          "with no folder list the same service is unresolved and UNKNOWN")
    rows = rows_for_service(geo, geo_json, None, folders=reg)
    check(rows[0]["status"] == COPIED,
          "an unreachable manifest does not stop the locator being resolved")
    rows = rows_for_service(geo, {"properties": {
        "locatorWorkspacePath": REG_DIR + "\\streets", "locator": "Streets"}},
        {}, folders=[])
    check(rows[0]["status"] == UNRESOLVED
          and exit_code(summarize(rows)) == 1,
          "a locator that nothing settles fails the gate rather than passing "
          "as a copy  <-- pinned defect")
    rows = rows_for_service(geo, {"properties": {
        "locatorWorkspaceConnectionString":
            "INSTANCE=sde:sqlserver:DBHOST1;DATABASE=gisdb;USER=loc;"
            "PASSWORD=hunter2;ENCRYPTED_PASSWORD=00022e59ab",
        "locator": "gisdb.loc.Streets"}}, geo_manifest, folders=reg)
    check(rows[0]["database"] == "gisdb" and rows[0]["server"] == "DBHOST1"
          and rows[0]["status"] == UNRESOLVED,
          "a geodatabase locator reports its database and stays unresolved")
    check(not any("hunter2" in v or "00022e59ab" in v
                  for v in rows[0].values()),
          "and its saved password reaches no column  <-- pinned defect")

    # The manifest's databases, beside the locator path.
    rows = rows_for_service(geo, geo_json, {"databases": [{
        "onServerConnectionString": "DATABASE=" + COPY_DIR,
        "datasets": [{"onServerName": "Address"}]}]}, folders=[])
    check(len(rows) == 1 and rows[0]["status"] == COPIED
          and rows[0]["by_reference"] == "false",
          "a manifest entry for the locator with no flag no longer skips the "
          "locator check, and the copy is still found  <-- pinned defect")
    rows = rows_for_service(geo, geo_json, {"databases": [{
        "byReference": True, "onServerConnectionString": "DATABASE=gisdb",
        "datasets": [{"onServerName": "gisdb.sde.AddressPoints"}]}]},
        folders=[])
    check([(r["status"], r["database"]) for r in rows]
          == [(COPIED, COPY_DIR), (OK, "gisdb")],
          "another database in the manifest is its own row, and does not "
          "make a copied locator read as referenced  <-- pinned defect")
    check(summarize(rows)["copied_locators"] == 1,
          "and the service is still counted as a copied locator")
    rows = rows_for_service(geo, geo_json, {"databases": [{
        "onServerConnectionString": "DATABASE=gisdb",
        "datasets": [{"onServerName": "gisdb.sde.AddressPoints"}]}]},
        folders=[])
    check(rows[1]["status"] == UNRESOLVED and "UNKNOWN" in rows[1]["note"],
          "and one with no flag is UNKNOWN in its own row")
    streets_json = {"properties": {"locatorWorkspacePath": REG_DIR,
                                   "locator": "Streets"}}
    rows = rows_for_service(geo, streets_json, {"databases": [{
        "byReference": True,
        "onServerConnectionString": "DATABASE=\\\\fileserver\\locators",
        "datasets": [{"onServerName": "Streets"}]}]}, folders=[])
    check(len(rows) == 1 and rows[0]["status"] == OK
          and rows[0]["by_reference"] == "true"
          and "byReference true" in rows[0]["note"],
          "a manifest entry naming the locator folder settles it with its own "
          "flag, in one row")
    rows = rows_for_service(geo, {}, {"databases": [other_entry]}, folders=reg)
    check([r["status"] for r in rows] == [UNRESOLVED, OK]
          and "no locator folder" in rows[0]["note"],
          "a geocode service naming no locator is UNKNOWN even when its "
          "manifest lists a database  <-- pinned defect")
    rows = rows_for_service(geo, {}, geo_manifest, folders=reg)
    check(rows[0]["status"] == UNRESOLVED
          and "keys=resources" in rows[0]["note"],
          "a geocode service naming no locator and no database reads as it "
          "did before")
    check(rows_for_service(geo, {}, None)[0]["note"] == "no manifest reachable",
          "and so does one whose manifest never answered")
    check(rows_for_service(roads, geo_json, geo_manifest, folders=reg)[0]
          ["status"] == UNRESOLVED,
          "locator properties on a map service are not read as its source")
    rows = rows_for_service(geo, geo_json, geo_manifest,
                            portal_item_id="fff66666666666666666666666666666",
                            folders=reg)
    check(rows[0]["source_item_id"] == "fff66666666666666666666666666666"
          and rows[0]["note"].startswith("item ID found by portal search")
          and "copied to the server" in rows[0]["note"],
          "a locator row keeps the portal-search note beside its own")

    # Eleven services on a copy and one on the reference: the shape of the
    # site this upgrade was written for.
    # The one on the reference says so in its manifest, because a registered
    # folder alone no longer settles it.
    ref_manifest = {"databases": [{
        "byReference": True, "onServerConnectionString": "DATABASE=" + REG_DIR}]}
    fleet = []
    for i in range(12):
        fleet.extend(rows_for_service(
            {"folder": "Locators", "service": "L%d" % i,
             "type": "GeocodeServer"},
            ref_json if i == 0 else geo_json,
            ref_manifest if i == 0 else geo_manifest, folders=reg))
    fleet_summary = summarize(fleet)
    check(fleet_summary["copied_locators"] == 11,
          "eleven geocode services on a copied locator are counted"
          "  <-- pinned defect")
    check(any("locators copied to the server: 11 geocode service(s)" in l
              for l in describe(fleet_summary)),
          "and the report names them as services a rebuild will not reach")
    check(exit_code(fleet_summary) == 3,
          "a copied locator exits 3, so a job reading only the code sees it, "
          "and not the gate's 1  <-- pinned defect")
    check(verdict_line(fleet_summary).startswith("COPIED LOCATORS:")
          and "11 geocode service(s)" in verdict_line(fleet_summary),
          "and the last line names the copies instead of saying PASS"
          "  <-- pinned defect")
    check(exit_code(summarize(fleet + rows_for_service(
        {"folder": "", "service": "Cached", "type": "MapServer"}, {}, None)))
          == 1,
          "an unread source still exits 1 beside a copied locator, because "
          "the inventory is incomplete")
    mixed = summarize(rows_for_service(
        geo, {"properties": {"locatorWorkspacePath": REG_DIR + "\\streets",
                             "locator": "Streets"}},
        {"databases": [{"byReference": True,
                        "onServerConnectionString":
                            "DATABASE=" + REG_DIR + "\\streets"},
                       {"byReference": False,
                        "onServerConnectionString":
                            "DATABASE=gisdb;INSTANCE=sde:sqlserver:H",
                        "datasets": [{"onServerName": "pts"}]}]},
        folders=reg))
    check((mixed["copied_locators"], mixed["copied"]) == (0, 1)
          and exit_code(mixed) == 0,
          "a geocode service reading its locator in place and a copied "
          "database is counted as copied data, not a copied locator"
          "  <-- pinned defect")
    check(summarize(rows_for_service(roads, {}, copied))["copied_locators"]
          == 0,
          "a copied map service is not counted as a copied locator")
    check(not any("locators copied" in l
                  for l in describe(summarize(fleet[:1]))),
          "a site whose locators are all referenced says nothing about them")

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
    check(verdict_line(summary).startswith("FAIL:"),
          "and the last line says FAIL")
    check(verdict_line(summarize(site_rows[-1:])) == "PASS: every data source "
          "was read. 1 service(s) read data copied to the server.",
          "a pass with copied data names the copies on the PASS line")
    check(verdict_line(summarize([])) == "PASS: every data source was read.",
          "a pass with nothing copied says only PASS")
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
    check(redact("url?token=Ab%2BCd%2FEf%3D%3D&f=json", "Ab+Cd/Ef==")
          == "url?token=***&f=json",
          "a token in the escaped form urlencode gave it is redacted too"
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
                     open_error=None, not_json=False, fail_after=None,
                     data_items=None):
            self.folders = folders or {}
            self.data_items = data_items
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
            if path.endswith(FIND_ITEMS):
                if self.data_items is None:
                    return {"error": {"code": 403,
                                      "message": "not permitted"}}
                return self.data_items
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

    # The README's site: a feature service, a geometry service and two
    # geocode services, one locator copied at publish time and one in a
    # registered folder. GEO_LISTED adds the manifest entry that settles the
    # second as read in place.
    GEO_FOLDERS = {
        "": {"folders": ["Locators"],
             "services": [{"serviceName": "Parcels", "type": "FeatureServer"},
                          {"serviceName": "Geometry",
                           "type": "GeometryServer"}]},
        "Locators": {"folders": [],
                     "services": [{"serviceName": "Address",
                                   "type": "GeocodeServer"},
                                  {"serviceName": "Streets",
                                   "type": "GeocodeServer"}]},
    }
    GEO_SERVICES = dict(SITE_SERVICES)
    GEO_SERVICES.update({
        "Locators/Address.GeocodeServer": {"properties": {
            "locatorWorkspacePath": COPY_DIR, "locator": "Address"}},
        "Locators/Streets.GeocodeServer": {"properties": {
            "locatorWorkspacePath": REG_DIR + "\\streets",
            "locator": "Streets"}},
    })
    GEO_MANIFESTS = dict(SITE_MANIFESTS)
    GEO_MANIFESTS.update({
        "Locators/Address.GeocodeServer": {
            "resources": [{"onPremisePath": "C:\\desk\\Address.loc"}]},
        "Locators/Streets.GeocodeServer": {
            "resources": [{"onPremisePath": "C:\\desk\\Streets.loc"}]},
    })
    GEO_LISTED = dict(GEO_MANIFESTS, **{
        "Locators/Streets.GeocodeServer": {
            "databases": [{
                "byReference": True,
                "onServerConnectionString": "DATABASE=" + REG_DIR + "\\streets",
                "datasets": [{"onServerName": "Streets"}]}],
            "resources": [{"onPremisePath": "C:\\desk\\Streets.loc"}]}})
    GEO_ITEMS = {"items": [
        {"path": "/fileShares/locators", "type": "folder",
         "info": {"isManaged": False, "dataStoreConnectionType": "shared",
                  "path": REG_DIR}}]}

    def geo_site(**kw):
        opts = {"folders": GEO_FOLDERS, "services": GEO_SERVICES,
                "manifests": GEO_MANIFESTS, "data_items": GEO_ITEMS}
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
    check(handler_context(type("Bare", (object,), {"handlers": []})())
          is None,
          "the context probe answers None for an opener with no TLS handler "
          "rather than inventing one")
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
    try:
        serving(site(open_error=boom),
                lambda: _call("%s/admin/services" % ROOT,
                              {"token": "Ab+Cd/Ef=="}, secret="Ab+Cd/Ef=="))
    except RuntimeError as exc:
        check("Ab%2BCd" not in "%s" % exc and "***" in "%s" % exc,
              "nor a token that urlencode escaped  <-- pinned defect")
    admin_error = {"status": "error", "code": 498,
                   "messages": ["Invalid token."]}
    errored = site(services={"Bad.MapServer": admin_error,
                             "Failed.MapServer": {"status": "failed"},
                             "Fine.MapServer": {"status": "success"}})
    raises(lambda: serving(errored, lambda: _call(
        "%s/admin/services/Bad.MapServer" % ROOT, {"token": "TESTTOKEN"})),
        "the Admin API's status error envelope raises")
    try:
        serving(errored, lambda: _call("%s/admin/services/Bad.MapServer"
                                       % ROOT, {"token": "TESTTOKEN"}))
    except RuntimeError as exc:
        check("498" in "%s" % exc and "Invalid token." in "%s" % exc,
              "the Admin API's status error envelope raises with its code "
              "and message  <-- pinned defect")
    raises(lambda: serving(errored, lambda: _call(
        "%s/admin/services/Failed.MapServer" % ROOT, {"token": "TESTTOKEN"})),
        "and so does status failed, the word the same page uses")
    check(serving(errored, lambda: _call(
        "%s/admin/services/Fine.MapServer" % ROOT, {"token": "TESTTOKEN"}))
          == {"status": "success"}, "while status success is an answer")
    anonymous = site()
    raises(lambda: serving(anonymous, lambda: json_getter(None)(
        "%s/admin/services" % ROOT, {"q": "x"})),
           "a getter with no token is refused by a site that wants one")
    check(anonymous.targets == ["%s/admin/services?q=x&f=json" % ROOT],
          "and it sends its own parameters and no empty token parameter")
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
    three = {"": {"folders": ["A", "B", "C"], "services": []},
             "A": {"folders": [], "services": []},
             "B": {"folders": [], "services": []},
             "C": {"folders": [], "services": []}}
    check(serving(site(folders=three), lambda: walk_catalog(
        json_getter("TESTTOKEN"), ROOT, max_folders=3)) == [],
          "exactly max_folders folders are walked")
    raises(lambda: serving(site(folders=three), lambda: walk_catalog(
        json_getter("TESTTOKEN"), ROOT, max_folders=2)),
           "one folder past max_folders refuses  <-- pinned defect")

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
    rows = serving(site(manifests=dict(SITE_MANIFESTS, **{
        "Parcels.FeatureServer": admin_error})), lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    check([r["note"] for r in rows if r["service"] == "Parcels"]
          == ["no manifest reachable"],
          "a manifest refused in the Admin API's shape reads as unreachable, "
          "not as a manifest with no databases  <-- pinned defect")

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

    # ---- the inventory of a site with geocode services
    check(not any(FIND_ITEMS in t for t in collected.targets),
          "a site with no geocode service never asks for the registered "
          "folders")
    located = geo_site()
    rows = serving(located, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    by_name = dict((r["service"], r) for r in rows)
    check(by_name["Address"]["status"] == COPIED
          and by_name["Address"]["by_reference"] == "false",
          "the geocode service on a copied locator is flagged copied"
          "  <-- pinned defect")
    check(by_name["Streets"]["status"] == UNRESOLVED
          and by_name["Streets"]["by_reference"] == ""
          and "inside registered folder" in by_name["Streets"]["note"],
          "the one in a registered folder is UNKNOWN, and the note names the "
          "folder  <-- pinned defect")
    finds = [t for t in located.targets if FIND_ITEMS in t]
    both = geo_site(services=dict(GEO_SERVICES, **{
        "Locators/Address.GeocodeServer": {"properties": {
            "locatorWorkspacePath": REG_DIR + "\\address",
            "locator": "Address"}}}))
    serving(both, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    check(len(finds) == 1
          and sum(1 for t in both.targets if FIND_ITEMS in t) == 1,
          "two geocode services that need the registered folders ask for "
          "them once  <-- pinned defect")
    check("parentPath=%2FfileShares" in finds[0]
          and "token=TESTTOKEN" in finds[0],
          "the folder search asks under /fileShares, with the token")
    check(summarize(rows)["copied_locators"] == 1
          and exit_code(summarize(rows)) == 1,
          "the site reports one copied locator, and fails the gate for the "
          "locator it could not settle")

    refused = geo_site(data_items=None)
    rows = serving(refused, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    by_name = dict((r["service"], r) for r in rows)
    check(len(rows) == 4 and by_name["Streets"]["status"] == UNRESOLVED
          and "UNKNOWN" in by_name["Streets"]["note"],
          "a folder search the site refuses leaves the registered locator "
          "UNKNOWN and the inventory whole  <-- pinned defect")
    check(by_name["Address"]["status"] == COPIED,
          "while the locator in arcgisinput is still a copy, because that "
          "needs no folder list")
    check(by_name["Streets"]["note"] == "locator copy or reference UNKNOWN: "
          "the registered folders could not be read",
          "and the note gives the refused search as the reason, not a folder "
          "that failed to match  <-- pinned defect")
    check(sum(1 for t in refused.targets if FIND_ITEMS in t) == 1,
          "and a refused search is asked for once")
    check(exit_code(summarize(rows)) == 1,
          "and the gate fails, because copy or reference was not settled")

    listed = geo_site(manifests=GEO_LISTED)
    rows = serving(listed, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    check(not any(FIND_ITEMS in t for t in listed.targets),
          "a copy in arcgisinput and a locator whose manifest entry carries "
          "its flag never ask for the folders")
    check(dict((r["service"], r["status"]) for r in rows)
          == {"Parcels": OK, "Geometry": NODATA, "Address": COPIED,
              "Streets": OK},
          "and both are still settled")
    check(exit_code(summarize(rows)) == 3,
          "that site reports one copied locator and every source read")
    in_gdb = geo_site(services=dict(GEO_SERVICES, **{
        "Locators/Streets.GeocodeServer": {"properties": {
            "locatorWorkspaceConnectionString": "DATABASE=gisdb",
            "locator": "Streets"}}}))
    serving(in_gdb, lambda: inventory(
        json_getter("TESTTOKEN"), ROOT,
        walk_catalog(json_getter("TESTTOKEN"), ROOT)))
    check(not any(FIND_ITEMS in t for t in in_gdb.targets),
          "a locator in a geodatabase has no folder to look up, so the "
          "folders are not asked for")

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

    class OddPortal(Site):
        """A portal whose search answers results in some other shape."""

        def __init__(self, results):
            Site.__init__(self)
            self.results = results

        def _body(self, path, params):
            return {"results": self.results}
    check(all(serving(OddPortal(odd), lambda: search(record)) == ""
              for odd in ({"id": "x"}, 5, "x", [], [None], [{"id": 5}])),
          "search results that are an object, a number, empty or hold no "
          "text ID return a blank ID rather than ending the inventory"
          "  <-- pinned defect")
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
    replace_file(out_path, csv_text(rows))
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
    replace_file(empty_path, csv_text([]))
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
    replace_file(nested, csv_text(rows))
    check(os.path.isfile(nested), "a missing output directory is created")
    extra = dict(rows[0])
    extra["surprise"] = "x"
    replace_file(os.path.join(tmp, "extra.csv"), csv_text([extra]))
    check(read_back(os.path.join(tmp, "extra.csv"))[0][0].get("surprise")
          is None, "a row carrying an unknown key does not break the writer")
    sparse = os.path.join(tmp, "sparse.csv")
    replace_file(sparse, csv_text([{"service": "Only"}]))
    check(read_back(sparse)[0][0]["service"] == "Only",
          "a row missing most columns is written with empty cells")
    dumps_path = os.path.join(tmp, "raw", "manifests.json")
    replace_file(dumps_path, manifest_text(
        {"Parcels.FeatureServer": {"databases": []}}))
    check(os.path.isfile(dumps_path),
          "a missing directory is created for the manifest dump as well")
    with io.open(dumps_path, "r", encoding="utf-8") as handle:
        check(json.load(handle)["Parcels.FeatureServer"] == {"databases": []},
              "the raw manifests are written as JSON when they are asked for")
    saved_conn = ("ENCRYPTED_PASSWORD=00022e68SAVED;SERVER=db1;"
                  "password = plain ;ENCRYPTED_PASSWORD_UTF8=SAVED8;"
                  "DATABASE=gis")
    replace_file(dumps_path, manifest_text({"Parcels.FeatureServer": {
        "databases": [{
            "onServerConnectionString": saved_conn,
            "onPremiseConnectionString": saved_conn,
            "Password": "SAVEDKEY", "dbPassword": ["SAVEDLIST"],
            "password": 87654321, "passwordPolicy": {"hint": ["SAVEDHINT"]},
            "byReference": True,
            "datasets": [{"onServerName": "gis.x.P"}]}]}}))
    with io.open(dumps_path, "r", encoding="utf-8") as handle:
        dumped_text = handle.read()
    check("SAVED" not in dumped_text and "plain" not in dumped_text,
          "a saved password in a manifest does not reach the dump on disk"
          "  <-- pinned defect")
    check("87654321" not in dumped_text,
          "a list, a number or an object under a password key is masked too"
          "  <-- pinned defect")
    entry = json.loads(dumped_text)["Parcels.FeatureServer"]["databases"][0]
    check(entry["onServerConnectionString"]
          == "ENCRYPTED_PASSWORD=***;SERVER=db1;password =***;"
             "ENCRYPTED_PASSWORD_UTF8=***;DATABASE=gis"
          and entry["passwordPolicy"] == {"hint": ["***"]}
          and entry["byReference"] is True
          and entry["datasets"] == [{"onServerName": "gis.x.P"}],
          "and everything else in the manifest is written as it came")
    check(mask_passwords("SERVER=h;PASSWORD=pa;ssTAIL;USER=u")
          == "SERVER=h;PASSWORD=***;USER=u"
          and mask_passwords("PASSWORD=pa;ssTAIL") == "PASSWORD=***",
          "a password holding a semicolon is masked to the next key, not to "
          "the semicolon  <-- pinned defect")

    # Written whole or not at all: the operator's last inventory survives a
    # write that fails, and no .partial file is left beside it.
    kept = os.path.join(tmp, "kept.csv")
    with io.open(kept, "w", encoding="utf-8") as handle:
        handle.write(u"PREVIOUS GOOD INVENTORY\n")

    class Unwritable(str):
        def encode(self, *args):
            raise IOError("disk full")
    raises(lambda: replace_file(kept, Unwritable("new")),
           "a write that fails raises", IOError)
    with io.open(kept, encoding="utf-8") as handle:
        check(handle.read() == "PREVIOUS GOOD INVENTORY\n",
              "and leaves the previous file whole  <-- pinned defect")
    raises(lambda: replace_file(tmp, "x"),
           "a path the file cannot replace raises", OSError)
    check(not os.path.exists(tmp + ".partial"),
          "and the partial file it wrote first is removed")
    replace_file(kept, csv_text([{"database": u"gis\ud800db"}]))
    with io.open(kept, encoding="utf-8") as handle:
        check("gis\\ud800db" in handle.read(),
              "a lone surrogate in a manifest is written escaped, not a "
              "half-written file  <-- pinned defect")

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
        os.environ.pop(SECRET_ENV, None)

        def no_terminal(prompt=""):
            raise EOFError()
        getpass.getpass = no_terminal
        raises(lambda: read_secret("gis_admin"),
               "with no variable and no terminal the run stops with a message "
               "instead of an EOFError traceback  <-- pinned defect")
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
    def usage_code(argv):
        """The exit code _parse stops with, or None when it does not."""
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                _parse(argv)
        except SystemExit as exc:
            return exc.code
    check(usage_code(["--nonexistent-flag"]) == 64,
          "an unknown flag stops the run with the usage code 64, not "
          "argparse's 2, which here means the site could not be read"
          "  <-- pinned defect")
    check(usage_code(["--server", ROOT, "--user", "u", "--timeout", "abc"])
          == 64, "and so does a timeout that is not a number")
    check(usage_code(["--server", ROOT, "--user", "u"]) is None,
          "while a correct command line does not stop at all")
    check(_parse([ROOT, "--user", "u"]).server == ROOT,
          "the server may be given positionally, for a script tool")
    check(_parse([ROOT, "--server", "https://other/arcgis", "--user", "u"]
                 ).server == "https://other/arcgis",
          "the named server wins when both forms are given")

    def run(argv):
        """main() with its output captured, so the self-test stays readable."""
        return captured(lambda: main(argv))

    os.environ[SECRET_ENV] = "hunter2"
    unwritten = os.path.join(tmp, "unwritten.csv")
    code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                              "gis_admin", "--out",
                                              unwritten]))
    check(code == 0, "a site that resolves every data source exits 0")
    check("PASS: every data source was read. 1 service(s) read data copied"
          in text, "and its last line passes and names the copied data")
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
    # A path of the test's own, not the default name in the working
    # directory. An operator's real services_inventory.csv sat there and
    # turned this assertion red.
    check(not os.path.exists(unwritten),
          "and no file appears where --out points  <-- pinned defect")

    applied = os.path.join(tmp, "applied.csv")
    code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                              "gis_admin", "--out", applied,
                                              "--apply"]))
    check(code == 0 and os.path.isfile(applied), "--apply writes the CSV")
    check("wrote %s" % applied in text, "and the run names the file it wrote")
    written, _ = read_back(applied)
    check(len(written) == 5, "the file holds every row the summary counted")
    with io.open(applied, encoding="utf-8") as handle:
        applied_text = handle.read()
    check("hunter2" not in applied_text and "TESTTOKEN" not in applied_text,
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
    code, text = serving(site(folders={"": {}}),
                         lambda: run(["--server", ROOT, "--user", "gis_admin"]))
    check(code == 2 and "services: 0" not in text and "PASS" not in text
          and "not an ArcGIS catalog" in text,
          "an empty object where the catalog belongs exits 2, not a clean "
          "empty site  <-- pinned defect")
    os.environ[SECRET_ENV] = "wrong"
    code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                              "gis_admin"]))
    check(code == 2, "a password the site refuses exits 2")
    check("wrong" not in text, "and the password is not printed back")
    os.environ.pop(SECRET_ENV, None)
    saved_getpass = getpass.getpass
    try:
        getpass.getpass = no_terminal
        code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                                  "gis_admin"]))
    finally:
        getpass.getpass = saved_getpass
    check(code == 2 and "no password" in text and "Traceback" not in text,
          "a scheduled run with no password and no terminal exits 2 with a "
          "message, not the gate's 1 with a traceback  <-- pinned defect")
    os.environ[SECRET_ENV] = "hunter2"

    def defect(get_json, root, max_folders=MAX_FOLDERS):
        raise KeyError("walked with token TESTTOKEN")
    saved_walk = globals()["walk_catalog"]
    globals()["walk_catalog"] = defect
    try:
        code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                                  "gis_admin"]))
    finally:
        globals()["walk_catalog"] = saved_walk
    check(code == 2 and "unexpected KeyError" in text
          and "TESTTOKEN" not in text and "rows:" not in text,
          "a defect in the tool exits 2 with the token redacted, never the "
          "gate's 1  <-- pinned defect")

    uni_csv = os.path.join(tmp, "unicode.csv")
    wide = site(manifests=dict(SITE_MANIFESTS, **{"Parcels.FeatureServer": {
        "databases": [{"byReference": True,
                       "onServerConnectionString":
                           "SERVER=DBHOST1;DATABASE=\u5730\u7406",
                       "datasets": [{"onServerName": "Parcels"}]}]}}))
    raw = io.BytesIO()
    narrow = io.TextIOWrapper(raw, encoding="cp1252")
    saved = (sys.stdout, sys.stderr)
    sys.stdout = sys.stderr = narrow
    try:
        code = serving(wide, lambda: main(["--server", ROOT, "--user",
                                           "gis_admin", "--out", uni_csv,
                                           "--apply"]))
    finally:
        sys.stdout, sys.stderr = saved
        narrow.flush()
        narrow.detach()
    console_text = raw.getvalue().decode("cp1252")
    check(code == 0 and "\\u5730\\u7406" in console_text
          and os.path.isfile(uni_csv),
          "a database name outside a redirected cp1252 console is printed "
          "escaped, and the file is still written  <-- pinned defect")
    with io.open(uni_csv, encoding="utf-8") as handle:
        check("\u5730\u7406" in handle.read(),
              "and the file keeps the name as it is")

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
    check(same_file(os.path.join(tmp, "c", "Inv.csv"),
                    os.path.join(tmp, "c", "..", "c", "inv.CSV"))
          == (os.name == "nt"),
          "two spellings differing in case are one file on Windows and two "
          "on POSIX, as the file system has it")
    code, text = serving(site(), lambda: run(
        ["--server", ROOT, "--user", "u", "--out",
         os.path.join(tmp, "c", "Inv.csv"), "--apply", "--dump-manifest",
         os.path.join(tmp, "c", "inv.CSV")]))
    check(code == (64 if os.name == "nt" else 0),
          "so on Windows a case-only difference cannot put the manifest dump "
          "over the CSV  <-- pinned defect")
    code, text = serving(site(), lambda: run(["--server", ROOT, "--user",
                                              "gis_admin", "--out", tmp,
                                              "--apply"]))
    check(code == 2,
          "a write that fails exits 2, not the gate's 1  <-- pinned defect")
    check("services:" not in text and "could not write" in text,
          "and prints no inventory, because the file is written before the "
          "summary")

    # A manifest nested past the recursion limit. json.loads reads it on
    # Python 3.12, and masking it for the dump raised RecursionError, which
    # the write block did not catch: a traceback, exit 1, a 0-byte dump.
    deep = []
    for _level in range(sys.getrecursionlimit() + 50):
        deep = [deep]
    raises(lambda: manifest_text({"deep": deep}),
           "a manifest nested past the recursion limit cannot be dumped",
           RecursionError)
    prior = os.path.join(tmp, "prior.csv")
    with io.open(prior, "w", encoding="utf-8") as handle:
        handle.write(u"PREVIOUS GOOD INVENTORY\n")
    deep_dump = os.path.join(tmp, "deep.json")

    def too_deep(dumps):
        return manifest_text({"deep": deep})
    saved_text = globals()["manifest_text"]
    globals()["manifest_text"] = too_deep
    try:
        code, text = serving(site(), lambda: run(
            ["--server", ROOT, "--user", "gis_admin", "--out", prior,
             "--dump-manifest", deep_dump, "--apply"]))
    finally:
        globals()["manifest_text"] = saved_text
    with io.open(prior, encoding="utf-8") as handle:
        prior_text = handle.read()
    check(code == 2 and "could not write: RecursionError" in text
          and "Traceback" not in text,
          "a dump that cannot be serialized exits 2 with a message, not a "
          "traceback and the gate's 1  <-- pinned defect")
    check(prior_text == "PREVIOUS GOOD INVENTORY\n"
          and not os.path.exists(deep_dump),
          "and neither file is touched, because both are built before either "
          "is written  <-- pinned defect")

    # Plain http sends the password and the token in the clear, so it needs
    # the same opt-in as unverified TLS.
    plain = site()
    code, text = serving(plain, lambda: run(
        ["--server", "http://gis.example.com/arcgis", "--user", "gis_admin"]))
    check(code == 64 and "plain http" in text and not plain.targets,
          "an http:// server without --insecure is refused before the "
          "password is sent  <-- pinned defect")
    code, text = serving(site(), lambda: run(
        ["--server", ROOT, "--user", "u", "--portal",
         "http://portal.example.com/portal/sharing/rest",
         "--public-rest-root", "https://gis.example.com/server/rest/services"]))
    check(code == 64 and "--portal is plain http" in text,
          "and so is an http:// portal  <-- pinned defect")
    code, text = serving(site(), lambda: run(
        ["--server", ROOT, "--user", "u", "--portal", "portal.example.com",
         "--public-rest-root", "https://gis.example.com/server/rest/services"]))
    check(code == 64 and "--portal must start with https://" in text,
          "a portal url with no scheme is a usage error too  <-- pinned defect")
    code, text = serving(site(), lambda: run(
        ["--server", "http://gis.example.com/arcgis", "--user", "gis_admin",
         "--insecure"]))
    check(code == 0 and "warning: --server is plain http" in text,
          "with --insecure it runs, and warns that nothing is encrypted")

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

    geo_csv = os.path.join(tmp, "geo.csv")
    code, text = serving(geo_site(manifests=GEO_LISTED), lambda: run(
        ["--server", ROOT, "--user", "gis_admin", "--out", geo_csv,
         "--apply"]))
    check(code == 3 and "locators copied to the server: 1 geocode service(s)"
          in text and "PASS" not in text and "COPIED LOCATORS:" in text,
          "the command line reports the copied locator, exits 3 and does not "
          "say PASS  <-- pinned defect")
    check("services: 4\nrows: 4\n" in text
          and "portal item IDs: 1 present, 3 missing" in text,
          "and prints the counts the README quotes for this site"
          "  <-- pinned defect")
    geo_back = dict((r["service"], r) for r in read_back(geo_csv)[0])
    check(geo_back["Address"]["by_reference"] == "false"
          and geo_back["Address"]["database"] == COPY_DIR
          and geo_back["Streets"]["by_reference"] == "true",
          "and the copied and referenced locators reach the file")
    check(geo_back["Streets"]["note"] == "locator read in place: the "
          "manifest entry for its folder says byReference true",
          "and the referenced locator's note names the manifest entry, as "
          "the README row quotes it  <-- pinned defect")

    # ---- the same command line against a real HTTP server on 127.0.0.1.
    # Everything above swaps _opener for a stand-in. This drives the real
    # opener, urllib, the socket and the JSON parse, against the same site
    # served by http.server on a loopback port the OS picks.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class StubHandler(BaseHTTPRequestHandler):
        def answer(self, query):
            path = urllib.parse.urlsplit(self.path).path
            self.server.site.targets.append(self.path)
            if path.endswith("/boom"):
                self.send_error(500, "stub failure")
                return
            if path.endswith("/moved"):
                # A proxy that keeps the query string, token and all.
                self.send_response(302)
                self.send_header("Location", "http://localhost:%d/elsewhere?%s"
                                 % (self.server.server_address[1], query))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.dumps(self.server.site._body(
                path, dict(urllib.parse.parse_qsl(query)))).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.answer(urllib.parse.urlsplit(self.path).query)

        def do_POST(self):
            query = self.rfile.read(
                int(self.headers.get("Content-Length") or 0)).decode("utf-8")
            self.server.site.posted.append(query)
            self.answer(query)

        def log_message(self, *args):
            pass

    def over_http(stub_site, fn):
        """Run fn(root) with stub_site served on a loopback port."""
        server = HTTPServer(("127.0.0.1", 0), StubHandler)
        server.site = stub_site
        worker = threading.Thread(target=server.serve_forever)
        worker.daemon = True
        worker.start()
        proxy_before = os.environ.get("no_proxy", "")
        # A proxy set in the environment would otherwise receive the calls.
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        try:
            return fn("http://127.0.0.1:%d/arcgis" % server.server_address[1])
        finally:
            os.environ["no_proxy"] = proxy_before
            server.shutdown()
            server.server_close()

    wire = geo_site()
    wire_csv = os.path.join(tmp, "wire.csv")
    code, text = over_http(wire, lambda url: run(
        ["--server", url, "--user", "gis_admin", "--out", wire_csv,
         "--apply", "--insecure"]))
    check(code == 1 and "services: 4" in text
          and "Locators/Streets.GeocodeServer: locator copy or reference "
          "UNKNOWN: it is inside registered folder" in text
          and "FAIL:" in text,
          "over a real socket the run reads the whole site and exits 1 for "
          "the locator only a registered folder vouches for  <-- pinned defect")
    check("locators copied to the server: 1 geocode service(s)" in text,
          "and still reports the copied locator")
    check(any("password=hunter2" in p for p in wire.posted)
          and not any("hunter2" in t for t in wire.targets),
          "the password travels in the POST body, never in a url")
    check(sum(1 for t in wire.targets if FIND_ITEMS in t) == 1,
          "the registered folders are asked for once over the wire")
    with io.open(wire_csv, encoding="utf-8") as handle:
        wire_text = handle.read()
    check("hunter2" not in wire_text and "TESTTOKEN" not in wire_text
          and "hunter2" not in text and "TESTTOKEN" not in text,
          "no credential reaches the file or the console  <-- pinned defect")
    wire_rows = dict((r["service"], r) for r in read_back(wire_csv)[0])
    check(wire_rows["Address"]["status"] == COPIED
          and wire_rows["Streets"]["status"] == UNRESOLVED,
          "the file says which locator is the copy and which is UNKNOWN")

    code, text = over_http(geo_site(data_items=None), lambda url: run(
        ["--server", url, "--user", "gis_admin", "--insecure"]))
    check(code == 1 and "the registered folders could not be read" in text
          and "not inside a registered folder" not in text,
          "a folder search refused over the wire exits 1 with UNKNOWN and "
          "says why  <-- pinned defect")

    def boom_call(url):
        try:
            _call("%s/boom" % url, {"token": "TESTTOKEN"}, secret="TESTTOKEN")
        except RuntimeError as exc:
            return "%s" % exc
    message = over_http(site(), boom_call)
    check("500" in message and "TESTTOKEN" not in message,
          "an HTTP 500 from a real server raises with the token redacted"
          "  <-- pinned defect")

    def moved_call(url):
        try:
            _call("%s/moved" % url, {"token": "TESTTOKEN"}, secret="TESTTOKEN")
        except RuntimeError as exc:
            return "%s" % exc
    moved = site()
    message = over_http(moved, moved_call)
    check("redirect" in message and "TESTTOKEN" not in message
          and not any("/elsewhere" in t for t in moved.targets),
          "a redirect is refused, so the token in the query string never "
          "reaches the host it names  <-- pinned defect")
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

    # The footer, fed a failure too so the red branch is proven to be red.
    with contextlib.redirect_stdout(io.StringIO()) as footer:
        red = tally(2, ["a planted failure"])
    check(red == 1 and "3 assertions, 1 failed" in footer.getvalue()
          and "  FAILED: a planted failure" in footer.getvalue(),
          "a failed assertion turns the footer red, names it and exits 1"
          "  <-- pinned defect")

    # Imported under any other name, the module defines its functions and
    # runs nothing. A tool that ran on import would start an inventory inside
    # whatever imported it.
    import runpy
    with contextlib.redirect_stdout(io.StringIO()) as imported:
        loaded = runpy.run_path(os.path.abspath(__file__),
                                run_name="svcsource_imported")
    check(callable(loaded.get("main")) and imported.getvalue() == "",
          "importing the module defines main and runs nothing")

    print("-" * 70)
    return tally(passed[0], failed)


# ----------------------------------------------------------------------- cli

class UsageParser(argparse.ArgumentParser):
    """argparse, exiting 64 on a usage error as the README documents.

    argparse's own code is 2, which is this tool's "the site could not be
    read", so a mistyped flag in a scheduled job read as a site outage.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


def same_file(one, other):
    """True when two paths name one file, as the file system compares them.

    normcase folds case and slashes on Windows, where Inv.csv and inv.CSV are
    one file. It does nothing on POSIX, where they are two.
    """
    # ponytail: a case-insensitive macOS volume is not folded here; compare
    # with os.path.samefile after the first write if that ever matters.
    return (os.path.normcase(os.path.realpath(one))
            == os.path.normcase(os.path.realpath(other)))


def _parse(argv):
    ap = UsageParser(
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
                    help="also write every manifest to this JSON file, saved "
                         "passwords masked, for a data source this tool did "
                         "not understand")
    ap.add_argument("--timeout", type=int, default=HTTP_TIMEOUT,
                    help="seconds to wait for one Admin API call (default %d)"
                         % HTTP_TIMEOUT)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification, for a site behind an internal "
                         "CA, and allow a plain http:// url. Off by default.")
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
    # A scheduled job redirects stdout, and on Windows that makes it cp1252.
    # A database name or locator path outside it then ended the summary in
    # a UnicodeEncodeError. An escaped character is still readable.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.server:
        print("error: --server is required. Use --self-test to verify the "
              "tool without a site.", file=sys.stderr)
        return 64
    for flag, url in (("--server", args.server), ("--portal", args.portal)):
        if not url:
            continue
        if not is_http_url(url):
            print("error: %s must start with https:// or http://, got %r."
                  % (flag, url), file=sys.stderr)
            return 64
        if url.lower().startswith("http://"):
            # Plain http sends the password and then the token unencrypted,
            # which is weaker than the unverified TLS that --insecure opts
            # into, so it needs the same opt-in. It used to need nothing.
            if not args.insecure:
                print("error: %s is plain http, which sends the password and "
                      "the token unencrypted. Use https://, or add --insecure "
                      "to accept that." % flag, file=sys.stderr)
                return 64
            print("warning: %s is plain http. The password and the token "
                  "cross the network unencrypted." % flag, file=sys.stderr)
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
    if args.dump_manifest and same_file(args.dump_manifest, args.out):
        print("error: --dump-manifest and --out cannot be the same file.",
              file=sys.stderr)
        return 64

    root = admin_root(args.server)
    dumps = {} if args.dump_manifest else None

    secret = token = portal_secret = portal_token = None
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
    except Exception as exc:
        # Anything else is a defect in this tool, and it still exits 2. An
        # uncaught exception exits 1, which is the gate's "inventory
        # incomplete", and a scheduled job would read a crash as a finding.
        print("error: unexpected %s: %s" % (
            type(exc).__name__,
            redact(exc, secret, token, portal_secret, portal_token)),
            file=sys.stderr)
        return 2

    summary = summarize(rows)
    # Written before anything is printed, so that nothing printed can stop
    # the file being written.
    wrote = []
    if args.apply:
        try:
            # Both texts are built before either file is touched, so a
            # manifest that cannot be serialized, such as one nested past the
            # recursion limit, leaves both files as they were.
            outputs = [(args.out, csv_text(rows))]
            if args.dump_manifest:
                outputs.append((args.dump_manifest, manifest_text(dumps)))
            for path, text in outputs:
                wrote.append(replace_file(path, text))
        except Exception as exc:
            # Every exception, not only IOError. A RecursionError escaped
            # here and exited 1, the gate's "inventory incomplete".
            print("error: could not write: %s: %s" % (
                type(exc).__name__,
                redact(exc, secret, token, portal_secret, portal_token)),
                file=sys.stderr)
            return 2

    for line in describe(summary):
        print(line)
    if wrote:
        print("\n" + "\n".join("wrote %s" % path for path in wrote))
    else:
        print("\nRead only. %s was not written. Re-run with --apply."
              % args.out)

    print("\n%s" % verdict_line(summary))
    return exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
