# svcsource

Report the database, instance, dataset and portal item ID behind every service on an ArcGIS Server
site, from the Admin API. No arcpy and no Pro licence.

You are moving an enterprise geodatabase to a new database server, and the first question is which
services read it. There are two hundred of them. Nothing you can reach says where any one of them
gets its data: `/rest/services` does not carry it, the service description every client reads does
not carry it, and the answer lives in the service manifest, which the Admin API hands you one
service at a time. So the inventory is somebody clicking through Manager for a day, and the
services they miss are the ones that break.

Then there is the trap that only surfaces after cutover. You republish a service on the new site,
the REST url comes out identical, every link in every document still resolves, and the map still
draws. A week later the web maps start coming back empty. A republished service is a new portal
item with a new item ID, and a cloned web map does not reference a layer by url. It references it
by item ID. The old item ID is in the service's own `portalProperties`, on the old server, which
you have just decommissioned. This tool writes that ID into a column before you touch anything.

```
$ python svcsource.py --self-test
svcsource self-test: no server, no portal, no network, no token
----------------------------------------------------------------------
PASS  the REST catalog url people paste is trimmed to the site root
PASS  a url with no scheme is refused before urllib quotes it back  <-- pinned defect
PASS  the REST path of the same service is Folder/Name/Type  <-- pinned defect
PASS  a serviceName that already carries its folder is not doubled  <-- pinned defect
PASS  a catalog whose service entries are not objects is read as empty, not as a traceback  <-- pinned defect
...
PASS  the database name is read
PASS  sde:sqlserver:HOST reports HOST as the machine
PASS  the geodatabase version is read, because a service on a child version reads different rows
PASS  a saved password in the connection string reaches no column  <-- pinned defect
PASS  a named SQL Server instance stays attached to its host  <-- pinned defect
PASS  a host carrying a port stays whole, two ports are two servers
PASS  an Oracle service name is read as the machine's address
PASS  a connection string with no INSTANCE falls back to SERVER
PASS  a file geodatabase reports its path, and its host is blank  <-- pinned defect
PASS  the publisher's connection string is read when it is the only one  <-- pinned defect
...
PASS  both portal items of one service are read
PASS  the item ID reported is the one whose type matches the service
PASS  the same service as a feature service reports the other item ID  <-- pinned defect
PASS  every item ID is listed, so a republish preserves both
PASS  the itemId spelling is read as the same field  <-- pinned defect
PASS  only the enabled extensions are listed  <-- pinned defect
...
PASS  a service reading two databases produces two rows  <-- pinned defect
PASS  one database holding three datasets makes three rows
PASS  a database listing no dataset still reports the database
PASS  data copied to the server is reported as copied, not as ok  <-- pinned defect
PASS  a manifest with no databases is unresolved, not silently ok  <-- pinned defect
PASS  a service whose manifest never answered is one unresolved row  <-- pinned defect
PASS  a GeometryServer with no manifest is expected, not a failure  <-- pinned defect
PASS  the summary counts services, not rows  <-- pinned defect
PASS  one unreadable data source fails the run  <-- pinned defect
...
PASS  the default opener verifies the certificate against the system trust store  <-- pinned defect
PASS  the password is posted, never put in the url  <-- pinned defect
PASS  and the password is not in the message  <-- pinned defect
PASS  a portal token comes from the portal's own endpoint, with no /admin in front of it  <-- pinned defect
PASS  an expired token raises instead of reading as an empty catalog  <-- pinned defect
PASS  a JSON list where an object belongs raises, because every caller reads it as an object  <-- pinned defect
PASS  and the url urllib quoted back carries no token  <-- pinned defect
PASS  a catalog that will not answer raises rather than reporting an empty site  <-- pinned defect
PASS  a folder that lists itself is visited once, not for ever
...
PASS  and its feature service item is in the column beside it  <-- pinned defect
PASS  a service whose own JSON will not answer is still inventoried  <-- pinned defect
PASS  a portal that will not answer returns a blank ID rather than stopping the inventory  <-- pinned defect
PASS  a service that knows its own item ID is not searched for
PASS  and no search call is made at all  <-- pinned defect
PASS  no doubled carriage return, which Excel reads as a blank row  <-- pinned defect
PASS  a site with no services writes a header and no rows  <-- pinned defect
...
PASS  --apply is off by default, so nothing is written  <-- pinned defect
PASS  --insecure is off by default, so certificates are verified  <-- pinned defect
PASS  --dump-manifest is off by default, so no raw manifest is written  <-- pinned defect
PASS  --out without --apply writes nothing and says so  <-- pinned defect
PASS  no credential reaches the file on disk  <-- pinned defect
PASS  a catalog that cannot be read exits 2, not the gate's 1  <-- pinned defect
PASS  and no inventory at all is printed  <-- pinned defect
PASS  a run with no --user is a usage error  <-- pinned defect
PASS  a write that fails exits 2, not the gate's 1  <-- pinned defect
PASS  the harness records a false check, a missing exception and two wrong exceptions as four failures, so a broken tool turns this self-test red  <-- pinned defect
----------------------------------------------------------------------
305 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Standard library only: `urllib`, `json`, `os`, `sys`, `ssl`, `io`, `csv`,
`argparse`, `getpass`, and `contextlib`, `tempfile` and `shutil` in the self-test. It runs on
ArcGIS Pro's Python and on a plain `python3`. `arcpy` is not used and the `arcgis` package is not
needed, so it runs on a server, on a laptop with no Pro licence, and in a scheduled job.

```
git clone https://github.com/uhsear/svcsource.git
python svcsource.py --self-test
```

`--self-test` needs no site, no portal, no network and no credentials, so you can check the tool
before you point it at a deployment. It covers the network paths as well as the decisions: the
token exchange, the catalog walk, the manifest read, a dead token, a transport failure and the
whole command line all run against a stand-in site that answers inside the process. No socket is
opened.

## Usage

Point it at the site and give it an administrator to sign in as.

```
export SVCSOURCE_PASSWORD='...'
python svcsource.py --server https://gis.example.com/arcgis --user gis_admin
```

Nothing is written until you ask for it:

```
python svcsource.py --server https://gis.example.com/arcgis --user gis_admin \
    --out services.csv --apply
