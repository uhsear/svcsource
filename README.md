# svcsource

Report the database, instance, dataset and portal item ID behind every service on an ArcGIS Server
site, from the Admin API. It also says whether each geocode service reads its locator in place or a
copy of it, which services read a given locator, which services recycle in the same minute against
one geodatabase, and which `.aprx` each service was published from. No arcpy and no Pro licence.

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
On one site I worked on, most of the geocode services had been published with the locator copied
to the server. The rebuild wrote a locator that nothing read. After the services were repointed at
the registered folder, the geocoder stopped missing the new addresses. That is my own observation
of one site, not a measurement this tool makes. Nothing had reported an error. For every geocode service, this tool reports `copied` when the
locator is in the server's own `arcgisinput` directory, and takes the manifest's `byReference`
flag when the manifest names the locator folder. Anything else is UNKNOWN and the run exits 1. A
locator in a registered folder is UNKNOWN too, because Esri documents that "copy all data" copies
registered data as well.

The worst failure reports success. A republish batch ran over a whole site. The script logged
success for every service in it, and not one service had changed. The log was the only evidence
anyone had, and it said the work was done. A tool that only asks "did the call return?" cannot tell
those two cases apart. This tool writes what the server records, so you can run it before the batch
and after it and diff the two files. A service that moved shows in the diff. A batch that did
nothing leaves the file exactly as it was. The self-test pins both outcomes.

The last three problems are the ones that surface in the week after cutover. You can ask this tool
about them in the same run, and each one is read only:

- `--locator PATH` turns the inventory round. A rebuild job writes one locator, and this lists
  which geocode services read it, which serve a copy of it, and which composites might use it.
  The participants of a composite are named by Esri, not located, so a composite is UNKNOWN. It is
  never reported as clean.
- `--recycle-with CSV` flags services that recycle in the same minute against one geodatabase,
  on this site and on the sites in other inventories. It is a risk heuristic and not a diagnosis.
