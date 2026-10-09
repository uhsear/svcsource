# svcsource

Report the database, instance, dataset and portal item ID behind every service on an ArcGIS Server
site, from the Admin API, and whether each geocode service reads its locator in place or a copy of
it. No arcpy and no Pro licence.

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

Geocode services have a trap of their own. A nightly job rebuilds the address locator from the
latest address points, the job log says it succeeded, and the geocoder keeps missing new addresses.
On one site, eleven of twelve geocode services had been published with the locator copied to the
server. The rebuild wrote a locator that nothing read. After the services were repointed at the
registered folder, the unmatched rate on a sample of 400 live addresses fell from 2.2% to 0.8%.
Nothing had reported an error. For every geocode service, this tool reports `copied` when the
locator is in the server's own `arcgisinput` directory, and takes the manifest's `byReference`
flag when the manifest names the locator folder. Anything else is UNKNOWN and the run exits 1. A
locator in a registered folder is UNKNOWN too, because Esri documents that "copy all data" copies
registered data as well.

```
$ python svcsource.py --self-test
svcsource self-test: no real site, no portal, no token; loopback only
----------------------------------------------------------------------
...
PASS  the REST catalog url people paste is trimmed to the site root
...
PASS  a url with no scheme is refused before urllib quotes it back  <-- pinned defect
...
PASS  the REST path of the same service is Folder/Name/Type  <-- pinned defect
...
PASS  a serviceName that already carries its folder is not doubled  <-- pinned defect
...
PASS  a listing with no services key is not a catalog, so it stops the walk rather than reading as an empty site  <-- pinned defect
...
PASS  a catalog whose service entries are not objects stops the walk with a message, not a traceback  <-- pinned defect
...
PASS  the database name is read
PASS  sde:sqlserver:HOST reports HOST as the machine
...
PASS  the geodatabase version is read, because a service on a child version reads different rows
...
PASS  a saved password in the connection string reaches no column  <-- pinned defect
PASS  a named SQL Server instance stays attached to its host  <-- pinned defect
PASS  a host carrying a port stays whole, two ports are two servers
PASS  an Oracle service name is read as the machine's address
PASS  an Oracle Easy Connect instance keeps its host, port and service name, so two servers on one port stay two  <-- pinned defect
...
PASS  a 3-tier port is not a machine, so SERVER names it when it is there  <-- pinned defect
...
PASS  a connection string with no INSTANCE falls back to SERVER
PASS  a file geodatabase reports its path, and its host is blank  <-- pinned defect
...
PASS  the publisher's connection string is read when it is the only one  <-- pinned defect
...
PASS  both portal items of one service are read
PASS  the item ID reported is the one whose type matches the service
PASS  the same service as a feature service reports the other item ID  <-- pinned defect
PASS  every item ID is listed, so a republish preserves both
...
PASS  the itemId spelling is read as the same field  <-- pinned defect
...
PASS  only the enabled extensions are listed  <-- pinned defect
...
PASS  a service reading two databases produces two rows  <-- pinned defect
...
PASS  one database holding three datasets makes three rows
...
PASS  a database listing no dataset still reports the database
...
PASS  data copied to the server is reported as copied, not as ok  <-- pinned defect
...
PASS  a manifest with no databases is unresolved, not silently ok  <-- pinned defect
...
PASS  a service whose manifest never answered is one unresolved row  <-- pinned defect
...
PASS  a GeometryServer with no manifest is expected, not a failure  <-- pinned defect
...
PASS  a sibling folder that shares the prefix is not within it  <-- pinned defect
PASS  a path that climbs out with .. is not within it  <-- pinned defect
...
PASS  the folder item in Esri's documented findItems answer is read
PASS  a replicated folder is the server's path, not the publisher's  <-- pinned defect
...
PASS  a failed lookup is None, never the empty list a site with no folders gives  <-- pinned defect
...
PASS  a locator in a registered folder is UNKNOWN, because a copy published from that folder reads the same  <-- pinned defect
...
PASS  a locator in the server's arcgisinput directory is a copy  <-- pinned defect
...
PASS  a registered folder that holds the server's own directories does not turn the copy into a reference  <-- pinned defect
...
PASS  a locator in no registered folder is UNKNOWN, not called a copy on missing evidence  <-- pinned defect
...
PASS  the same share registered under a full host name or a drive letter is UNKNOWN too  <-- pinned defect
PASS  registered folders that could not be read give UNKNOWN, not a guess  <-- pinned defect
PASS  a locator in a geodatabase is UNKNOWN, because a connection string does not say
...
PASS  a manifest entry for a SUBFOLDER of the locator folder does not turn a locator read in place into a copy  <-- pinned defect
...
PASS  a geocode service reading a copied locator is reported copied, not unresolved  <-- pinned defect
...
PASS  a geocode service whose locator is only in a registered folder is unresolved, not ok, and fails the gate  <-- pinned defect
PASS  a byReference false entry spelling the share by its full host name leaves the locator UNKNOWN and the run failed, never PASS  <-- pinned defect
...
PASS  a manifest entry naming the locator folder settles it with its own flag, in one row
...
PASS  eleven geocode services on a copied locator are counted  <-- pinned defect
...
PASS  a copied locator exits 3, so a job reading only the code sees it, and not the gate's 1  <-- pinned defect
PASS  and the last line names the copies instead of saying PASS  <-- pinned defect
...
PASS  the summary counts services, not rows  <-- pinned defect
...
PASS  one unreadable data source fails the run  <-- pinned defect
...
PASS  a token in the escaped form urlencode gave it is redacted too  <-- pinned defect
PASS  the default opener verifies the certificate against the system trust store  <-- pinned defect
...
PASS  the password is posted, never put in the url  <-- pinned defect
...
PASS  and the password is not in the message  <-- pinned defect
...
PASS  a portal token comes from the portal's own endpoint, with no /admin in front of it  <-- pinned defect
PASS  an expired token raises instead of reading as an empty catalog  <-- pinned defect
...
PASS  a JSON list where an object belongs raises, because every caller reads it as an object  <-- pinned defect
...
PASS  and the url urllib quoted back carries no token  <-- pinned defect
...
PASS  the Admin API's status error envelope raises with its code and message  <-- pinned defect
...
PASS  a catalog that will not answer raises rather than reporting an empty site  <-- pinned defect
...
PASS  a folder that lists itself is visited once, not for ever
...
PASS  and its feature service item is in the column beside it  <-- pinned defect
...
PASS  a service whose own JSON will not answer is still inventoried  <-- pinned defect
...
PASS  two geocode services that need the registered folders ask for them once  <-- pinned defect
...
PASS  a folder search the site refuses leaves the registered locator UNKNOWN and the inventory whole  <-- pinned defect
PASS  while the locator in arcgisinput is still a copy, because that needs no folder list
...
PASS  a portal that will not answer returns a blank ID rather than stopping the inventory  <-- pinned defect
...
PASS  a service that knows its own item ID is not searched for
PASS  and no search call is made at all  <-- pinned defect
...
PASS  no doubled carriage return, which Excel reads as a blank row  <-- pinned defect
PASS  a site with no services writes a header and no rows  <-- pinned defect
...
PASS  a saved password in a manifest does not reach the dump on disk  <-- pinned defect
PASS  a list, a number or an object under a password key is masked too  <-- pinned defect
...
PASS  a password holding a semicolon is masked to the next key, not to the semicolon  <-- pinned defect
...
PASS  and leaves the previous file whole  <-- pinned defect
...
PASS  a lone surrogate in a manifest is written escaped, not a half-written file  <-- pinned defect
...
PASS  --apply is off by default, so nothing is written  <-- pinned defect
PASS  --insecure is off by default, so certificates are verified  <-- pinned defect
PASS  --dump-manifest is off by default, so no raw manifest is written  <-- pinned defect
...
PASS  --out without --apply writes nothing and says so  <-- pinned defect
...
PASS  no credential reaches the file on disk  <-- pinned defect
...
PASS  a catalog that cannot be read exits 2, not the gate's 1  <-- pinned defect
PASS  and no inventory at all is printed  <-- pinned defect
...
PASS  a run with no --user is a usage error  <-- pinned defect
...
PASS  a write that fails exits 2, not the gate's 1  <-- pinned defect
...
PASS  a dump that cannot be serialized exits 2 with a message, not a traceback and the gate's 1  <-- pinned defect
PASS  and neither file is touched, because both are built before either is written  <-- pinned defect
PASS  an http:// server without --insecure is refused before the password is sent  <-- pinned defect
PASS  and so is an http:// portal  <-- pinned defect
PASS  a portal url with no scheme is a usage error too  <-- pinned defect
...
PASS  the command line reports the copied locator, exits 3 and does not say PASS  <-- pinned defect
PASS  and prints the counts the README quotes for this site  <-- pinned defect
PASS  and the copied and referenced locators reach the file
PASS  and the referenced locator's note names the manifest entry, as the README row quotes it  <-- pinned defect
PASS  over a real socket the run reads the whole site and exits 1 for the locator only a registered folder vouches for  <-- pinned defect
PASS  and still reports the copied locator
PASS  the password travels in the POST body, never in a url
PASS  the registered folders are asked for once over the wire
PASS  no credential reaches the file or the console  <-- pinned defect
PASS  the file says which locator is the copy and which is UNKNOWN
PASS  a folder search refused over the wire exits 1 with UNKNOWN and says why  <-- pinned defect
PASS  an HTTP 500 from a real server raises with the token redacted  <-- pinned defect
PASS  a redirect is refused, so the token in the query string never reaches the host it names  <-- pinned defect
PASS  the harness records a false check, a missing exception and two wrong exceptions as four failures, so a broken tool turns this self-test red  <-- pinned defect
PASS  a failed assertion turns the footer red, names it and exits 1  <-- pinned defect
PASS  importing the module defines main and runs nothing
----------------------------------------------------------------------
487 assertions, 0 failed
```

