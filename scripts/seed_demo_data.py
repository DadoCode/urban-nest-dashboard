"""Seeds the demo copy's database with realistic, entirely fake data.
Run with the project's venv: python3 scripts/seed_demo_data.py
Safe to re-run -- wipes and rebuilds this copy's own data/dashboard.db only."""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import db  # noqa: E402

random.seed(7)

DB_PATH = db.DB_PATH
if DB_PATH.exists():
    DB_PATH.unlink()

db.ensure_schema()
conn = db.get_conn()

PROPERTIES = [
    ("flat-riverside-1", "RIV1", "12 Riverside Court", "12 Riverside Court, London SE1", "flat", "2024-03-01"),
    ("flat-riverside-2", "RIV2", "14 Riverside Court", "14 Riverside Court, London SE1", "flat", "2024-03-01"),
    ("flat-maple-house", "MPL1", "5 Maple House", "5 Maple House, London E2", "flat", "2024-06-01"),
    ("flat-canal-view", "CNL1", "22 Canal View", "22 Canal View, London N1", "flat", "2024-09-01"),
    ("flat-kings-cross", "KGX1", "8 Kings Cross Apartments", "8 Kings Cross Apartments, London N1C", "flat", "2025-01-01"),
    ("overhead-shared", "OVH", "Shared Overheads", "Portfolio-wide overheads", "overhead", "2024-03-01"),
]

for pid, code, name, address, ptype, start in PROPERTIES:
    conn.execute(
        """INSERT INTO properties (id, code, name, address, active, type, start_date, created_at)
           VALUES (?,?,?,?,1,?,?,datetime('now'))""",
        (pid, code, name, address, ptype, start),
    )

PLATFORMS = ["airbnb", "booking_com", "direct"]
today = date(2026, 9, 1)


def months_back(n):
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


flats = [p for p in PROPERTIES if p[4] == "flat"]

for pid, code, name, address, ptype, start in flats:
    base_rate = random.uniform(85, 160)
    for (y, m) in months_back(14):
        start_dt = date(y, m, 1)
        if start_dt < date(*map(int, start.split("-")[:2]), 1):
            continue
        days_in_month = (date(y + (m == 12), (m % 12) + 1, 1) - start_dt).days
        n_bookings = random.randint(2, 5)
        day_cursor = 1
        month_occupancy_boost = random.uniform(0.55, 0.9)
        for _ in range(n_bookings):
            if day_cursor >= days_in_month - 1:
                break
            stay = random.randint(2, 7)
            gap = random.randint(0, 3)
            check_in = start_dt + timedelta(days=day_cursor + gap - 1)
            check_out = check_in + timedelta(days=stay)
            if (check_out - start_dt).days > days_in_month:
                check_out = start_dt + timedelta(days=days_in_month)
            nights = max((check_out - check_in).days, 1)
            rate = base_rate * random.uniform(0.85, 1.25) * month_occupancy_boost
            gross = round(rate * nights, 2)
            fees = round(gross * 0.03, 2)
            cleaning = round(random.uniform(45, 85), 2)
            net = round(gross - fees, 2)
            conn.execute(
                """INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out,
                       gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source)
                   VALUES (?,?,?,?,?,?,?,?,?, 'confirmed', 'demo_seed')""",
                (pid, random.choice(PLATFORMS), f"DEMO-{pid[:3].upper()}-{y}{m:02d}-{_}",
                 check_in.isoformat(), check_out.isoformat(), gross, fees, cleaning, net),
            )
            day_cursor += stay + gap

        for cat, lo, hi, capex in [
            ("cleaning", 120, 320, 0), ("utilities", 60, 140, 0),
            ("maintenance", 0, 260, 0), ("management_fee", 80, 220, 0),
        ]:
            if cat == "maintenance" and random.random() < 0.5:
                continue
            amt = round(random.uniform(lo, hi), 2)
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, description, amount,
                       direction, category, capex, source)
                   VALUES (?,?,?,?,?, 'expense', ?, ?, 'demo_seed')""",
                (pid, f"{y}-{m:02d}-05", cat.title().replace("_", " ") + " Co", f"{cat} for {y}-{m:02d}",
                 amt, cat, capex),
            )
        if random.random() < 0.25:
            amt = round(random.uniform(300, 1400), 2)
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, description, amount,
                       direction, category, capex, source)
                   VALUES (?,?,?,?,?, 'expense', 'furniture', 1, 'demo_seed')""",
                (pid, f"{y}-{m:02d}-15", "Furnish Co", "Replacement furniture/appliance", amt),
            )

        conn.execute(
            """INSERT OR REPLACE INTO targets (property_id, year, month, revenue_target, profit_target, occupancy_target, source)
               VALUES (?,?,?,?,?,?, 'demo_seed')""",
            (pid, y, m, round(base_rate * days_in_month * 0.7, 0), round(base_rate * days_in_month * 0.4, 0), 70),
        )

overhead = PROPERTIES[-1]
for (y, m) in months_back(14):
    for cat, lo, hi in [("insurance", 80, 150), ("software", 40, 90), ("accounting", 100, 250), ("marketing", 0, 200)]:
        if random.random() < 0.15:
            continue
        amt = round(random.uniform(lo, hi), 2)
        conn.execute(
            """INSERT INTO transactions (property_id, date, vendor, description, amount,
                   direction, category, capex, source)
               VALUES (?,?,?,?,?, 'expense', ?, 0, 'demo_seed')""",
            (overhead[0], f"{y}-{m:02d}-03", cat.title() + " Ltd", f"{cat} for {y}-{m:02d}", amt, cat),
        )

conn.commit()
n_bookings = conn.execute("SELECT COUNT(*) AS n FROM bookings").fetchone()["n"]
n_tx = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
print(f"Seeded demo DB at {DB_PATH}: {n_bookings} bookings, {n_tx} transactions, {len(PROPERTIES)} properties.")