- `--aprx-dir FOLDER` pairs each service with the `.aprx` to republish it from. When two projects
  match, it refuses. The row reads `AMBIGUOUS`, nothing is paired, and the run exits 4.

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
PASS  system and Utilities are listed, so a data service in one is read  <-- pinned defect
PASS  a GPServer, GeometryServer or SymbolServer in System or Utilities is the site's own, whatever the case of the folder
PASS  a data service in Utilities, or a GPServer outside it, is not left out  <-- pinned defect
PASS  the system folder name is matched at its last path segment
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
PASS  a password in the userinfo of a url is masked with no secret named  <-- pinned defect
PASS  the word after generateToken: is blanked, as the README Limits say
PASS  a colon form, a JSON snippet in a string, PASSWD=, PASS=, CREDENTIAL= and a passwd, pass or credential key are masked  <-- pinned defect
PASS  a token or secret is masked after a space, inside quotes, after a colon and in JSON, not only up to the next space  <-- pinned defect
PASS  an API key or access key in a url, a connection string or JSON is masked like a token  <-- pinned defect
PASS  a manifest key named for an API key or access key is masked whole, and one inside a string is masked too  <-- pinned defect
PASS  a password in the userinfo of a url inside a manifest is masked in the dump file, and a url with none is kept  <-- pinned defect
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
PASS  a data service in a folder named Utilities is walked, and the site's own services beside it are not  <-- pinned defect
PASS  an unreadable service in Utilities fails the run, not PASS  <-- pinned defect
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
PASS  a non-ASCII folder, and a service name holding a space or #, are read, and every request carries its token  <-- pinned defect
...
PASS  a portal that will not answer returns None, which is not a blank ID, rather than stopping the inventory  <-- pinned defect
...
PASS  a double quote in a service name goes into the portal query as it is, which the README Limits say
...
PASS  two items that record one service url are several, not the first one  <-- pinned defect
PASS  a portal that lists fewer hits than its total is several, and a single hit with the same url in another case, or with no url, is the item  <-- pinned defect
PASS  a hit that names another url is not this service  <-- pinned defect
...
PASS  a portal search that failed marks the row, fails the run and says so, rather than reading as a service with no item  <-- pinned defect
PASS  a service with no item ID in its own record and no --portal search exits 5 and does not say PASS  <-- pinned defect
...
PASS  several portal items blank the item ID, fail the run and say so, rather than a PASS with one arbitrary ID  <-- pinned defect
PASS  a service that knows its own item ID is not searched for
PASS  and no search call is made at all  <-- pinned defect
...
PASS  no doubled carriage return, which Excel reads as a blank row  <-- pinned defect
PASS  a site with no services writes a header and no rows  <-- pinned defect
...
PASS  a saved password in a manifest does not reach the dump on disk  <-- pinned defect
PASS  a list, a number or an object under a password key is masked too  <-- pinned defect
PASS  a PWD= field, a token= parameter and a key naming a token or secret are masked in the dump  <-- pinned defect
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
PASS  a password typed on the command line as --password is not echoed in the usage error, which still exits 64  <-- pinned defect
...
PASS  a password typed on the command line as -p is not echoed in the usage error, which still exits 64  <-- pinned defect
PASS  a password typed on the command line as --pwd is not echoed in the usage error, which still exits 64  <-- pinned defect
PASS  a password typed on the command line as --apply is not echoed in the usage error, which still exits 64  <-- pinned defect
PASS  a password typed on the command line as --timeout is not echoed in the usage error, which still exits 64  <-- pinned defect
...
PASS  a password typed on the command line as hunter2 is not echoed in the usage error, which still exits 64  <-- pinned defect
PASS  an unknown flag is not quoted back, and the error says where the password goes
PASS  a quoted value in any other usage error is masked
...
PASS  --out without --apply writes nothing and says so  <-- pinned defect
PASS  the command line exits 5 for a service with no item ID and no --portal search, says so on the last line, and still writes the inventory  <-- pinned defect
...
PASS  neither the password nor the token the test used reaches the file on disk  <-- pinned defect
...
PASS  a service name with an escape sequence is printed as sent, which the README Limits say
...
PASS  a credential in the --server url is not echoed in the error line  <-- pinned defect
PASS  a --server url with user:password@ is refused with 64 before any call, and the password is not echoed  <-- pinned defect
...
PASS  a --portal url with user:password@ is refused with 64 before any call, and the password is not echoed  <-- pinned defect
PASS  a --server url with a password in the port position is refused with 64 before any call, and the password is not echoed  <-- pinned defect
PASS  a --portal url with a password in the port position is refused with 64 before any call, and the password is not echoed  <-- pinned defect
PASS  an @ before the first ? or # counts as user information, so a password holding a / is refused too; one in the query or the fragment does not  <-- pinned defect
PASS  a bad scheme does not echo the password beside it  <-- pinned defect
PASS  a catalog that cannot be read exits 2, not the gate's 1  <-- pinned defect
PASS  and no inventory at all is printed  <-- pinned defect
...
PASS  a valid empty catalog is trusted: PASS, exit 0 and a header-only file, as the README Limits say
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
PASS  a portal whose search is down fails the run from the command line, and the last line says why  <-- pinned defect
...
PASS  the command line reports the copied locator, exits 3 and does not say PASS  <-- pinned defect
PASS  and prints the counts the README quotes for this site  <-- pinned defect
PASS  and the copied and referenced locators reach the file
PASS  and the referenced locator's note names the manifest entry, as the README row quotes it  <-- pinned defect
PASS  over a real socket the run reads the whole site and exits 1 for the locator only a registered folder vouches for  <-- pinned defect
PASS  and still reports the copied locator
PASS  the password travels in the POST body, never in a url
PASS  the registered folders are asked for once over the wire
PASS  neither the password nor the token the test used reaches the file or the console  <-- pinned defect
PASS  the file says which locator is the copy and which is UNKNOWN
PASS  a folder search refused over the wire exits 1 with UNKNOWN and says why  <-- pinned defect
PASS  an HTTP 500 from a real server raises with the token redacted  <-- pinned defect
PASS  a redirect is refused, so the token in the query string never reaches the host it names  <-- pinned defect
...
PASS  one participant that cannot be named makes the list UNKNOWN, because a short list would read as a whole one  <-- pinned defect
...
PASS  while a service that reads its own data in place is not tied to the file it was once published from  <-- pinned defect
...
PASS  a geocode service whose REST resource was unreadable may be a composite, so it is UNKNOWN for every target  <-- pinned defect
...
PASS  a locator is unsettled when any service is UNKNOWN, or when nothing matched and geocode services exist  <-- pinned defect
PASS  an unmatched service is a namesake when its locator has this locator's last segment, with or without .loc, in any case
PASS  a READS answer is unsettled when another service records a locator of the same name under another path, and settled when the others record different locators  <-- pinned defect
PASS  --locator names the namesake services when it is unsettled and stays quiet when the others record different locators
...
PASS  no match with geocode services present is UNSETTLED, names the spelling limit and lists their locator paths, and never says nothing uses the locator  <-- pinned defect
...
PASS  a string of digits too long for int() on Python 3.11 is not an interval, and no traceback follows  <-- pinned defect
...
PASS  two projects that share only a parent folder name are AMBIGUOUS, never paired on it  <-- pinned defect
PASS  a recorded .mxd, or no recorded path, cannot tell two projects apart  <-- pinned defect
...
PASS  a service whose own JSON was unreadable says so in its note  <-- pinned defect
...
PASS  the summary prints the AMBIGUOUS note whole, even when a project path holds a semicolon  <-- pinned defect
...
PASS  --aprx-dir that pairs no service fails the run, because a wrong folder cannot be told from a site with no projects  <-- pinned defect
...
PASS  a service whose JSON would not answer fails the run and the last line says why  <-- pinned defect
...
PASS  an unsettled --locator target fails the run with its own last line  <-- pinned defect
...
PASS  a folder that cannot be listed stops the run, because a skipped folder can hold the second project  <-- pinned defect
...
PASS  a prefix of any new flag, or of --apply, is refused with 64  <-- pinned defect
...
PASS  a locator no service matched exits 1 and says it compared path text only  <-- pinned defect
PASS  --locator exits 1, not 0, when one service reads the locator in place and another reads a locator of the same name through a mapped drive  <-- pinned defect
...
PASS  a geocode service whose REST resource could not be read leaves the --locator answer UNKNOWN and exits 1, not 0  <-- pinned defect
...
PASS  an empty --locator is a usage error, not a clean answer  <-- pinned defect
...
PASS  two inventories with the same file name are two sites, told apart by their paths  <-- pinned defect
PASS  a password in the --server address never reaches the recycle skew site label  <-- pinned defect
...
PASS  a --recycle-with that is not a file is a usage error before any sign-in  <-- pinned defect
PASS  an --out that is also a --recycle-with input is a usage error before any sign-in  <-- pinned defect
PASS  the same --recycle-with file twice is a usage error before any sign-in  <-- pinned defect
PASS  an --aprx-dir that is not a folder is a usage error before any sign-in  <-- pinned defect
PASS  and with --apply the input inventory is left as it was  <-- pinned defect
...
PASS  a cell starting with equals gets one apostrophe  <-- pinned defect
...
PASS  a cell starting with tab gets one apostrophe  <-- pinned defect
PASS  a cell starting with carriage return gets one apostrophe  <-- pinned defect
...
PASS  a byte-order mark before a trigger is not guarded, as the README Limits say
...
PASS  text that starts with apostrophes and then a trigger gets one more, so the guard can be undone exactly
...
PASS  the dataset column is guarded for every trigger, tab and carriage return included  <-- pinned defect
...
PASS  the database column is guarded for every trigger, tab and carriage return included  <-- pinned defect
...
PASS  the recycle columns stay numeric: no apostrophe
PASS  the line the README quotes for hostile text is what csv_text writes, note and source document in their own columns  <-- pinned defect
...
PASS  a run against a site with trigger text writes every one guarded  <-- pinned defect
...
PASS  read_inventory returns what the tool saw, not what it wrote  <-- pinned defect
PASS  a database that starts with a trigger still groups with the same database in another site's file  <-- pinned defect
...
PASS  --aprx-dir pairs the service it can and refuses the one that two projects match, with exit 4  <-- pinned defect
...
PASS  two inventories of one unchanged site are identical, and a service that really moved shows in the diff: a batch that reported success and changed nothing leaves the CSV as it was
PASS  over a socket --locator finds the service that reads the locator, and the copied locator still exits 3
PASS  over a socket the AMBIGUOUS refusal exits 4
PASS  over a socket a geocode resource that answers an error leaves --locator UNKNOWN and exits 1
PASS  the harness records a false check, a missing exception and two wrong exceptions as four failures, so a broken tool turns this self-test red  <-- pinned defect
PASS  a failed assertion turns the footer red, names it and exits 1  <-- pinned defect
PASS  importing the module defines main and runs nothing
----------------------------------------------------------------------
717 assertions, 0 failed
```

The block above is an excerpt of the real run, not the whole output. Each `...` line stands for omitted `PASS` lines, every line shown is
verbatim from a fresh run in the same order, and the footer is the count of the whole run. A run
prints 717 assertions in all. The count is 717 on Windows with Python 3.13, 3.12 and 3.9, and on
Linux with Python 3.12. Python 3.9 was not run on Linux. Every run prints the same lines.

## Requirements

Python 3.9 or newer. Standard library only: `urllib`, `json`, `os`, `posixpath`, `sys`, `ssl`,
`io`, `re`, `csv`, `argparse`, `getpass`, and `contextlib`, `tempfile`, `shutil`, `threading`,
`http.server` and `runpy` in the self-test. It runs on
a plain `python3` and passed its self-test under the Python of an ArcGIS Pro conda environment
(3.13.7) and under the Python that ships inside a Pro install (also 3.13.7). `arcpy` is not used and the `arcgis` package is not
needed, so it runs on a server, on a laptop with no Pro licence, and in a scheduled job.

```
git clone https://github.com/uhsear/svcsource.git
python svcsource.py --self-test
```

`--self-test` needs no site, no portal, no remote host and no credentials, so you can check the
tool before you point it at a deployment. It covers the network paths as well as the decisions.
The token exchange, the catalog walk, the manifest read, the registered-folder search, the REST
read of a geocode service, a dead token, a transport failure and the whole command line all run
against a stand-in site that answers inside the process. The command line then runs again against
the same kind of site, served by `http.server` on `127.0.0.1`, so the real opener, `urllib` and the
socket are exercised too. Nothing leaves the machine.

The sample outputs further down are not printed by `--self-test`. I made them by running the tool
against a local stand-in site, built like the self-test's `http.server` one, with synthetic
service names, and pasting what it printed. That stand-in site is not one of the four shipped files, so
you cannot reproduce these samples from the repository alone. The self-test is the part you can
run.

Branch coverage of the self-test is 100%, measured with coverage 7.16.1 on Python 3.13.2:

```
python -m coverage run --branch svcsource.py --self-test
python -m coverage report -m
```

```
Name           Stmts   Miss Branch BrPart  Cover   Missing
----------------------------------------------------------
svcsource.py    2457      0    558      0   100%
----------------------------------------------------------
TOTAL           2457      0    558      0   100%
```

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

Services published outside the portal have no item ID in their own record. Without `--portal`, a
service of a type that can have a portal item and has none exits 5, and the last line says
`NO ITEM IDS`, so a job does not read a blank item ID as a clean pass. The portal can be asked
instead, by the url it recorded on the item:

```
python svcsource.py --server https://gis.example.com/arcgis --user gis_admin \
    --portal https://portal.example.com/portal/sharing/rest \
    --public-rest-root https://gis.example.com/server/rest/services \
    --out services.csv --apply