The count is 487 on Windows with Python 3.13. Every run prints the same lines.

## Requirements

Python 3.9 or newer. Standard library only: `urllib`, `json`, `os`, `posixpath`, `sys`, `ssl`,
`io`, `csv`, `argparse`, `getpass`, and `contextlib`, `tempfile`, `shutil`, `threading`,
`http.server` and `runpy` in the self-test. It runs on
ArcGIS Pro's Python and on a plain `python3`. `arcpy` is not used and the `arcgis` package is not
needed, so it runs on a server, on a laptop with no Pro licence, and in a scheduled job.

```
git clone https://github.com/uhsear/svcsource.git
python svcsource.py --self-test
```

`--self-test` needs no site, no portal, no remote host and no credentials, so you can check the
tool before you point it at a deployment. It covers the network paths as well as the decisions.
The token exchange, the catalog walk, the manifest read, the registered-folder search, a dead
token, a transport failure and the whole command line all run against a stand-in site that answers
inside the process. The command line then runs again against the same site, served by
`http.server` on `127.0.0.1`, so the real opener, `urllib` and the socket are exercised too.
Nothing leaves the machine.

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
| `--server` | none | The site url. Required. `/rest/services`, `/admin/services` and `/manager` are trimmed off it. May also be given positionally. An `http://` url needs `--insecure`. |
| `--user` | none | ArcGIS Server administrator to sign in as. Required. |
| `--out` | `services_inventory.csv` | Path for the inventory CSV. |
| `--apply` | off | Write `--out`. Without it nothing is written. |
| `--portal` | none | Portal `sharing/rest` root, which enables the item-ID search. |
| `--portal-user` | `--user` | Portal user for that search. |
| `--public-rest-root` | none | The site's public REST root. Required by `--portal`: it is the url portal items record. |
| `--dump-manifest` | off | Also write every manifest to this JSON file, with saved passwords masked. Needs `--apply`. |
| `--timeout` | `30` | Seconds to wait for one Admin API call. |
| `--insecure` | off | Skip TLS verification, for a site behind an internal CA, and allow an `http://` server or portal url. |
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
| `server`, `instance`, `database`, `db_user`, `version` | The connection the service reads through. For a geocode service, `database` is the locator folder and `dataset` is the locator. |
| `extensions` | The enabled extensions, which a plain republish does not recreate. |
| `source_document` | The `.mxd` or `.aprx` on the publisher's machine, from the manifest. |

