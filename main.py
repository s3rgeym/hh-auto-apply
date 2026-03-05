#!/usr/bin/env python
import argparse
import http.cookiejar
import json
import logging
import os.path
import random
import re
import sys
import time
from itertools import count
from pathlib import Path
from typing import Any, Iterator, TypedDict
from urllib.parse import parse_qs, urljoin, urlparse

import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

Vacancy = TypedDict(
    "Vacancy",
    {
        "@isAdv": bool,
        "@responseLetterRequired": bool,
        "@showContact": bool,
        "@workSchedule": str,
        "acceptLaborContract": bool,
        "allowChatWithManager": bool,
        "civilLawContracts": list[dict[str, Any]],
        "employment": dict[str, str],
        "links": dict[str, str],
        "name": str,
        "professionalRoleIds": list[dict[str, Any]],
        "responsesCount": int,
        "searchRid": str,
        "show_question_input": bool,
        "totalResponsesCount": int,
        "userLabels": list[str],
        "userTestPresent": bool,
        "vacancyId": int,
        "vacancyProperties": dict[str, Any],
        "workExperience": str,
        "workFormats": list[dict[str, Any]],
        "workingTimeIntervals": list[dict[str, Any]],
    },
)


def rand_text(s: str) -> str:
    while (
        s1 := re.sub(
            r"{([^{}]+)}",
            lambda m: random.choice(m.group(1).split("|")),
            s,
        )
    ) != s:
        s = s1
    return s


class HHAutoApplier:
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ACCEPT_HEADER = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ACCEPT_LANGUAGE_HEADER = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"

    json_decoder = json.JSONDecoder()

    def __init__(
        self,
        search_url: str,
        cookies_filename: str,
        resume_id: str | None = None,
        letter_file: str | None = None,
        force_letter: bool = False,
    ) -> None:
        self.force_letter = force_letter
        self.letter_template = (
            open(letter_file, encoding="utf-8").read()
            if letter_file and os.path.exists(letter_file)
            else None
        )
        parsed = urlparse(search_url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        # self.base_search_url = parsed._replace(query="", fragment="").geturl()
        self.search_params = parse_qs(parsed.query)
        self.cookies_path = Path(cookies_filename)
        self.session = self.get_session()
        self.resume_id = resume_id or self.get_latest_resume_hash()

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
        logger.debug("Отправляем отклик с данными: %r", payload)
        r = self.request(
            "POST",
            "/applicant/vacancy_response/popup",
            data=payload,
            headers={
                # "Referer": referer_url,
                "X-Hhtmfrom": "vacancy",
                "X-Hhtmsource": "vacancy_response",
                "X-Requested-With": "XMLHttpRequest",
                "X-Xsrftoken": self.xsrf_token,
            },
        )
        # r.raise_for_status()
        # assert r.status_code >= 200
        logger.debug("%d %s", r.status_code, r.url)
        data = r.json()
        return data

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
            # question = (task.get("description") or "").strip()
            if solutions:
                # Чаще всего правильный ответ в середину пихают
                payload[field_name] = solutions[len(solutions) // 2]["id"]
            else:
                payload[f"{field_name}_text"] = "Да"

        return self.send_response(payload, response_url)

    def get_vacancies(self) -> Iterator[Vacancy]:
        for page in count():
            params = self.search_params | {"page": [str(page)]}
            logger.debug(f"Ищем вакансии: {params}")
            response = self.request("GET", "/search/vacancy", params=params)
            temp = response.text.split(',"vacancies":')[1]
            vacancies, _ = self.json_decoder.raw_decode(temp)
            logger.debug("Найдено вакансий на странице: %d", len(vacancies))
            if not vacancies:
                break
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
                        logger.debug("Пропускаем вакансию с откликом %s", vacancy_url)
                        continue

                    is_letter_required = vacancy.get("@responseLetterRequired", False)
                    if is_letter_required and not self.letter_template:
                        logger.warning(
                            "Для отклика на вакансию требуется сопроводительное: %s",
                            vacancy_url,
                        )
                        continue

                    letter = (
                        rand_text(self.letter_template)  # pyright: ignore[reportArgumentType]
                        if is_letter_required or self.force_letter
                        else ""
                    ).replace("%vacancyName%", vacancy_name)

                    self.rand_delay()

                    if vacancy.get("userTestPresent"):
                        logger.debug(
                            "Пробуем откликнуться на вакансию с тестом: %s", vacancy_url
                        )
                        result = self.apply_vacancy_with_test(vacancy_id, letter=letter)
                    else:
                        logger.debug(
                            "Пробуем откликнуться на вакансию: %s", vacancy_url
                        )
                        result = self.apply_vacancy(
                            vacancy_id, vacancy_url, letter=letter
                        )

                    # logger.debug(result)
                    if err := result.get("error"):
                        if err == "negotiations-limit-exceeded":
                            logger.info("Достигли лимита на отклики!")
                            return

                        logger.error(err)
                        continue

                    assert result.get("success")
                    print(
                        f"Отправили отклик на {vacancy_url}: {vacancy_name} (откликов: {vacancy['totalResponsesCount']})"
                    )
                except requests.RequestException as ex:
                    logger.error(ex)
        finally:
            self.save_cookies()


def main() -> None | int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-u", "--url", help="Ссылка, используемая для поиска вакансий", required=True
    )
    parser.add_argument("-c", "--cookies", help="Путь до кукис", default="cookies.txt")
    parser.add_argument(
        "-r", "--resume-id", "--resume", help="Резюме используемое для откликов"
    )
    parser.add_argument(
        "-l",
        "--letter-file",
        "--letter",
        default="letter.txt",
        help=(
            "Файл с сопроводительным письмом."
            " Выбор случайного варианта: {вариант 1|вариант 2|вариант 3}."
            " Варианты могут быть вложенными."
            " %%vacancyName%% будет заменено на название вакансии."
        ),
    )
    parser.add_argument(
        "-f",
        "--force-letter",
        help="Отправлять сопроводительное всегда",
        action="store_true",
    )
    parser.add_argument(
        "-v", "--verbose", help="Более подробный вывод", action="store_true"
    )

    args = parser.parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    applier = HHAutoApplier(
        search_url=args.url,
        cookies_filename=args.cookies,
        resume_id=args.resume_id,
        letter_file=args.letter_file,
        force_letter=args.force_letter,
    )

    try:
        return applier.apply_vacancies()
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logger.exception(ex)
    return 1


if __name__ == "__main__":
    sys.exit(main())