```

The three read-only extras ride on the same walk and add no write:

```
python svcsource.py --server https://gis.example.com/arcgis --user gis_admin \
    --locator '\\fileserver\locators\streets\Streets.loc' \
    --recycle-with other_site/services_inventory.csv \
    --aprx-dir D:\projects
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
| `--locator` | none | A locator file a rebuild writes. Prints which geocode services read it, serve a copy of it, or may use it. Repeat the flag for more than one. An empty value is a usage error. |
| `--recycle-with` | none | An inventory CSV this tool wrote for another site, which joins the recycle skew check. Repeat the flag for more than one. |
| `--aprx-dir` | none | A folder of `.aprx` projects. Each service is paired with one, or refused as `AMBIGUOUS`. The folder is walked, never written. |
| `--timeout` | `30` | Seconds to wait for one Admin API call. |
| `--insecure` | off | Skip TLS verification, for a site behind an internal CA, and allow an `http://` server or portal url. |
| `--self-test` | off | Run the offline assertions and exit. |

A flag may not be shortened. `--ap` is a usage error with exit 64, and it does not mean `--apply`.
The parser sets `allow_abbrev=False`, and the self-test pins that a prefix of the write flag and of
each new flag is refused.

There is no `--password` flag and there never will be. `argv` is readable by every process on the
machine, and it lands in shell history and in scheduler logs. The password comes from
`SVCSOURCE_PASSWORD`, or from `SVCSOURCE_PORTAL_PASSWORD` for the portal, or from an unechoed
prompt. The CSV keeps only five keys of a connection string (see Limits for the text fields that are written as the site sent them), and every error message is stripped of the password and the
token before it is printed, because `generateToken` is a POST that urllib repeats in some of its
exceptions and a token travels as a query parameter in a url urllib quotes back. A password typed
on the command line by mistake, as `--password=hunter2`, is not echoed in the usage error: the
error says that an argument was not recognised and does not print it, and any quoted value in
another usage error is shown as `'***'`, including a value that holds an apostrophe, which argparse
quotes with double quotes. A `--server` or `--portal` address with an `@` before its first `?` or `#`
is refused with exit 64 before any call. That covers `user:password@`, also when the password holds
a `/`. So is an address whose port is not a number, such as `https://host:PASSWORD/arcgis`, which
urllib quotes back in its error. urllib cannot open a `user:password@` address, and
through an http proxy it can, which would put the password in the site name that the summary
prints.

## What it reports

One row per dataset, because one service reads several and a row per service would have to pick
one of them. A service with no dataset still gets a row, so the service count in the file is the
service count on the site.

```
folder,service,type,path,status,source_item_id,all_item_ids,by_reference,dataset,
server,instance,database,db_user,version,capabilities,extensions,source_document,note,
participants,recycle_start,recycle_interval,aprx
```

| Column | What it answers |
|---|---|
| `status` | `ok`, `copied`, `no-datasource` or `unresolved`. Only the `unresolved` count of the exit code comes from this column. The exit code also reads the `note` column (service JSON unread, portal search failed, no item, several items) and the `aprx` column (`NOT FOUND`, `AMBIGUOUS`), so a run can exit 1 while every row says `ok`. Gate on the exit code, not on this column. |
| `source_item_id` | The portal item ID to preserve when this service is republished. |
| `all_item_ids` | Every item ID on the service, as `Type=id`. A map service shared as a feature service has two. |
| `by_reference` | `false` means the data was copied to the server at publish time. |
| `dataset` | The dataset name as the server sees it. |
| `server`, `instance`, `database`, `db_user`, `version` | The connection the service reads through. For a geocode service, `database` is the locator folder and `dataset` is the locator. |
| `extensions` | The enabled extensions, which a plain republish does not recreate. |
| `source_document` | The `.mxd` or `.aprx` path the manifest records in `onPremisePath`. When a manifest resource has no path, the cell holds its `clientName`, which Esri documents as the publishing machine and not a file. The `aprx` pairing uses `onPremisePath` only. |
| `participants` | For a geocode service only. The reference names of a composite's locators, joined with `\|`. Blank for a single locator. `UNKNOWN` when the REST resource would not answer. |
| `recycle_start`, `recycle_interval` | The service's `recycleStartTime` as `HH:MM`, and its `recycleInterval` in whole hours. Blank when the service JSON does not give a value in that form. |
| `aprx` | With `--aprx-dir`: the paired project, `AMBIGUOUS`, or `NOT FOUND`. Blank without the flag, and for a service that reads no project. |

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
it never says PASS over a copied locator. A service that copied ordinary data to the server is
different: the run still exits 0, and the last line says PASS and counts those services, when
every service that can have a portal item has one.

These are the two geocode rows the same run wrote with `--apply`:

