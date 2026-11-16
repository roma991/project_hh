#!/usr/bin/env python3
"""
Отчёт по договорам: по каждой работе — цена, выплачено, осталось оплатить;
по каждому договору — итоги. На заданную дату.

Запуск:
    python report.py --data data --date 2026-11-10
    python report.py --data data --date 2026-11-10 --out report.csv
"""
import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

CENT = Decimal("0.01")


def money(s: str) -> Decimal:
    s = (s or "").strip()
    if not s:
        return Decimal("0.00")
    return Decimal(s).quantize(CENT, rounding=ROUND_HALF_UP)


def pdate(s: str) -> Optional[date]:
    s = (s or "").strip()
    return date.fromisoformat(s) if s else None


@dataclass
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date


@dataclass
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: Optional[date]
    cancelled_at: Optional[date]


@dataclass
class Payment:
    payment_id: str
    contract_id: str
    work_id: Optional[str]
    purpose: str
    amount: Decimal
    status: str
    created_at: Optional[date]
    paid_at: Optional[date]
    refunded_at: Optional[date]


def read_csv(path: Path) -> list:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load(data_dir: Path):
    contracts = [
        Contract(
            r["contract_id"].strip(),
            r["contractor"].strip(),
            money(r["amount"]),
            pdate(r["signed_at"]),
        )
        for r in read_csv(data_dir / "contracts.csv")
    ]
    works = [
        Work(
            r["work_id"].strip(),
            r["contract_id"].strip(),
            r["title"].strip(),
            money(r["price"]),
            r["status"].strip(),
            pdate(r["completed_at"]),
            pdate(r["cancelled_at"]),
        )
        for r in read_csv(data_dir / "works.csv")
    ]
    payments = [
        Payment(
            r["payment_id"].strip(),
            r["contract_id"].strip(),
            (r["work_id"].strip() or None),
            r["purpose"].strip(),
            money(r["amount"]),
            r["status"].strip(),
            pdate(r["created_at"]),
            pdate(r["paid_at"]),
            pdate(r["refunded_at"]),
        )
        for r in read_csv(data_dir / "payments.csv")
    ]
    return contracts, works, payments


def effective_on(p: Payment, as_of: date) -> bool:
    if p.status == "queued":
        return False
    if p.paid_at is None or p.paid_at > as_of:
        return False
    if p.status == "refunded":
        if p.refunded_at is not None and p.refunded_at <= as_of:
            return False
    return True


def work_status_on(w: Work, as_of: date) -> str:
    if w.status == "cancelled":
        return "cancelled"
    if w.status == "done":
        if w.completed_at is not None and w.completed_at <= as_of:
            return "done"
        return "planned"
    return "planned"


@dataclass
class WorkRow:
    work: Work
    status_on: str
    paid_direct: Decimal = Decimal("0.00")
    paid_fifo: Decimal = Decimal("0.00")

    @property
    def paid(self) -> Decimal:
        return self.paid_direct + self.paid_fifo

    @property
    def price_effective(self) -> Decimal:
        return Decimal("0.00") if self.status_on == "cancelled" else self.work.price

    @property
    def remaining(self) -> Decimal:
        r = self.price_effective - self.paid
        return r if r > 0 else Decimal("0.00")

    @property
    def overpay(self) -> Decimal:
        r = self.paid - self.price_effective
        return r if r > 0 else Decimal("0.00")


@dataclass
class ContractRow:
    contract: Contract
    works: list = field(default_factory=list)
    paid_total: Decimal = Decimal("0.00")
    unallocated: Decimal = Decimal("0.00")

    @property
    def remaining(self) -> Decimal:
        r = self.contract.amount - self.paid_total
        return r if r > 0 else Decimal("0.00")

    @property
    def overpay(self) -> Decimal:
        r = self.paid_total - self.contract.amount
        return r if r > 0 else Decimal("0.00")


