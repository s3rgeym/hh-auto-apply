#!/usr/bin/env python
import argparse
import http.cookiejar
import json
import logging
import logging.handlers
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from functools import partial
from itertools import count
from pathlib import Path
from typing import Any, Iterator, TypedDict
from urllib.parse import parse_qs, urljoin, urlparse

import requests

CURDIR = Path(__file__).parent.resolve()

print = partial(print, flush=True)


class ColorFormatter(logging.Formatter):
    RED = "\x1b[31;20m"
    GREEN = "\x1b[32;20m"
    YELLOW = "\x1b[33;20m"
    BLUE = "\x1b[34;20m"
    PURPLE = "\x1b[35;20m"
    CYAN = "\x1b[36;20m"
    LIGHT_GREY = "\x1b[37;20m"
    GREY = "\x1b[38;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"
    FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

    LOG_COLORS = {
        logging.DEBUG: LIGHT_GREY,
        logging.INFO: GREEN,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED,
    }

    def __init__(self) -> None:
        super().__init__()
        self._formatters = {
            level: logging.Formatter(f"{color}{self.FORMAT}{self.RESET}")
            for level, color in self.LOG_COLORS.items()
        }

    def format(self, record):
        if formatter := self._formatters.get(record.levelno):
            return formatter.format(record)
        return super().format(record)


logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter())
logger.addHandler(handler)
logger.setLevel(logging.INFO)

VacancyData = TypedDict(
    "VacancyData",
    {
        "vacancyId": int,
        "name": str,
        "@workSchedule": str,
        "links": dict[str, str],
        "totalResponsesCount": int,
        "area": dict[str, Any],
        "company": dict[str, Any],
        "compensation": dict[str, Any],
        "creationTime": str,
        "lastChangeTime": dict[str, Any],
        "userLabels": list[str],
        "@responseLetterRequired": bool,
        "userTestPresent": bool,
    },
)