```
Locators,Address,GeocodeServer,Locators/Address.GeocodeServer,copied,,,false,Address,,,C:\arcgisserver\directories\arcgissystem\arcgisinput\Locators\Address.GeocodeServer\extracted\p30,,,,,C:\desk\Address.loc,"locator copied to the server at publish time: it is in the server's arcgisinput directory, so rebuilding the source locator does not update this service. Overwrite the service.",,,,
Locators,Streets,GeocodeServer,Locators/Streets.GeocodeServer,ok,,,true,Streets,,,\\fileserver\locators\streets,,,,,C:\desk\Streets.loc,locator read in place: the manifest entry for its folder says byReference true,,,,
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

A service whose own JSON would not answer is not dropped, and it does not pass. Its item ID, its
capabilities and its recycle time are blank, which looks the same as a service with no portal item.
So the row's note says the JSON could not be read, the summary lists the service, and the run
exits 1.

## The CSV guards formula-starting cells

Every text cell of the file is text from the site: a dataset label, a database name, a user, a source
document path, a note. A spreadsheet runs a cell that starts with `=`, `+`, `-`, `@`, a tab or a
carriage return as a formula, and OWASP's CSV injection page lists those characters. So the
writer puts one apostrophe in front of such a cell. This is the line `csv_text` writes when it is given a row with only a hostile dataset label,
database, user, source document and note set. A real run fills the item ID, server, instance,
version and capabilities columns too. `<TAB>` stands for a tab character:

```
,Parcels,FeatureServer,Parcels.FeatureServer,ok,,,,"'=HYPERLINK(""http://x.test"")",,,'@gisdb,'-sdeowner,,,,'+C:/x.mxd,'<TAB>=1+1,,,,
```

- Every column is guarded except `recycle_start` and `recycle_interval`. The tool builds those
  itself as `HH:MM` and whole hours, and they stay numeric.
- A value that already begins with apostrophes and then a trigger gets one more apostrophe, so the
  guard can be undone exactly. `--recycle-with` reads an inventory back through that undo, so a
  database named `@gisdb` in another site's file still groups with `@gisdb` on this site. A
  database that does not start with a trigger is read as it was written.
- The single quote prefix is one of the techniques OWASP lists. OWASP also says that these
  techniques are "not reliable in Microsoft Excel after saving and re-opening the CSV file", and
  that no sanitization strategy is safe for every spreadsheet and every downstream reader. For
  Excel it recommends a different mitigation: a tab character inside the quoted field. This tool
  does not use that, because the tab stays in the data and a script that reads the CSV back would
  have to strip it. This tool also does not claim what Excel shows for the apostrophe: no test
  opens the file in a spreadsheet. The guard makes sure the cell does not start with a trigger
  character. Read the file with a script, and the apostrophe is the only change.
- OWASP also lists a line feed and the full-width forms of `=`, `+`, `-` and `@`, and it says that
  checking only the first character is not enough, because a field separator or a quote inside a
  value can start a new cell. This tool guards those six characters (`=`, `+`, `-`, `@`, tab and CR) and
  nothing else.
- An inventory written by an earlier version has no guard. Reading it back changes a cell only
  when it starts with an apostrophe and then a trigger.

Sources: OWASP, CSV Injection, https://owasp.org/www-community/attacks/CSV_Injection (it now
redirects to https://community.owasp.org/attacks/CSV_Injection). Microsoft's page on stopping Excel
from converting numbers to dates,
https://support.microsoft.com/en-us/office/stop-automatically-changing-numbers-to-dates-452bd2db-cc96-47d1-81e4-72cec11c4ed8,
says an apostrophe typed before an entry is not displayed in the cell. That page describes typing
into a cell, not opening a CSV, so it does not say what Excel shows for an apostrophe inside a CSV.
The guard is tested for what the file holds, not for what a spreadsheet shows.

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

## Which services read this locator: `--locator`

The inventory says which locator each service reads. A rebuild job needs the opposite: it writes
one locator, and it must know which services that reaches. `--locator PATH` turns the table round.
The `.loc` extension on the path is optional, and the comparison ignores case for a Windows path.

| Verdict | What it means |
|---|---|
| `READS` | The service reads this locator in place, so a rebuild reaches it. |
| `COPY` | The service serves a copy, or was published from this file and serves a copy. A rebuild does not reach it. Overwrite the service. |
| `UNKNOWN` | The service names this locator and copy or reference is UNKNOWN, or it is a composite, or its REST resource would not answer. |

A composite locator is UNKNOWN for every target. Esri documents the `locators` property of a
GeocodeServer resource as "names of locators participating in a composite locator". That is a
reference name, chosen when the composite was built. It is not a path. So a participant that has the
same name as your locator is pointed out, and it is not believed. The tool reads `locators` from
`<site>/rest/services/<Folder>/<Name>/GeocodeServer`, one call per geocode service, and writes the
names to the `participants` column.

When nothing matches, the tool does not say that nothing uses the locator, because it compared path
text only. A service that reads the same share under a full host name, a drive letter or a DFS path
does not match. The output then reads `UNSETTLED`, it lists the locator path each geocode service
records, and the run exits 1. A site with no geocode service at all gets a plain answer and exit 0.

A match does not settle the others. When one service reads the locator and an unmatched service
records a locator with the same last name under another path (for example `Z:\locators\streets`
against `\\fileserver\locators\streets`), the output adds an `UNSETTLED` line that names it and
the run exits 1. An unmatched service that records a locator with another name does not change the
answer.

Every `UNKNOWN` verdict and every `UNSETTLED` one exits 1. That is the code a rebuild job should
gate on. `READS` and `COPY` do not change the code on their own: a `COPY` verdict anywhere on the
site still exits 3.

## Recycle skew: `--recycle-with`

Two sites can recycle their services in the same minute against one geodatabase. Every service on
both sites then reconnects together. That is the risk this flag looks for, and it is a heuristic.
It names a geodatabase and a minute, and it does not say that anything will fail.

Run the tool on each site with `--apply`, then run it on one site with `--recycle-with` and the
other sites' CSVs. A risk is one geodatabase on one server, in one minute, with services from two or
more sites. A single site is also a risk when 20 or more of its services share a minute on one
geodatabase. A service that records no start time is counted as unnamed and is left out. It is never
guessed. An interval that divides 24 hours is expanded to every minute it recycles at, wrapping past
midnight.

Two CSVs with the same file name are two sites, told apart by their whole paths. A file that is not
an inventory from this version is refused with exit 2 before any sign-in.

This is a real run against two synthetic sites on `127.0.0.1`. The other site's CSV was written
earlier by this tool. Both sites have the same two services on one geodatabase, recycling at 02:00.
The run also passes `--locator` and `--aprx-dir`. The project folder holds `a\Parcels.aprx`,
`a\Roads.aprx` and `b\Roads.aprx`:

```
services: 4
rows: 4

data sources:
  gisdb                        DBHOST1                     2 row(s)
  C:\arcgisserver\directories\arcgissystem\arcgisinput\Locators\Combined.GeocodeServer\extracted\p30 (none)                      1 row(s)
  \\fileserver\locators\streets (none)                      1 row(s)

portal item IDs: 1 present, 3 missing
locators copied to the server: 1 geocode service(s). Rebuilding the source locator does not update these; overwrite the service.

projects: 1 paired, 1 AMBIGUOUS, 0 not found
  Roads.MapServer: AMBIGUOUS: 2 projects are named roads.aprx (projects\a\Roads.aprx | projects\b\Roads.aprx), and the recorded path matches none of them in full. None was paired

recycle skew risk: 1 geodatabase minute(s) where services recycle together. A risk heuristic, not a diagnosis: stagger their recycle start times.
  gisdb on DBHOST1 at 02:00: 4 service(s) from 2 site(s): 127.0.0.1:18601, services_inventory.csv

locator \\fileserver\locators\streets\Streets.loc:
  READS   Locators/Streets.GeocodeServer: reads it in place, so a rebuild reaches it
  UNKNOWN Locators/Combined.GeocodeServer: a composite locator with participants Streets, Zip. Esri gives each participant's reference name, not its path, so whether one is this locator is UNKNOWN. One participant has this locator's name.

wrote siteA/services_inventory.csv

