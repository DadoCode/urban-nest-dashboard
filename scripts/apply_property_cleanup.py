"""Apply the reviewed property clean-up to a dashboard database, safely.

    .venv/bin/python scripts/apply_property_cleanup.py --db PATH            # dry run: report only, writes nothing
    .venv/bin/python scripts/apply_property_cleanup.py --db PATH --apply    # backup first, then migrate + clean up

What it does (all of it undoable from Documents -> Import monthly workbook -> the "cleanup" batch):
  * full display names (ids and every record untouched; old names kept as aliases)
  * Flat 3 NW4 -> operated (rent-to-rent)
  * removes ONLY the business-cost rows proven to be a duplicate of a property's own monthly costs

--apply first writes a timestamped backup next to the database (or into --backup-dir), prints the SHA-256 of the
database before anything changes and the backup's path, verifies the backup against the original (integrity check and
row counts), then runs the additive schema migration and the clean-up in one transaction. There is no default
database: --db is required, so nothing runs against the real data by accident.
"""
import argparse
import datetime
import hashlib
import os
import sqlite3
import sys
from pathlib import Path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def counts(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
        return {t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in tables}
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to the dashboard database")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--backup-dir", help="where to put the backup (default: next to the database)")
    args = ap.parse_args()
    path = Path(args.db).resolve()
    if not path.exists():
        sys.exit(f"No such database: {path}")
    os.environ["DASHBOARD_DB_PATH"] = str(path)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

    before_hash = sha256(path)
    print(f"database : {path}\nsha256   : {before_hash}\nmode     : {'APPLY' if args.apply else 'dry run (nothing is written)'}")

    if not args.apply:
        # read-only look: work on a throw-away copy so even the additive migration is not run against the real file
        import shutil
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / "preview.db"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        src.close()
        dst.close()
        os.environ["DASHBOARD_DB_PATH"] = str(tmp)
    import db
    if not args.apply:
        db.ensure_schema()
    from services.workbook import cleanup as K

    if args.apply:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else path.parent
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{path.stem}-backup-{stamp}.db"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        dst = sqlite3.connect(backup)
        src.backup(dst)
        src.close()
        dst.close()
        ok = sqlite3.connect(backup).execute("pragma integrity_check").fetchone()[0]
        same = counts(path) == counts(backup)
        print(f"backup   : {backup}\n           sha256 {sha256(backup)} | integrity {ok} | row counts identical to the original: {same}")
        if ok != "ok" or not same:
            sys.exit("The backup did not verify. Nothing was changed.")
        db.ensure_schema()                                   # additive migration only

    conn = db.get_conn()
    print("\nPROPERTY NAME TABLE")
    for r in K.mapping_table(conn):
        print(f"  {r['current_name']:<26}-> {r['canonical']:<22} {r['code']:<11} {r['sheet']:<13} {r['model']:<20} {r['confidence']:<7}"
              + (f" ?? {r['question']}" if r["question"] else ""))
    actions = K.plan_cleanup(conn)
    print(f"\nrenames   : {[(a['from'], a['to']) for a in actions['renames']]}")
    print(f"model     : {[(a['property_id'], a['from'], '->', a['to']) for a in actions['models']]}")
    print(f"duplicates: {len(actions['duplicates'])} business rows, total {sum(d['row']['amount'] for d in actions['duplicates']):.2f}")
    for d in actions["duplicates"]:
        print(f"    {d['row']['date']} {d['row']['description']} {d['row']['amount']:.2f} = {d['property_id']} costs {d['property_total']:.2f}")
    print(f"left alone: {len(actions['left_alone'])} business rows that name a property but do not match its costs")
    if args.apply:
        batch = K.apply_cleanup(conn, actions, "script")
        print(f"\napplied as batch #{batch} (undo it from the Import page)")
        print(f"sha256 after: {sha256(path)}\nbackup to restore from: {backup}")
    else:
        print("\nDry run only. Re-run with --apply to back up and make these changes.")


if __name__ == "__main__":
    main()
