# Как запустить Cerberus (для друга)

## Требования
- Linux (Ubuntu, Debian, Raspberry Pi) **или** Android с Termux
- Интернет соединение
- Python 3.9+ (скрипт установит сам)

---

## Один шаг — Linux (Ubuntu / Debian / Raspberry Pi)

```bash
curl -fsSL https://raw.githubusercontent.com/VItaly0117/cerberus/main/install_and_run.sh | bash
```

или если уже скачал:

```bash
chmod +x install_and_run.sh
./install_and_run.sh
```

---

## Для Android (Termux) — тоже один шаг

Открыть Termux и вставить:

```bash
curl -fsSL https://raw.githubusercontent.com/VItaly0117/cerberus/main/install_and_run.sh | bash
```

Скрипт **автоматически** определяет Termux и не использует `sudo` — всё работает без рута.

> **Примечание:** если `curl` не установлен в свежем Termux, сначала:
> ```bash
> pkg install curl -y
> ```
> и затем повторить команду выше.

---

## Что делает скрипт
1. Определяет окружение (Termux / Ubuntu / Debian / Arch)
2. Устанавливает Python, git, curl, screen без лишних вопросов
3. Клонирует репозиторий в `~/project-cerberus`
4. Создаёт виртуальное окружение и ставит зависимости
5. Запускает `--preflight` проверку — если что-то не так, скажет
6. Спрашивает сколько часов гонять (по умолчанию 72)
7. Запускает в фоне через **screen** — можно закрыть терминал/приложение, всё продолжит работать

---

## Наблюдать за процессом

```bash
screen -r cerberus                                        # подключиться к сессии
tail -f ~/project-cerberus/artifacts/paper/run.log       # только лог
```

Отключиться от screen без остановки: **Ctrl+A, затем D**

---

## Получить отчёт

После окончания отчёт появится в:
```
~/project-cerberus/artifacts/paper/report_*.json
```

Посмотреть:
```bash
cat ~/project-cerberus/artifacts/paper/report_*.json
```

---

## Если что-то пошло не так

| Проблема | Решение |
|----------|---------|
| `sudo: command not found` | Ты в Termux — скрипт должен это определить автоматически. Убедись что используешь последнюю версию скрипта. |
| `python3: command not found` | `pkg install python` (Termux) или `sudo apt install python3` (Linux) |
| `curl: command not found` | `pkg install curl` (Termux) или `sudo apt install curl` (Linux) |
| Preflight упал на `websockets` | `pip install websockets` внутри `.venv` |
| screen завис | `screen -X -S cerberus quit` — убить, потом запустить скрипт заново |