FAIL: 1 --locator target(s) could not be settled. The tool cannot say that no service reads them.
```

The run exits 1, not 0 and not 4, because the `--locator` target could not be settled for the
composite. The order of precedence is in the exit code table below.

The premise of the check is that a recycle makes a service's instances reconnect to their
geodatabase. That is an inference. Esri documents `recycleInterval` as "the maximum amount of time
(in hours) an instance of the service can run. The default is 24 hours." The pages checked do not
document `recycleStartTime` at all, or the time zone of the value. The tool reads that field only
when it is exactly `HH:MM`, and it compares it as written.

## Pairing services with projects: `--aprx-dir`

A service is paired with the wrong `.aprx` when two projects have the same name. A republish from
the wrong project overwrites the service with the wrong map. So the pairing is made only when it is
safe, and it is refused when it is not:

| What the tool finds | `aprx` | Run |
|---|---|---|
| No project with that file name | `NOT FOUND` | exit 0, unless no service at all was paired: then exit 1 |
| One project with that file name | its path, with a note that the name alone matched | exit 0 |
| Several, and the recorded source path equals exactly one in full | that path | exit 0 |
| Several, and no recorded path equals one of them | `AMBIGUOUS`, nothing paired | exit 4 |

The key is the file name of the source document the manifest recorded, without its extension. When
the manifest recorded none, the key is the service name. A shared parent folder name is not enough:
`Projects` and `Maps` are names half the projects on a share have. A recorded `.mxd` never equals an
`.aprx`, so several projects named like it are `AMBIGUOUS`. A geocode service and a geometry service
read no project, so their `aprx` cell is blank.

The refusal prints both paths and names the cause in the summary and in the `note` column, and no
pairing is written. The folder is walked, never written. A folder under it that cannot be listed
stops the run with exit 2, because a skipped folder can hold the second project that makes a pairing
`AMBIGUOUS`.

A name match is not proof of origin. It tells you which project to open first.

## The sources

These pages are the basis for the Esri behaviour the tool relies on. The recycle, `locators` and
manifest pages were read on 2026-10-13. The other pages were read while the geocode feature was
written, and I did not re-read them for this version.

- Service manifest, `byReference`, `clientName`, `onPremisePath` and `resources.serverPath`:
  <https://developers.arcgis.com/rest/enterprise-administration/server/servicemanifest/>
- Recycle interval, in hours, 24 by default:
  <https://developers.arcgis.com/rest/enterprise-administration/server/createservice/>
- `locators` on a GeocodeServer resource, "names of locators participating in a composite
  locator": <https://developers.arcgis.com/rest/services-reference/enterprise/geocode-service/>
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

No page I could read documents `recycleStartTime`. Open one service in the Admin API with `?f=pjson`
and check that your site returns it before you trust the skew check. Also open one geocode service
with `?f=pjson` and check that `properties.locatorWorkspacePath` is there. `--dump-manifest` shows
whether your site's geocode manifests list the locator under `databases`.

## Why not the tools that already exist

ArcGIS Server Manager shows a service's data source on the service's own page, and it is accurate.
Pro shows it too, in the Share pane and in Analyze, because publishing is what builds the manifest
in the first place. The ArcGIS API for Python reads the service catalog with
`server.services.list()`, which is the right tool for reading one site interactively.

For locators, Pro's analyzer warns at publish time that data which is not registered with the
server will be copied, and Manager lists the registered folders under the site's data stores. Both
are correct. Neither leaves a record you can read a year later, when the nightly rebuild is the
only part anyone remembers.

All three are per service. The gap is the flat file: every service on the site, one row per
dataset, with the database, the host, the version and the item ID in columns you can sort, filter,
diff against the new site after cutover, and hand to somebody who has no Pro licence and no reason
to learn the Python API. Nobody clicks through two hundred services twice.

The statements above about Manager, Pro, the ArcGIS API for Python and the analyzer come from
Esri's documentation. The self-test does not exercise them.

I searched GitHub with `gh search repos` on 2026-10-13 for ArcGIS Server inventory, manifest,
recycle and admin tools. Two public projects are close, and neither does this job.
[arcgis-rest-crawler](https://github.com/erikboderek/arcgis-rest-crawler) walks the public REST
Services Directory and writes folders, services, layers and tables to Markdown. It does well at
mapping what a site publishes, and the public directory does not carry the data source.
[Soccer](https://github.com/AaronPLopez/Soccer) reads service status through the Admin API and
writes a CSV. I have not run either one, and the descriptions are theirs. I found nothing that
reads the manifest, reverses a locator, or compares recycle times across sites.

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
| 0 | Every data source was read, no geocode service serves a copied locator, and every service that can have a portal item has one. |
| 1 | A data source or a service's own JSON could not be read, a `--portal` search did not answer or found no item, a `--locator` target could not be settled, or `--aprx-dir` paired no service at all and found no ambiguity. The inventory is incomplete. |
| 2 | The site could not be read, a local input (`--recycle-with`, `--aprx-dir`) exists but could not be read, or a file could not be written. No inventory is given. A path that does not exist is a usage error, code 64. |
| 3 | Every data source was read, and at least one geocode service serves a copied locator. |
| 4 | Every data source was read, and `--aprx-dir` found a service that two or more projects match. |
| 5 | Every data source was read, and a service of a type that can have a portal item has none in its own record, and no `--portal` search was run. |
| 64 | Usage error. |

When one run has several findings, the order is 2, then 1, then 4, then 3, then 5. An unread source wins,
because the inventory is then incomplete. A refused pairing outranks a copied locator, because the
pairing a republish needs was not made. A site that cannot be read exits 2 and prints no inventory
at all. A partial inventory is worse than none: the services it missed are exactly the ones nobody
remembers, which is why somebody ran an inventory.

## Limits

- It reads one site. A deployment with several federated servers needs one run per server.
- `System` and `Utilities` are walked, and one kind of service in them is left out: a
  `GPServer`, `GeometryServer` or `SymbolServer`. Esri's page for the `Utilities` folder lists
  `GeocodingTools`, `OfflinePackaging`, `PrintingTools` and `RasterUtilities` (GPServer), `Geometry`
  (GeometryServer) and `Symbols` (SymbolServer)
  ([Utilities folder](https://developers.arcgis.com/rest/enterprise-administration/server/utilities-folder/)),
  and those are the services ArcGIS Server publishes for itself. Esri's page does not say what the
  `System` folder holds, so that rule rests on its `GPServer` services such as `PublishingTools`.
  Every other service in those two folders is read like any other, so real data in a folder named
  `Utilities` is inventoried and an unreadable one fails the run. The cost is the reverse case: a
  geoprocessing service of your own that sits in `System` or `Utilities` is left out, and the
  summary does not say so. The match is on the last segment of the folder path, in any case.
- A `GeometryServer` reads nothing, so an absent manifest is the correct answer for it and is
  reported `no-datasource`. Every other type that answers without a manifest is reported
  `unresolved`, which fails the run. That is the safe direction, and it means a cached map service
  whose layers were all copied reads as unresolved. Its `source_document` column still says where
  the source is. A `GeocodeServer` is resolved from its locator path instead, as described above.
- `copied` for a locator means that its folder is under the server's `arcgisinput` directory
  followed by an `extracted` folder, or
  that a manifest entry for its folder says `byReference` false. A locator in a folder that is not
  registered, or is registered under another spelling, reads UNKNOWN, not `copied`. On a site where
  many locators read UNKNOWN, compare their paths with the data stores by hand.
- The `arcgisinput` rule rests on Esri's documented example path for copied data, which is a
  geoprocessing service. No Esri page states the path for a copied locator. A site that stores
  its copies somewhere else reads UNKNOWN. A path spelled with the `${arcgisinput}` variable
  instead of the real directory is not matched either, and reads UNKNOWN.
- A composite locator is always copied, by Esri's design. Its participating locators can still be
  in a registered folder, and Esri's procedure for those is to rebuild them while the service is
  stopped. The tool names the participants but does not locate them, because Esri gives a reference
  name and not a path. For a composite, a `copied` row does not prove that a rebuild never reaches
  it.
- A locator stored in a geodatabase reads UNKNOWN. A connection string does not say whether that
  database was registered, and a match against the registered databases would be a guess.
- The registered-folder search needs an account that may read the site's data items. When the
  site refuses the search, every geocode service that the search would settle reads UNKNOWN and
  the run exits 1. A locator under `arcgisinput` still reads `copied`.
- The locator field names come from Esri's documentation. They have not been checked against
  the service JSON and manifest of a live site. The self-test runs against synthetic services
  built from that documentation, and no real geocode manifest was available.
- The reverse locator index reads the composite participants from `<root>/rest/services/...`,
  where root is the Admin API root. A site whose REST web adaptor differs from its admin one (the
  `--public-rest-root` example above) gets participants UNKNOWN for every geocode service, so
  every `--locator` query lists every geocode service as UNKNOWN.
- A composite locator is detected only when the REST resource lists `locators`. If a real
  single-locator service lists itself there, every geocode service becomes UNKNOWN for every
  `--locator` target. That is noise and not a false clean. Esri only says that `locators` returns
  the participants of a composite.
- `--locator` matches by exact path spelling only. The host name, drive letter, DFS path and mapped
  drive are not reconciled, so a service that reads the same share under another spelling is not
  listed. It is flagged `UNSETTLED` when its locator has the same last name. A service that reads
  the same locator under another spelling and another name is not flagged.
- `--recycle-with` labels a site by the CSV's base name, and by the whole path when two files share
  a base name. If you give the tool its own earlier CSV for the same site, it counts as a second
  site and reports a false cross-site risk.
- The recycle skew crowd threshold of 20 is a guess with no measurement behind it. ArcGIS Server's
  default recycle time is the same for every service. A site with 20 or more services on one
  geodatabase prints the risk line on a plain run, with no other site given.
- A server host written in two spellings in two inventories (a short name against a full host
  name, or an IP address) does not group in the recycle skew check, so the cross-site risk is
  missed. The same holds for a database written two ways. Recycle times are compared as written,
  and the time zone of the start time in the service JSON is not documented. Two sites in different
  time zones are not reconciled.
- Recycle times are modelled for one day. An interval that does not divide 24 hours lands on other
  minutes on later days, which the check ignores. A fractional interval such as 0.5, or a float
  such as 12.0, is read as none, so the service counts as recycling once a day.
- `--aprx-dir` pairs a lone project of the right name on the file name alone, even when its folder
  differs from the recorded path. A recorded `.mxd` pairs with a same-named `.aprx` the same way.
  Both pairings carry a note. Treat any pairing that is not an exact path as a name match.
- `--aprx-dir` walks the whole folder. A very large tree is slow, and one unreadable subfolder
  stops the run with exit 2.
- A service with no matching project reads `NOT FOUND`, and the run still exits 0 when at least
  one other service was paired. When `--aprx-dir` pairs no service at all, the run exits 1 with a
  FAIL line, because a wrong folder looks the same as a site with no projects. A wrong folder that
  still holds one project of the right name pairs that one service and passes. Check the
  `projects:` line.
- The `AMBIGUOUS` and `NOT FOUND` notes put your absolute project paths into the CSV and the
  console, and the paths mix `/` and `\` on Windows. Do not paste that output into a public issue.
- A text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
  apostrophe in the CSV. What a spreadsheet shows for that apostrophe is not tested. A cell that
  starts with a line feed or a full-width `=`, `+`, `-` or `@` is not guarded. See "The CSV guards
  formula-starting cells".
- The guard protects only the first character of a cell. A text cell with a space, or other
  leading white space, before a trigger character is written as it came. A comma-separated payload
  or a quote inside a value is not guarded either.
- A text cell that really starts with an apostrophe and then a trigger, such as a database named
  `'=x` in an inventory written before the guard existed, is read back with one apostrophe removed.