The summary on the screen is the part you read first. This is a real run against a synthetic site
served on `127.0.0.1`. The site has one feature service, one geometry service and two geocode
services. The Streets manifest lists its locator folder with `byReference` true:

```
services: 4
rows: 4

data sources:
  C:\arcgisserver\directories\arcgissystem\arcgisinput\Locators\Address.GeocodeServer\extracted\p30 (none)                      1 row(s)
  \\fileserver\locators\streets (none)                      1 row(s)
  gisdb                        DBHOST1                     1 row(s)

portal item IDs: 1 present, 3 missing
locators copied to the server: 1 geocode service(s). Rebuilding the source locator does not update these; overwrite the service.

Read only. services_inventory.csv was not written. Re-run with --apply.

COPIED LOCATORS: every data source was read, and 1 geocode service(s) serve a locator copied to the server. A rebuild of the source locator does not reach them.
```

The run exits 3. A copied locator is not an unread source, so it is not the gate's 1, but a
scheduled job that reads only the exit code still sees it. The last line says the same thing, so
it never says PASS over a copy.

These are the two geocode rows the same run wrote with `--apply`:

```
Locators,Address,GeocodeServer,Locators/Address.GeocodeServer,copied,,,false,Address,,,C:\arcgisserver\directories\arcgissystem\arcgisinput\Locators\Address.GeocodeServer\extracted\p30,,,,,C:\desk\Address.loc,"locator copied to the server at publish time: it is in the server's arcgisinput directory, so rebuilding the source locator does not update this service. Overwrite the service."
Locators,Streets,GeocodeServer,Locators/Streets.GeocodeServer,ok,,,true,Streets,,,\\fileserver\locators\streets,,,,,C:\desk\Streets.loc,locator read in place: the manifest entry for its folder says byReference true
```

