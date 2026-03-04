Скрипт для поиска и отклика на вакансии на сайте HH.RU.

Что умеет?

- Откликаться на вакансии, генерируя сопроводительные письма, а так же решать тесты (на все отвечает Да и выбирает средний вариант).

Инструкция:

- Создай и активируй виртуальное окружение, а потом установи зависимости.
  ```sh
  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```
- С помощью расширения [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) сохрани куки с `hh.ru` в файл `cookies.txt` и положи его рядом с `main.py`.
- Скопируй полностью ссылку для поиска вакансий из адресной строки браузера (что-то типа `https://hh.ru/search/vacancy?text=python&search_field=name...`).
- Запусти с активированным виртуальным окружением `python main.py -u <поисковая ссылка>`.
- Справка доступна с флагом `-h`.