- A folder or service name from the site is percent-encoded as UTF-8 before it is requested. The
  self-test proves that for a stand-in site, and a name with a space, a `#` or a non-ASCII letter is read there. It
  does not prove that every web adaptor decodes such a name the same way. A name that the server
  will not answer for gives a row that reads "service JSON could not be read" and exit 1. A folder
  listing that will not answer stops the run with exit 2.
- The token that `generateToken` returns lasts 60 minutes, as the tool asks. A run longer than that,
  or a site that stops answering part way, ends with exit 1 and unread rows, not exit 2. A
  scheduled job cannot tell that from a data source the tool could not resolve.
- A `--server` or `--portal` URL that carries a `token=` in its query is printed in an error with
  the token value replaced by `***`. One that carries `user:pass@` is refused with exit 64, and
  the refusal does not print it.
- A password typed on the command line is not echoed by the tool, but it is still in your shell
  history and in the process list while the command runs. The tool reads the password only from
  `SVCSOURCE_PASSWORD` or the prompt, so use `SVCSOURCE_PASSWORD`.
- A cell that starts with a byte-order mark and then a trigger, such as `U+FEFF` and `=`, is not
  guarded, because the first character is the mark.
- The end-to-end hostile-site test checks the `dataset`, `database`, `db_user` and
  `source_document` columns through a real run. The `note` and `aprx` columns are pinned only
  through `csv_text`, where every column is pinned for every trigger.
- A failed write under `--apply` exits 2, which is also the code for a site that could not be read.
  A scheduled job cannot tell a full disk from a site outage by the code alone. Read the message.
- With `--apply` and `--dump-manifest`, the two files are replaced one after the other. If the
  second write fails, `--out` has already been replaced, and the run exits 2 with "could not
  write".
- The portal item search puts the folder and service name into a quoted query string with no
  escaping. A name that holds a double quote changes the query and gives a wrong or missing item
  ID. Such a name is rare.
- The secret masks work on the text and on key names. `--dump-manifest` masks only fields and
  keys whose names contain `pass`, `pwd`, `credential`, `token`, `secret`, `apikey`, `api_key`,
  `access_key` (a hyphen also counts) or `accesskey`, and it masks the `user:password@` part of a
  url inside a string. So a password in a url query under another name, under a `PW=` field, or
  under another credential name is written as the server sent it. A `Bearer ...` string is written as sent too. A key named `key`, `pw`, `authorization` or
  `sig`, a `private_key=...`, an Azure-style `AccountKey=...` and a SAS `sig=` are written as sent as well. The text
  before the first `=` of an `AccountKey=KEY==` value stays, because the mask stops at the key
  name.
- The same name-only masking applies to error text. A password in a `--server` query under any
  key other than pass, pwd, token, secret, credential, api key or access key is printed in the
  generateToken error. A `--server` or `--portal` value that is not an http(s) url is not
  printed at all, so a password typed in that position stays out of the log.
- The masks also blank harmless words in error text. `generateToken: 400 Unable to generate token`
  prints as `generateToken: *** Unable to generate token`, because the word after
  `generateToken:` reads as a token value. The HTTP code is lost from that line, and
  `generateToken: <urlopen error ...>` prints with `***` for `<urlopen`.
