import os
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import pandas as pd
from rapidfuzz import fuzz, process


@dataclass(frozen=True)
class Employee:
    fio: str
    phone_e164: str
    email: str


def normalize_phone_to_e164_ru(raw: str) -> Optional[str]:
    """
    Приводим телефон к виду +7XXXXXXXXXX.
    В Excel номера могут быть в формате: 8..., +7..., 7..., с пробелами/скобками/дефисами.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D+", "", s)
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+7{digits}"
    return None


def _col(env_name: str, fallback: str) -> str:
    return os.environ.get(env_name, fallback).strip()


def load_employees() -> Dict[str, Employee]:
    """
    Загружаем сотрудников из Excel.
    Возвращаем dict по phone_e164 -> Employee.
    """
    path = os.environ.get("EMPLOYEES_XLSX_PATH", "data/Список сотрудников.xlsx")
    fio_col = _col("EMP_COL_FIO", "ФИО")
    phone_col = _col("EMP_COL_PHONE", "Телефон")
    email_col = _col("EMP_COL_EMAIL", "Email")

    df = pd.read_excel(path)
    for c in (fio_col, phone_col, email_col):
        if c not in df.columns:
            raise RuntimeError(f"Excel column '{c}' not found. Existing columns: {list(df.columns)}")

    result: Dict[str, Employee] = {}
    for _, row in df.iterrows():
        fio = str(row.get(fio_col, "")).strip()
        phone = normalize_phone_to_e164_ru(row.get(phone_col))
        email = str(row.get(email_col, "")).strip().lower()
        if not fio or not phone or not email:
            continue
        result[phone] = Employee(fio=fio, phone_e164=phone, email=email)
    return result


def build_fio_index(employees_by_phone: Dict[str, Employee]) -> Dict[str, Employee]:
    """
    Для поиска по ФИО: fio_lower -> Employee (последнее значение выигрывает).
    """
    idx: Dict[str, Employee] = {}
    for emp in employees_by_phone.values():
        idx[emp.fio.lower()] = emp
    return idx


def find_employee_by_fio(
    fio_query: str,
    fio_index: Dict[str, Employee],
    *,
    score_cutoff: int = 80,
) -> Optional[Tuple[Employee, int]]:
    q = (fio_query or "").strip().lower()
    if not q:
        return None
    if q in fio_index:
        return fio_index[q], 100
    choices = list(fio_index.keys())
    match = process.extractOne(q, choices, scorer=fuzz.WRatio, score_cutoff=score_cutoff)
    if not match:
        return None
    key, score, _ = match
    return fio_index[key], int(score)