def build(contracts, works, payments, as_of: date):
    c_by_id = {c.contract_id: c for c in contracts}
    w_by_id = {w.work_id: w for w in works}
    effective = [p for p in payments if effective_on(p, as_of)]

    direct = {}
    pool = {}
    for p in effective:
        if p.contract_id not in c_by_id:
            print(f"WARN: платёж {p.payment_id}: неизвестный договор {p.contract_id}", file=sys.stderr)
            continue
        if p.work_id:
            if p.work_id not in w_by_id:
                print(f"WARN: платёж {p.payment_id}: неизвестная работа {p.work_id} -> в пул договора", file=sys.stderr)
                pool[p.contract_id] = pool.get(p.contract_id, Decimal("0.00")) + p.amount
            else:
                direct[p.work_id] = direct.get(p.work_id, Decimal("0.00")) + p.amount
        else:
            pool[p.contract_id] = pool.get(p.contract_id, Decimal("0.00")) + p.amount

    rows = []
    for c in contracts:
        crow = ContractRow(contract=c)
        c_works = [w for w in works if w.contract_id == c.contract_id]

        for w in c_works:
            st = work_status_on(w, as_of)
            wr = WorkRow(work=w, status_on=st, paid_direct=direct.get(w.work_id, Decimal("0.00")))
            crow.works.append(wr)

        free = pool.get(c.contract_id, Decimal("0.00"))

        def sort_key(wr):
            rank = {"done": 0, "planned": 1, "cancelled": 2}.get(wr.status_on, 3)
            return (rank, wr.work.completed_at or date.max, wr.work.work_id)

        for wr in sorted(crow.works, key=sort_key):
            if wr.status_on == "cancelled":
                continue
            if free <= 0:
                break
            # Учитываем и прямые выплаты, и уже зачтённое из пула
            gap = wr.work.price - wr.paid_direct - wr.paid_fifo
            if gap <= 0:
                continue
            take = gap if gap < free else free
            wr.paid_fifo = wr.paid_fifo + take
            free -= take

        crow.paid_total = sum((wr.paid for wr in crow.works), Decimal("0.00")) + free
        crow.unallocated = free
        rows.append(crow)
    return rows


def fmt(d: Decimal) -> str:
    return f"{d.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def print_report(rows, as_of: date):
    print(f"Отчёт на {as_of.isoformat()}")
    print("=" * 100)
    for cr in rows:
        c = cr.contract
        print(f"\nДоговор {c.contract_id} — {c.contractor}  (сумма {fmt(c.amount)})")
        print("-" * 100)
        print(f"{'Работа':<8}{'Статус':<12}{'Цена':>14}{'Выплачено':>14}{'Осталось':>14}  Название")
        for wr in cr.works:
            extra = f"   [переплата {fmt(wr.overpay)}]" if wr.overpay > 0 else ""
            print(
                f"{wr.work.work_id:<8}{wr.status_on:<12}"
                f"{fmt(wr.price_effective):>14}{fmt(wr.paid):>14}{fmt(wr.remaining):>14}"
                f"  {wr.work.title}{extra}"
            )
        print(f"{'ИТОГО':<20}{'':<0}{fmt(c.amount):>14}{fmt(cr.paid_total):>14}{fmt(cr.remaining):>14}")


def write_csv(rows, as_of: date, path: Path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["date", as_of.isoformat()])
        w.writerow([])
        w.writerow(["contract_id", "contractor", "work_id", "title", "status_on",
                    "price", "paid", "remaining", "overpay"])
        for cr in rows:
            c = cr.contract
            for wr in cr.works:
                w.writerow([c.contract_id, c.contractor, wr.work.work_id, wr.work.title,
                            wr.status_on, fmt(wr.price_effective), fmt(wr.paid),
                            fmt(wr.remaining), fmt(wr.overpay)])
            w.writerow([c.contract_id, c.contractor, "ИТОГО", "", "",
                        fmt(c.amount), fmt(cr.paid_total), fmt(cr.remaining), fmt(cr.overpay)])
            w.writerow([])


def main():
    ap = argparse.ArgumentParser(description="Отчёт по выплатам на дату")
    ap.add_argument("--data", default="data", type=Path)
    ap.add_argument("--date", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    as_of = date.fromisoformat(args.date)
    contracts, works, payments = load(args.data)
    rows = build(contracts, works, payments, as_of)

    if args.out:
        write_csv(rows, as_of, args.out)
        print(f"Записано: {args.out}", file=sys.stderr)
    else:
        print_report(rows, as_of)


if __name__ == "__main__":
    main()