- A service, folder or database name from the site is printed to the console as sent, so a name
  with terminal escape sequences can change what your terminal shows. The CSV and the dump are not
  affected. Console output is not stripped of control characters.
- An empty catalog, `{"services": [], "folders": []}`, is trusted. It passes with exit 0, and under
  `--apply` it replaces the last good inventory with a header-only file. A real empty site and an
  empty answer from a broken proxy look the same. The self-test pins this as the design.
- `urllib` honours `HTTP_PROXY` and `HTTPS_PROXY` from the environment, and the tool does not
  change that. With an `http://` server and `--insecure`, the password and the token go to the
  proxy in clear text. Proxy settings apply to every call.
- A folder or service name from the site is appended to the server URL as path text. A hostile name
  such as `../../x` can address another path on the same host with the token. No other host is ever
  contacted, because redirects are refused and every URL starts from `--server` or `--portal`.
- `--dump-manifest` is compared with `--out` but not with the `--recycle-with` files. Under
  `--apply`, naming an inventory you want to keep as both replaces it with JSON.
- `recycle_start` and `recycle_interval` are kept safe by validation and not by the apostrophe.
  `rows_for_service` only ever builds `HH:MM` and whole hours, so no run writes a trigger there,
  but `csv_text` called directly with `=1` in `recycle_start` would write it as it came.
- A server's own message text in a `generateToken` failure is echoed to stderr. The password is
  redacted from it. Anything else the server puts there appears as sent.
- A catalog listing that has a `services` array but no `folders` key is read as having no folders,
  so a proxy that strips that key makes the inventory incomplete without a warning. A real
  ArcGIS Server always sends the key.
- The summary counts unread, failed and no-item services by searching the note text, so a
  service name or manifest key that repeats one of those phrases can change the counts.
- A `--recycle-with` file with a cell longer than 128 KB is refused with exit 2.
- Under `--apply` an existing file named `<out>.partial` next to the output is overwritten, even if
  you made it.
- A participant name that contains `|` is split in two in the `participants` column, because `|`
  is the separator.
- The manifest is what the server recorded at publish time. A service republished by hand against
  a different connection, or a registered data store repointed since, is reported as the server
  believes it, not as it is. `--dump-manifest` is there for the cases where that matters.
- A row that reads `ok` says the service records a data source, not that the service can read it.
  A broken data connection still serves metadata, so a service with a dead connection answers its
  REST endpoint with a 200 and draws nothing. Proving a service is healthy means querying a layer
  and getting features back, which is a different tool and a different call. Read an `ok` row as
  "this is the database to move", never as "this service works".
- A manifest can carry the saved password of the connection the service was published from.
  `--dump-manifest` masks every `PASSWORD=`, `PASSWD=`, `PASS=`, `PWD=`, `CREDENTIAL=`,
  `ENCRYPTED_PASSWORD=` and `ENCRYPTED_PASSWORD_UTF8=` field in every string, every `token=`,
  `token:`, `secret=`, `apikey=`, `access_key=` or `password:` value (a quoted value whole, white space after the separator
  allowed, otherwise up to the next `;`, `&`, quote or space), and every value under a key that
  contains `pass`, `pwd`, `credential`, `token`, `secret`, `apikey` or `access_key`. A password that itself contains `;KEY=` leaks its tail after that point. A
  credential under another name is written as the server sent it. Everything else is written as
  sent, hosts and user names included, so share the dump with care.
- The secret redaction is a plain string replace. A password of one or two characters also blanks
  those characters elsewhere in an error message. Error text also has `user:pass@` in a URL and
  any `password=`, `token=`, `secret=` or `apikey=` value masked, whether or not the tool knew that
  credential.
- The token travels in the query string of every GET, as the Admin API accepts, so it can appear
  in web adaptor and proxy access logs. Use https, and treat those logs as sensitive.
- The tool makes two Admin API calls per service, one for the service JSON and one for the
  manifest, and one REST call per geocode service, all in sequence. A site with a thousand services
  takes as long as that sounds. `--timeout` is per call.
- The portal fallback matches on the url the portal item recorded, which is not always the url the
  server reports: a web adaptor, a reverse proxy and a direct `:6443` all spell the same service
  differently. A search the portal answers with no item leaves the ID blank, adds the note
  "portal search found no item", lists the service in the summary and exits 1, because the tool
  cannot tell a service published outside the portal from an item recorded under another spelling
  of the url. A search that finds several items for one service url, or lists fewer hits than its
  total, leaves the ID blank, adds the note "portal search found several items", lists the service
  in the summary and exits 1, because the tool cannot tell which item is the source a republish
  must keep. The search asks for ten hits and takes only a hit whose own `url` equals the url it
  searched, ignoring case and a trailing slash. A search that does not answer, or answers something that is not a result list, adds the note
  "portal search failed" to the row, lists the service in the summary and exits 1. It does not
  stop the run.
