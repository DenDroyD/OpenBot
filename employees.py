import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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
    """
    Умный поиск сотрудника по ФИО.
    1. Сначала пробуем точное совпадение
    2. Если введено 2+ слова - ищем по фамилии (первое слово) + имя
    3. Используем fuzzy search с приоритетом на совпадение фамилии
    Возвращает список всех найденных совпадений при неоднозначности.
    """
    q = (fio_query or "").strip().lower()
    if not q:
        return None
    
    # Точное совпадение
    if q in fio_index:
        return fio_index[q], 100
    
    choices = list(fio_index.keys())
    
    # Если введено несколько слов, пробуем найти по комбинации
    parts = q.split()
    if len(parts) >= 2:
        # Ищем по фамилии + имени (первые два слова)
        short_query = f"{parts[0]} {parts[1]}"
        if short_query in fio_index:
            return fio_index[short_query], 100
        
        # Ищем по фамилии (первое слово) - наиболее важный критерий
        surname = parts[0]
        surname_matches = [c for c in choices if c.startswith(surname)]
        
        if surname_matches:
            # Среди совпадений по фамилии ищем лучшее по полному запросу
            best_match = process.extractOne(q, surname_matches, scorer=fuzz.WRatio, score_cutoff=score_cutoff)
            if best_match:
                key, score, _ = best_match
                return fio_index[key], int(score)
            
            # Если не нашли по полному запросу, берем первое совпадение по фамилии
            # Но только если фамилия совпадает точно или очень близко
            for candidate in surname_matches:
                cand_parts = candidate.split()
                if len(cand_parts) > 0 and cand_parts[0] == surname:
                    return fio_index[candidate], 95  # Высокий скор за точное совпадение фамилии
    
    # Стандартный fuzzy поиск по всем ФИО
    match = process.extractOne(q, choices, scorer=fuzz.WRatio, score_cutoff=score_cutoff)
    if not match:
        return None
    key, score, _ = match
    return fio_index[key], int(score)


def find_employees_by_fio_multiple(
    fio_query: str,
    fio_index: Dict[str, Employee],
    *,
    limit: int = 5,
    score_cutoff: int = 60,
) -> List[Tuple[Employee, int]]:
    """
    Поиск нескольких сотрудников по ФИО для выбора пользователем.
    Возвращает список кортежей (Employee, score).
    """
    from rapidfuzz import process, fuzz
    
    q = (fio_query or "").strip().lower()
    if not q:
        return []
    
    choices = list(fio_index.keys())
    parts = q.split()
    
    # Если введено 2+ слова, приоритезируем поиск по фамилии
    if len(parts) >= 2:
        surname = parts[0]
        surname_matches = [c for c in choices if c.startswith(surname)]
        
        if surname_matches:
            # Ищем среди совпадений по фамилии
            matches = process.extract(q, surname_matches, scorer=fuzz.WRatio, score_cutoff=score_cutoff, limit=limit)
            return [(fio_index[key], int(score)) for key, score, _ in matches]
    
    # Общий поиск
    matches = process.extract(q, choices, scorer=fuzz.WRatio, score_cutoff=score_cutoff, limit=limit)
    return [(fio_index[key], int(score)) for key, score, _ in matches]