When the site refuses the registered-folder search, the run says it cannot tell, and exits 1. The
locator in `arcgisinput` is still reported as a copy, because that verdict needs no folder list:

```
locators copied to the server: 1 geocode service(s). Rebuilding the source locator does not update these; overwrite the service.

unresolved data sources: 1
  Locators/Streets.GeocodeServer: locator copy or reference UNKNOWN: the registered folders could not be read

Read only. services_inventory.csv was not written. Re-run with --apply.

FAIL: at least one data source could not be read. The inventory is incomplete.
```

For a geodatabase, `data copied to the server` is the line that changes a migration plan. A
service published with its data copied to the server does not read your enterprise geodatabase at all. It reads a copy inside the server's
own managed database, and moving the geodatabase it was copied from does nothing to it, in either
direction. The permissions page will not tell you that and the service url does not hint at it.

## Geocode services: copy or reference

Esri documents two ways to share a locator. With "reference registered data", the service reads
the locator where it is, in a folder registered with the server. With "copy all data", publishing
copies the locator to the server, and Esri's update procedure for that case is to overwrite the
service. A rebuild of the source locator reaches only the first kind.

The manifest does not settle it on its own. Esri documents `byReference` on the manifest's
`databases` entries, and the tool reads that flag when an entry names the locator's own folder.
Esri does not document whether a geocode service's manifest lists its locator there, and no real
geocode manifest was available to check. So the tool also reads two other documented sources:

1. The service's own JSON, from `admin/services/<name>.GeocodeServer`. Its `properties` carry
   `locatorWorkspacePath` for a locator file in a folder, `locatorWorkspaceConnectionString` for a
   locator in a geodatabase, and `locator` for the locator's name.
2. The folders registered with the site, from `admin/data/findItems?parentPath=/fileShares`. The
   `info.path` of each folder item is the path the server reads. The tool asks for this list once
   per run, and only when a geocode service needs it.

The rules apply in this order, and the first one that matches decides:

| What the tool finds | `by_reference` | `status` |
|---|---|---|
| The service JSON names a locator in a geodatabase, or no locator | blank, the note says UNKNOWN | `unresolved` |
| `locatorWorkspacePath` is below `arcgisinput` and then `extracted` | `false` | `copied` |
| A manifest `databases` entry names the locator folder and has a flag | the manifest's own flag | `ok` or `copied` |
| The registered folders could not be read | blank, the note says UNKNOWN | `unresolved` |
| `locatorWorkspacePath` is inside a folder the user registered | blank, the note says UNKNOWN | `unresolved` |
| The path is only inside a folder that ArcGIS Server manages itself | blank, the note says UNKNOWN | `unresolved` |
| The path is inside no registered folder | blank, the note says UNKNOWN | `unresolved` |

A geocode service whose JSON and manifest name nothing at all reads `unresolved`, as before. Every
other database entry in its manifest gets its own row, read as for any other service.