class Database:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.create_schema()

    def create_schema(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    work_schedule TEXT,
                    url TEXT,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    city TEXT,
                    company_id INTEGER,
                    company_name TEXT,
                    company_url TEXT,
                    salary_from INTEGER,
                    salary_to INTEGER,
                    salary_currency TEXT,
                    responses_count INTEGER,
                    applied_at TIMESTAMP
                )
            """)

    def save_application(self, v: VacancyData):
        comp = v.get("compensation") or {}
        company = v.get("company") or {}

        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO applications
                (id, name, work_schedule, url, created_at, updated_at, city, company_id,
                 company_name, company_url, salary_from, salary_to, salary_currency,
                 responses_count, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    v["vacancyId"],
                    v["name"],
                    v.get("@workSchedule"),
                    v["links"].get("desktop"),
                    v.get("creationTime"),
                    v.get("lastChangeTime", {}).get("$"),
                    v.get("area", {}).get("name"),
                    company.get("id"),
                    company.get("name"),
                    company.get("companySiteUrl"),
                    comp.get("from"),
                    comp.get("to"),
                    comp.get("currencyCode"),
                    v.get("totalResponsesCount", 0),
                    datetime.now().astimezone().isoformat(),
                ),
            )


def rand_text(s: str) -> str:
    while (
        r := re.sub(
            r"{([^{}]+)}",
            lambda m: random.choice(m.group(1).split("|")),
            s,
        )
    ) != s:
        s = r
    return s


class HHAutoApplier:
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ACCEPT_HEADER = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ACCEPT_LANGUAGE_HEADER = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"

    json_decoder = json.JSONDecoder()

    def __init__(
        self,
        search_url: str,
        cookies_path: Path,
        db_path: Path,
        resume_id: str | None = None,
        letter_file: Path | None = None,
        force_letter: bool = False,
        max_responses: int | None = None,
    ) -> None:
        self.force_letter = force_letter
        self.letter_template = (
            letter_file.read_text(encoding="utf-8")
            if letter_file and letter_file.exists()
            else None
        )
        parsed = urlparse(search_url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        self.search_params = parse_qs(parsed.query)
        self.cookies_path = cookies_path
        self.max_responses = max_responses
        self.session = self.get_session()
        self.resume_id = resume_id or self.get_latest_resume_hash()
        self.db = Database(db_path)

    @property
    def xsrf_token(self) -> str | None:
        return next((c.value for c in self.session.cookies if c.name == "_xsrf"), None)

    def resolve_url(self, url: str) -> str:
        return urljoin(self.base_url, url)

    def request(
        self, method: str, endpoint: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        url = self.resolve_url(endpoint)
        return self.session.request(method, url, *args, **kwargs)

    def get_latest_resume_hash(self) -> str:
        r = self.request("GET", "/applicant/resumes")
        r.raise_for_status()
        if not (match := re.search(r'"latestResumeHash":"([a-f0-9]+)"', r.text)):
            raise ValueError("latestResumeHash not found on page")
        return match.group(1)

    def get_session(self) -> requests.Session:
        if not self.cookies_path.exists():
            raise FileNotFoundError(f"Cookie file not found: {self.cookies_path}")
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self.USER_AGENT,
                "Accept-Language": self.ACCEPT_LANGUAGE_HEADER,
                "Accept": self.ACCEPT_HEADER,
            }
        )
        jar = http.cookiejar.MozillaCookieJar(self.cookies_path)
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies = jar  # pyright: ignore[reportAttributeAccessIssue]
        return session

    def save_cookies(self) -> None:
        if isinstance(self.session.cookies, http.cookiejar.MozillaCookieJar):
            self.session.cookies.save(ignore_discard=True, ignore_expires=True)

    def get_vacancy_tests(self, url: str) -> dict:
        response = self.session.get(url)
        response.raise_for_status()
        temp = response.text.split(',"vacancyTests":')[1]
        data, _ = self.json_decoder.raw_decode(temp)
        return data

    def send_response(self, payload: dict, referer_url: str) -> dict:
        r = self.request(
            "POST",
            "/applicant/vacancy_response/popup",
            data=payload,
            headers={
                "X-Hhtmfrom": "vacancy",
                "X-Hhtmsource": "vacancy_response",
                "X-Requested-With": "XMLHttpRequest",
                "X-Xsrftoken": self.xsrf_token,
            },
        )
        logger.debug(
            "%d %s %s %r", r.status_code, r.request.method, r.request.url, payload
        )
        return r.json()

    def apply_vacancy(
        self, vacancy_id: int, referer_url: str, letter: str = ""
    ) -> dict:
        payload = {
            "_xsrf": self.xsrf_token,
            "vacancy_id": vacancy_id,
            "resume_hash": self.resume_id,
            "letter": letter,
            "ignore_postponed": "true",
        }
        return self.send_response(payload, referer_url)

    def apply_vacancy_with_test(self, vacancy_id: int, letter: str = "") -> dict:
        response_url = self.resolve_url(
            f"/applicant/vacancy_response?vacancyId={vacancy_id}&startedWithQuestion=false&hhtmFrom=vacancy"
        )
        tests_data = self.get_vacancy_tests(response_url)
        test_data = tests_data[str(vacancy_id)]
        logger.debug(test_data)

        payload = {
            "_xsrf": self.xsrf_token,
            "uidPk": test_data["uidPk"],
            "guid": test_data["guid"],
            "startTime": test_data["startTime"],
            "testRequired": test_data["required"],
            "vacancy_id": vacancy_id,
            "resume_hash": self.resume_id,
            "ignore_postponed": "true",
            "incomplete": "false",
            "lux": "true",
            "withoutTest": "no",
            "letter": letter,
        }

        for task in test_data.get("tasks", []):
            field_name = f"task_{task['id']}"
            solutions = task.get("candidateSolutions") or []
            if solutions:
                payload[field_name] = solutions[len(solutions) // 2]["id"]
            else:
                payload[f"{field_name}_text"] = "Да"

        return self.send_response(payload, response_url)

    def get_vacancies(self) -> Iterator[VacancyData]:
        total_responses = 0
        total_vacancies_count = 0

        for page in count():
            params = self.search_params | {"page": page}
            response = self.request("GET", "/search/vacancy", params=params)

            temp = response.text.split(',"vacancies":')[1]
            vacancies, _ = self.json_decoder.raw_decode(temp)
            logger.debug("Найдено вакансий %d на странице %d", len(vacancies), page + 1)

            if not vacancies:
                break

            total_vacancies_count += len(vacancies)
            total_responses += sum(v["totalResponsesCount"] for v in vacancies)

            logger.debug(
                "Среднее количество откликов на вакансии: %d",
                total_responses // total_vacancies_count,
            )

            if self.max_responses:
                vacancies = list(
                    filter(
                        lambda v: self.max_responses >= v["totalResponsesCount"],
                        vacancies,
                    )
                )

            logger.debug("Вакансий после фильтров: %d", len(vacancies))
            yield from vacancies
            self.rand_delay(max_sec=5.0)

    def rand_delay(self, min_sec: float = 1.0, max_sec: float = 3.0) -> None:
        time.sleep(random.uniform(min_sec, max_sec))

    def apply_vacancies(self) -> None:
        try:
            for vacancy in self.get_vacancies():
                try:
                    vacancy_id = vacancy["vacancyId"]
                    vacancy_url = vacancy["links"]["desktop"]
                    vacancy_name = vacancy["name"]

                    if vacancy.get("userLabels"):
                        continue

                    is_letter_required = vacancy.get("@responseLetterRequired", False)
                    if is_letter_required and not self.letter_template:
                        logger.warning("Требуется письмо: %s", vacancy_url)
                        continue

                    letter = (
                        rand_text(self.letter_template).replace(
                            "%vacancyName%", vacancy_name
                        )
                        if (is_letter_required or self.force_letter)
                        and self.letter_template
                        else ""
                    )

                    self.rand_delay()

                    logger.debug(
                        f"Пробуем откликнуться на вакансию {vacancy_name!r} ({vacancy_url}; откликов: {vacancy.get('totalResponsesCount', 0)})"
                    )

                    if vacancy.get("userTestPresent"):
                        result = self.apply_vacancy_with_test(vacancy_id, letter=letter)
                    else:
                        result = self.apply_vacancy(
                            vacancy_id, vacancy_url, letter=letter
                        )

                    if err := result.get("error"):
                        if err == "negotiations-limit-exceeded":
                            logger.info("Суточный лимит откликов исчерпан")
                            return

                        logger.error(f"{err}: {vacancy_url}")
                        continue

                    if result.get("success"):
                        self.db.save_application(vacancy)
                        logger.debug(
                            f"Отклик успешно отправлен: {vacancy_url} ({vacancy_name})"
                        )
                        print("Отклик отправлен", vacancy_url, vacancy_name)
                    else:
                        logger.error(
                            "Неизвестная ошибка при отклике на вакансию: %s (%s)",
                            vacancy_url,
                            vacancy_name,
                        )
                except Exception as ex:
                    logger.error(
                        f"Ошибка при обработке ID {vacancy.get('vacancyId')}: {ex}"
                    )
        finally:
            self.save_cookies()


def main() -> int | None:
    parser = argparse.ArgumentParser(
        description="Автоматическая рассылка откликов на вакансии HH.ru"
    )
    parser.add_argument("-u", "--url", required=True, help="URL поискового запроса")
    parser.add_argument(
        "-c",
        "--cookies",
        type=Path,
        default=CURDIR / "cookies.txt",
        help="Путь к cookies",
    )
    parser.add_argument(
        "-d",
        "--database",
        type=Path,
        default=CURDIR / "applications.db",
        help="Путь к базе данных для сохранения откликов",
    )
    parser.add_argument(
        "-log",
        "--log-file",
        type=Path,
        default=CURDIR / "log.txt",
        help="Путь к файлу лога",
    )
    parser.add_argument(
        "-r", "--resume-id", help="ID резюме или будет использовано последнее"
    )
    parser.add_argument(
        "-l",
        "--letter-file",
        type=Path,
        default=CURDIR / "letter.txt",
        help="Путь к шаблону сопроводительного письма",
    )
    parser.add_argument(
        "-f",
        "--force-letter",
        action="store_true",
        help="Принудительная отправка письма",
    )
    parser.add_argument(
        "-mr",
        "--max-responses",
        type=int,
        help="Максимальное количества откликов для вакансии",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Подробное логирование",
    )

    args = parser.parse_args()

    fh = logging.handlers.RotatingFileHandler(
        args.log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=1,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(fh)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    applier = HHAutoApplier(
        search_url=args.url,
        cookies_path=args.cookies,
        db_path=args.database,
        resume_id=args.resume_id,
        letter_file=args.letter_file,
        force_letter=args.force_letter,
        max_responses=args.max_responses,
    )

    try:
        applier.apply_vacancies()
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
    except Exception as ex:
        logger.exception(ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