- It does not compare two sites. Run it against the old site and the new one and diff the two CSVs
  with whatever you already use. `--recycle-with` compares recycle times only.
  [svcdrift](https://github.com/uhsear/svcdrift) is the tool for comparing a service against the
  dataset behind it.
- It changes nothing, on the server, in the portal or in the folders it reads. The only calls it
  makes are `generateToken`, `admin/services`, the service manifest, `admin/data/findItems`,
  `sharing/rest/search` and `<root>/rest/services/<Folder>/<Name>/GeocodeServer`. The only files it
  writes are `--out` and `--dump-manifest`, and only with `--apply`. Under `--apply` a temporary
  `<path>.partial` file is written next to each output and removed after the replace, and missing
  parent folders of the output are created.
- A cell that is not text is written as it is. A bare negative number is not guarded, which is
  harmless, because a spreadsheet reads it as a number.
- A long locator path pushes the data source summary out of its columns. The CSV is not affected.
- On Windows, the password prompt waits at the console. A scheduled job must set
  `SVCSOURCE_PASSWORD`, or the run waits for a password that nobody types. The prompt reads the
  console and not standard input, so `echo pw | python svcsource.py ...` and `< /dev/null` both
  hang with no output under Git Bash.
- `--insecure` skips certificate verification and exists for sites behind an internal CA. It is
  off by default. Add the CA to your trust store instead.
- The sample run with `--recycle-with`, `--locator` and `--aprx-dir` together is not part of the
  self-test. Each flag is tested on its own.
- An `@` in the path of `--server` or `--portal` is refused as if it were user information. Such a url is unusual, and refusing it is the safe
  direction.
- A secret placed in `--server` as `?key=` or `;jsessionid=` is echoed in a connection error. Only
  `token=`, password forms and `user:pass@` are masked there.
- The masks use regular expressions that can run slowly on a multi-megabyte value made of repeated
  `PASS` or `TOKEN` text with no `=`. No manifest does that.
- On Windows the password comes from `SVCSOURCE_PASSWORD` or the console prompt, never from
  piped stdin. `getpass` reads the console there and ignores a pipe, so a run that pipes the
  password waits. On Linux `getpass` does read piped stdin.
- The guard only keeps a cell from starting with a trigger character. What a spreadsheet displays
  for the apostrophe is not tested.
- The web map and item ID behaviour in the opening story is my own observation. It is not checked
  against Esri documentation, and no page in the Sources section backs it.
- The sample outputs are pasted from one local stand-in run and are not reproducible from the
  repository.
- The `arcgisinput` rule also needs an `extracted` segment after it, on any drive. A locator under
  `arcgisinput` with no `extracted` segment reads UNKNOWN. A path such as
  `D:\arcgisinput\X\extracted\loc.gdb\loc` reads as a copy even when the directory is not the
  server's own, which errs toward review.
- The coverage run was done on Windows with Python 3.13.2. The `coverage` module is not installed
  on the Linux host, so no Linux coverage figure is claimed.
- Masking in error text is partial in four rare cases. An unquoted value with a space after a
  `token`, `api_key`, `password` or `credential` key keeps the text after the space. A quoted
  secret that holds an escaped quote keeps the text after that quote. A password that holds `;word=`
  keeps its tail. An unquoted token that holds a quote keeps the text after the quote. The tool
  masks its own password and token exactly, so these only matter for a secret it did not hold.
- The secret masks use backtracking patterns. A hostile server message packed with `://` makes
  error masking slow, about 12 seconds for 80 KB. A normal message is instant.
- `--dump-manifest` keys each manifest by the service path. A service whose path holds `pass`,
  `pwd`, `token`, `secret`, `credential`, `api key` or `access key`, such as `Compass`, has its whole
  manifest written as `***`. That errs toward hiding.
- The credential-free CSV claim rests on `parse_conn` keeping only five connection-string keys. Text
  in `dataset`, `source_document`, `database` or `note` is written as the site sent it. The
  password and token masks run on the dump and on error text only, so a password that a site puts
  in one of those fields would reach the CSV.
- `--public-rest-root` is not checked for a user name or password in its address. Its text goes
  into the portal search query, so a password typed there reaches the portal and is never masked.
- `.gitignore` does not list `services_inventory.csv` or a `--dump-manifest` file. Running the tool
  inside a clone and then `git add .` can commit an inventory with real host, database and account
  names.
- `--timeout` is a socket timeout for each blocking read, not a limit on a whole call. A server that
  sends one byte at a time can hold a call open, and a response is read with no size cap.
- If stdout is closed after the files are written, as in `| head -1`, Python exits 120 with
  `Exception ignored on flushing sys.stdout`. That code is not one of the documented exit codes.
  The CSV is already on disk.
- On Windows, a run with the inventory open in Excel makes `os.replace` fail. The run exits 2 with
  `PermissionError` and keeps the old file, the same code as an unreadable site.
- The sample outputs come from a stand-in site that is not shipped. The self-test's own geocode
  site has no REST resource, so its CSV rows carry `participants=UNKNOWN`, while the README rows
  show it blank.
- The Esri pages in "The sources" load (HTTP 200). Only the recycle, locators and manifest pages
  were re-read on 2026-10-13. The statements about Manager, Pro, the ArcGIS API for Python and the
  analyzers, and the two GitHub projects, are not exercised by the self-test. The GitHub search is
  dated 2026-10-13 and may be stale.
- A connection-refused error prints as `generateToken: *** error [WinError 10061] ...>`, because
  the mask blanks `<urlopen`. No credential shows, and the exit code is 2 with no file written.
- The instance-string forms for SQL Server and Oracle (`HOST\INSTANCE`, `HOST,port` and Oracle Easy
  Connect) are read from observed connection strings, and no vendor page is cited for them.
- A `--recycle-with` file with a header and no rows, or whose rows have no server, prints the
  no-risk skew line although nothing was compared. When its rows carry a server and no
  `recycle_start`, a second line counts the services that were not compared. The recycle result is
  not part of the exit code.
- A `--recycle-with` or `--aprx-dir` path that is missing is a usage error with exit 64. One that
  exists but cannot be read exits 2.
- The CSV is UTF-8 without a byte-order mark. Import it into a spreadsheet as UTF-8 if names hold
  non-ASCII letters.
- Eleven self-test assertions of the first published version (commit 8285f8d, 602 assertions) are
  gone, because the behaviour they pinned changed in later review rounds: which folders are
  walked, a failed portal search (now exit 1, not a blank ID), an unmatched project, and the
  folder skip list. Each has a replacement. Two credential labels were reworded, not removed.
- The self-test removes its temporary directory only when it runs to the end. An exception inside
  it leaves a `svcsource-selftest-*` folder under the system temp directory.
- Under `--apply`, an exit 1 run (unread sources) still replaces the last good `--out` file with
  the incomplete inventory. Only exit 2 leaves the old file untouched. Keep a copy of the earlier
  file.
- A per-service JSON answer of `{}` is a valid object with no `serviceName`. It is read as a
  service with no portal item and no note, so a degenerate proxy answer gives a quiet row, not
  "service JSON could not be read". Without `--portal` the run then exits 5 for that service.
- A portal item search hit that has no `url` field is kept. With a service name that changes the
  query, such a hit could be reported as the source item ID. ArcGIS Server service names cannot
  hold a quote, so this is rare.
- The masks do not hide a bare `--password` followed by a value that starts with a dash, as in
  `run.exe --password -Abc123`. The tool never echoes its own arguments, so its output does not
  reach this case.
- `--self-test` writes temporary files under the system temp directory and removes them. It
  writes nothing in the repository.
- A service whose own `portalProperties` list no item, run without `--portal`, gets a blank
  `source_item_id` and no note in its row. The run exits 5, the last line says `NO ITEM IDS`, and
  the summary lists the service. An unfederated site looks like this for every service, so it
  always exits 5 without `--portal`; with `--portal` a service the search does not find exits 1.
  A `GeometryServer`, and a service type with no portal item type, are not counted. A service
  type with no portal item and a `--portal` search that answers is the existing exit 1.
- When stdout is closed, the exit code and the message depend on the platform. On Linux Python
  exits 120. On Windows, `--self-test | head -1` ended with an `OSError` traceback (Errno 22) and
  exit 1. No self-test assertion exercises a closed stdout.
- The CSV is not scanned for secrets. It stays free of credentials only because the connection
  string is cut down to five keys. A password that a site puts in `dataset`, `source_document`,
  `database` or `note` is written as it came.
- The TLS default is pinned by reading the attributes of the SSL context that the tool builds. No
  HTTPS handshake against a self-signed stub is run, so the certificate check itself is not
  exercised end to end.
- A `--recycle-with` CSV with rows shorter than its header is read without a complaint. Those rows
  join the recycle check with blank fields. The tool prints no count of the sites or rows it
  compared, so check the file yourself when it may have been cut short.
- The temporary file under `--apply` has the fixed name `<out>.partial`. In a shared folder, another
  user can plant a symlink at that name and the write follows it. Write `--out` into a folder that
  only you can write to.
- A bad path given to `--recycle-with` or `--aprx-dir` is printed as typed in the usage error. A
  password typed into one of those flags by mistake appears in that line.
- `pyflakes` was run on Windows only, and it is not installed on the Linux host. The module
  compiles under `python -W error` on both.
- The error-text mask can leave part of a password that holds both an apostrophe and a double quote
  and appears in the `repr()` form, such as inside a printed dict. No call site in the tool builds
  that text today.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools by the same author that pair with this one:

- [svcdrift](https://github.com/uhsear/svcdrift) - the schema half: what a service was published with against what the dataset holds now
- [ghostsvc](https://github.com/uhsear/ghostsvc) - the services on the site that no portal item accounts for
- [itemcensus](https://github.com/uhsear/itemcensus) - the portal items on the other side of the item ID