```

Services published outside the portal have no item ID in their own record. The portal can be asked
instead, by the url it recorded on the item:

```
python svcsource.py --server https://gis.example.com/arcgis --user gis_admin \
    --portal https://portal.example.com/portal/sharing/rest \
    --public-rest-root https://gis.example.com/server/rest/services \
    --out services.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--server` | none | The site url. Required. `/rest/services`, `/admin/services` and `/manager` are trimmed off it. May also be given positionally. |
| `--user` | none | ArcGIS Server administrator to sign in as. Required. |
| `--out` | `services_inventory.csv` | Path for the inventory CSV. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--portal` | none | Portal `sharing/rest` root, which enables the item-ID search. |
| `--portal-user` | `--user` | Portal user for that search. |
| `--public-rest-root` | none | The site's public REST root. Required by `--portal`: it is the url portal items record. |
| `--dump-manifest` | off | Also write every raw manifest to this JSON file. |
| `--timeout` | `30` | Seconds to wait for one Admin API call. |
| `--insecure` | off | Skip TLS verification, for a site behind an internal CA. |
| `--self-test` | off | Run the offline assertions and exit. |

There is no `--password` flag and there never will be. `argv` is readable by every process on the
machine, and it lands in shell history and in scheduler logs. The password comes from
`SVCSOURCE_PASSWORD`, or from `SVCSOURCE_PORTAL_PASSWORD` for the portal, or from an unechoed
prompt. No credential reaches the CSV, and every error message is stripped of the password and the
token before it is printed, because `generateToken` is a POST that urllib repeats in some of its
exceptions and a token travels as a query parameter in a url urllib quotes back.

## What it reports

One row per dataset, because one service reads several and a row per service would have to pick
one of them. A service with no dataset still gets a row, so the service count in the file is the
service count on the site.

```
folder,service,type,path,status,source_item_id,all_item_ids,by_reference,dataset,
server,instance,database,db_user,version,capabilities,extensions,source_document,note
```

| Column | What it answers |
|---|---|
| `status` | `ok`, `copied`, `no-datasource` or `unresolved`. The gate reads this and nothing else. |
| `source_item_id` | The portal item ID to preserve when this service is republished. |
| `all_item_ids` | Every item ID on the service, as `Type=id`. A map service shared as a feature service has two. |
| `by_reference` | `false` means the data was copied to the server at publish time. |
| `dataset` | The dataset name as the server sees it. |
| `server`, `instance`, `database`, `db_user`, `version` | The connection the service reads through. |
| `extensions` | The enabled extensions, which a plain republish does not recreate. |
| `source_document` | The `.mxd` or `.aprx` on the publisher's machine, from the manifest. |

The summary on the screen is the part you read first:

```
services: 4
rows: 5

data sources:
  gisdb                        DBHOST1                     3 row(s)
  managed                      MAPSRV1                     1 row(s)

portal item IDs: 2 present, 2 missing
data copied to the server: 1 service(s). Moving the source geodatabase does not move these.

unresolved data sources: 1
  Locators/Address.GeocodeServer: no databases in the manifest; keys=databases,resources
```

`copied` is the line that changes a migration plan. A service published with its data copied to
the server does not read your enterprise geodatabase at all. It reads a copy inside the server's
own managed database, and moving the geodatabase it was copied from does nothing to it, in either
direction. The permissions page will not tell you that and the service url does not hint at it.

