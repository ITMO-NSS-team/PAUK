# Server storage layout (einsteinium)

Where PAUK keeps its files on `einsteinium.nsslab`: the small local disk for
what must not sit on a network share, `/var/essdata` (NFS) for everything
bulky. Prose description and the reasons:
[`../architecture/deploy.md`](../architecture/deploy.md#storage-layout).

Solid boxes exist today. Dashed boxes are planned and not moved yet.

```mermaid
flowchart TB
    subgraph LOCAL["local disk (ext4, /home/asteb/pauk): databases, secrets, personal data"]
        direction TB
        SECRETS["secrets/<br/>.env files and scripts with passwords<br/>dir 700, files 600"]
        SCRATCH["scratch/&lt;source&gt;-&lt;date&gt;/<br/>one-off scripts and logs,<br/>kept until colleagues decide"]
        CODE["code/<br/>the repository (now ~/PAUK)"]:::planned
        DB["db/mongo/, db/neo4j/<br/>database data, bind mounts<br/>(now ~/pauk-mongo-data, /var/lib/neo4j)"]:::planned
        PRIVATE["private/<br/>russian_names.csv<br/>(now ~/PAUK/data/static)"]:::planned
    end

    subgraph ESS["/var/essdata/pauk (NFS4 on GPFS): bulky, no secrets"]
        direction TB
        DUMPS["dumps/&lt;stand&gt;/&lt;date&gt;/<br/>database dumps: itmo, hse"]
        PDF["pdf/&lt;stand&gt;/<br/>publication PDFs, only real %PDF files"]
        RUNS["runs/&lt;stand&gt;/&lt;group&gt;/<br/>run logs, audit/ inside"]
        SNAP["snapshots/&lt;stand&gt;/&lt;date&gt;/<br/>graph snapshots (data/cache)"]
        REL["releases/&lt;stand&gt;/<br/>built site versions"]
        REVIEW["to-review/<br/>leftovers awaiting a decision"]
    end

    PIPE["pipeline<br/>~/PAUK/data/pdf (symlink)"] --> PDF

    classDef planned stroke-dasharray: 5 5
```

`<stand>` is a deployment of PAUK: `itmo` is the main one, `hse` a conserved
demo. A new stand gets its own subfolder in each type folder.