The `arcgisinput` rule is the only positive evidence of a copy. Esri's own example of a copied data
path is `C:\arcgisserver\directories\arcgissystem\arcgisinput\servicename.GPServer\extracted\...`,
and Esri says not to modify files in the system directory by hand. So no rebuild job writes there.
The rule matches the `arcgisinput` and `extracted` segments, not the whole path, because the
system directory can be moved. It is checked before the registered folders, because a registered
`C:\` or `/home/arcgis` also holds the server's own directories.

A path inside no registered folder is not called a copy. The same share registered under another
spelling, such as a full host name, a mapped drive or a DFS path, matches nothing here, and a
missing match is not evidence. The row reads UNKNOWN and the run exits 1.

The path comparison works on whole path segments, so `D:\locators2` is not inside `D:\locators`,
and a `..` segment cannot climb out of a registered folder. A path with a drive letter or a
backslash is compared without regard to case, as Windows does. A POSIX path is compared exactly.
A replicated folder is matched on the server's path, not on the publisher's `clientPath`.

The sources, all read on 2026-09-26:

- Service manifest, `byReference` and `resources.serverPath`:
  <https://developers.arcgis.com/rest/enterprise-administration/server/servicemanifest/>
- GeocodeServer properties `locatorWorkspacePath`, `locatorWorkspaceConnectionString` and
  `locator`: <https://resources.arcgis.com/en/help/server-admin-api/serviceTypes.html> and
  <https://developers.arcgis.com/rest/enterprise-administration/server/createservice/>
- The service resource returns those `properties`:
  <https://developers.arcgis.com/rest/enterprise-administration/server/service/>
- Find data items, `parentPath` and `types`:
  <https://developers.arcgis.com/rest/enterprise-administration/server/finddataitems/>
- Folder data item `info.path`, `clientPath`, `dataStoreConnectionType` and `isManaged`:
  <https://developers.arcgis.com/rest/enterprise-administration/server/dataitem/>
- Reference registered data against copy all data:
  <https://doc.esri.com/en/arcgis-pro/latest/help/sharing/overview/understanding-reference-registered-data-and-copy-all-data.html>
- A locator on a local machine is copied to the server when it is published:
  <https://doc.esri.com/en/arcgis-pro/latest/help/sharing/overview/publish-a-geocode-service.html>
- A copied locator is updated by overwriting the service. A registered-folder locator is replaced
  in the folder while the service is stopped:
  <https://doc.esri.com/en/arcgis-pro/latest/help/data/geocoding/keep-your-locator-up-to-date.html>
- A composite locator is always copied, and its participants in a registered folder are not:
  <https://doc.esri.com/en/arcgis-enterprise/latest/administer/geocode-services.html>
- A copied data path is under `arcgissystem\arcgisinput\<service>\extracted`:
  <https://doc.esri.com/en/arcgis-pro/latest/help/analysis/geoprocessing/share-analysis/publishing-web-tools-in-arcgis-pro.html>
- Do not modify files in the system directory by hand:
  <https://doc.esri.com/en/arcgis-enterprise/latest/administer/about-server-directories.html>

Before you trust the result on your own site, open one geocode service in the Admin API with
`?f=pjson` and check that `properties.locatorWorkspacePath` is there. `--dump-manifest` shows
whether your site's geocode manifests list the locator under `databases`.

## Why not the tools that already exist

ArcGIS Server Manager shows a service's data source on the service's own page, and it is accurate.
Pro shows it too, in the Share pane and in Analyze, because publishing is what builds the manifest
in the first place. The ArcGIS API for Python reads the service catalog in two lines with
`server.services.list()` and each service's properties beside it, which is the right tool for
reading one site interactively.

For locators, Pro's analyzer warns at publish time that data which is not registered with the
server will be copied, and Manager lists the registered folders under the site's data stores. Both
are correct. Neither leaves a record you can read a year later, when the nightly rebuild is the
only part anyone remembers.

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

The host is everything after the `sde:<dbms>:` prefix of `INSTANCE`, and what follows the host is
part of the machine's address rather than decoration. `sde:sqlserver:DBHOST1\SQL2019` is a named
instance, `sde:sqlserver:10.0.0.5,1433` is a port, and `sde:oracle11g:DBHOST1:1521/orcl` is an
Oracle Easy Connect string with a port and a service name. Truncating any of them merges two
database servers into one row and the migration plan then moves the wrong one. An `INSTANCE` with
no `sde:` prefix, such as `5151`, is a 3-tier port and not a machine, so the host is read from
`SERVER`. A file geodatabase source has no `INSTANCE` at all, and reports a path in the
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
| 0 | Every data source was read, and no geocode service serves a copied locator. |
| 1 | At least one data source could not be read, so the inventory is incomplete. |
| 2 | The site could not be read, or the file could not be written. No inventory is given. |
| 3 | Every data source was read, and at least one geocode service serves a copied locator. |
| 64 | Usage error. |

When one run has both an unread source and a copied locator, it exits 1, because the inventory is
incomplete. A site that cannot be read exits 2 and prints no inventory at all. A partial inventory
is worse than none: the services it missed are exactly the ones nobody remembers, which is why
somebody ran an inventory.

## Limits

- It reads one site. A deployment with several federated servers needs one run per server.
- `System` and `Utilities` are not walked. They hold the services ArcGIS Server publishes for
  itself, none of which reads your data.
- A `GeometryServer` reads nothing, so an absent manifest is the correct answer for it and is
  reported `no-datasource`. Every other type that answers without a manifest is reported
  `unresolved`, which fails the run. That is the safe direction, and it means a cached map service
  whose layers were all copied reads as unresolved. Its `source_document` column still says where
  the source is. A `GeocodeServer` is resolved from its locator path instead, as described above.
- `copied` for a locator means that its folder is under the server's `arcgisinput` directory, or
  that a manifest entry for its folder says `byReference` false. A locator in a folder that is not
  registered, or is registered under another spelling, reads UNKNOWN, not `copied`. On a site where
  many locators read UNKNOWN, compare their paths with the data stores by hand.
- The `arcgisinput` rule rests on Esri's documented example path for copied data, which is a
  geoprocessing service. No Esri page states the path for a copied locator. A site that stores
  its copies somewhere else reads UNKNOWN. A path spelled with the `${arcgisinput}` variable
  instead of the real directory is not matched either, and reads UNKNOWN.
- A composite locator is always copied, by Esri's design. Its participating locators can still be
  in a registered folder, and Esri's procedure for those is to rebuild them while the service is
  stopped. The tool does not read the participants. For a composite, a `copied` row does not prove
  that a rebuild never reaches it.
- A locator stored in a geodatabase reads UNKNOWN. A connection string does not say whether that
  database was registered, and a match against the registered databases would be a guess.
- The registered-folder search needs an account that may read the site's data items. When the
  site refuses the search, every geocode service that the search would settle reads UNKNOWN and
  the run exits 1. A locator under `arcgisinput` still reads `copied`.
- The locator field names come from Esri's documentation. They have not been checked against
  the service JSON and manifest of a live site. The self-test runs against synthetic services
  built from that documentation, and no real geocode manifest was available.
- The manifest is what the server recorded at publish time. A service republished by hand against
  a different connection, or a registered data store repointed since, is reported as the server
  believes it, not as it is. `--dump-manifest` is there for the cases where that matters.
- A row that reads `ok` says the service records a data source, not that the service can read it.
  A broken data connection still serves metadata, so a service with a dead connection answers its
  REST endpoint with a 200 and draws nothing. Proving a service is healthy means querying a layer
  and getting features back, which is a different tool and a different call. Read an `ok` row as
  "this is the database to move", never as "this service works".
- A manifest can carry the saved password of the connection the service was published from.
  `--dump-manifest` masks every `PASSWORD=`, `ENCRYPTED_PASSWORD=` and `ENCRYPTED_PASSWORD_UTF8=`
  field in every string, and every string under a key that contains `password`. Everything else
  is written as the server sent it, hosts and user names included, so share the dump with care.
- It is one Admin API call per service for the service JSON and one for the manifest, done in
  sequence. A site with a thousand services takes as long as that sounds. `--timeout` is per call.
- The portal fallback matches on the url the portal item recorded, which is not always the url the
  server reports: a web adaptor, a reverse proxy and a direct `:6443` all spell the same service
  differently. It is best effort by design and returns a blank ID rather than stopping the run.
- It compares nothing. Run it against the old site and the new one and diff the two CSVs with
  whatever you already use. [svcdrift](https://github.com/uhsear/svcdrift) is the tool for
  comparing a service against the dataset behind it.
- It changes nothing, on the server or in the portal. The only calls it makes are
  `generateToken`, `admin/services`, the service manifest, `admin/data/findItems` and
  `sharing/rest/search`.
- A long locator path pushes the data source summary out of its columns. The CSV is not affected.
- On Windows, the password prompt waits at the console. A scheduled job must set
  `SVCSOURCE_PASSWORD`, or the run waits for a password that nobody types.
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