## Why not the tools that already exist

ArcGIS Server Manager shows a service's data source on the service's own page, and it is accurate.
Pro shows it too, in the Share pane and in Analyze, because publishing is what builds the manifest
in the first place. The ArcGIS API for Python reads the service catalog in two lines with
`server.services.list()` and each service's properties beside it, which is the right tool for
reading one site interactively.

All three are per service. The gap is the flat file: every service on the site, one row per
dataset, with the database, the host, the version and the item ID in columns you can sort, filter,
diff against the new site after cutover, and hand to somebody who has no Pro licence and no reason
to learn the Python API. Nobody clicks through two hundred services twice.

## The part that is actually hard

The connection string, which is where the answer lives, and which is not a url.

```
SERVER=DBHOST1;INSTANCE=sde:sqlserver:DBHOST1;DBCLIENT=sqlserver;DATABASE=gisdb;
USER=sdeowner;VERSION=sde.DEFAULT;AUTHENTICATION_MODE=DBMS
```

The host is the last field of `INSTANCE`, not the whole of it, and what follows the host is part
of the machine's address rather than decoration. `sde:sqlserver:DBHOST1\SQL2019` is a named
instance, `sde:sqlserver:10.0.0.5,1433` is a port, and an Oracle instance ends in a service name.
Truncating any of them merges two database servers into one row and the migration plan then moves
the wrong one. A file geodatabase source has no `INSTANCE` at all, and reports a path in the
`database` column with the host left empty.

Only five fields come out of that string. The string can also carry `ENCRYPTED_PASSWORD`, the
saved password of the connection the service was published from, and this tool writes a CSV that
people mail to each other.

The item ID has its own trap. `portalProperties.portalItems` is a list, not a field. A map service
shared as a feature service has two items, one per type, and a republish that preserves only the
first leaves every web map built on the feature layer pointing at an item that is gone. The row
reports the ID matching the service's own type and lists all of them in the column beside it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every service's data source was read. |
| 1 | At least one could not be, so the inventory is incomplete. |
| 2 | The site could not be read, or the file could not be written. No inventory is given. |
| 64 | Usage error. |

A site that cannot be read exits 2 and prints no inventory at all. A partial inventory is worse
than none: the services it missed are exactly the ones nobody remembers, which is why somebody ran
an inventory.

## Limits

- It reads one site. A deployment with several federated servers needs one run per server.
- `System` and `Utilities` are not walked. They hold the services ArcGIS Server publishes for
  itself, none of which reads your data.
- A `GeometryServer` reads nothing, so an absent manifest is the correct answer for it and is
  reported `no-datasource`. Every other type that answers without a manifest is reported
  `unresolved`, which fails the run. That is the safe direction, and it means a cached map service
  whose layers were all copied, and a `GeocodeServer` whose source is a locator file rather than a
  database, both read as unresolved. Their `source_document` column still says where the source
  is.
- The manifest is what the server recorded at publish time. A service republished by hand against
  a different connection, or a registered data store repointed since, is reported as the server
  believes it, not as it is. `--dump-manifest` is there for the cases where that matters.
- A row that reads `ok` says the service records a data source, not that the service can read it.
  A broken data connection still serves metadata, so a service with a dead connection answers its
  REST endpoint with a 200 and draws nothing. Proving a service is healthy means querying a layer
  and getting features back, which is a different tool and a different call. Read an `ok` row as
  "this is the database to move", never as "this service works".
- `--dump-manifest` writes the manifest verbatim, and a manifest can carry the saved password of
  the connection the service was published from. The CSV never does. Treat the dump as a
  credential.
- It is one Admin API call per service for the service JSON and one for the manifest, done in
  sequence. A site with a thousand services takes as long as that sounds. `--timeout` is per call.
- The portal fallback matches on the url the portal item recorded, which is not always the url the
  server reports: a web adaptor, a reverse proxy and a direct `:6443` all spell the same service
  differently. It is best effort by design and returns a blank ID rather than stopping the run.
- It compares nothing. Run it against the old site and the new one and diff the two CSVs with
  whatever you already use. [svcdrift](https://github.com/uhsear/svcdrift) is the tool for
  comparing a service against the dataset behind it.
- It changes nothing, on the server or in the portal. The only calls it makes are
  `generateToken`, `admin/services`, the service manifest and `sharing/rest/search`.
- `--insecure` skips certificate verification and exists for sites behind an internal CA. It is
  off by default. Add the CA to your trust store instead.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [svcdrift](https://github.com/uhsear/svcdrift) - the schema half: what a service was published with against what the dataset holds now
- [ghostsvc](https://github.com/uhsear/ghostsvc) - the services on the site that no portal item accounts for
- [itemcensus](https://github.com/uhsear/itemcensus) - the portal items on the other side of the item ID
