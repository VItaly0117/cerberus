# Как запустить Cerberus (для друга)

## Требования
- Linux (Ubuntu, Debian, Raspberry Pi) или Android с Termux
- Интернет соединение
- Python 3.9+ (скрипт установит сам)

## Один шаг

```bash
curl -fsSL https://raw.githubusercontent.com/VItaly0117/project-cerberus/main/install_and_run.sh | bash
```

или если уже скачал:

```bash
chmod +x install_and_run.sh
./install_and_run.sh
```

## Что делает скрипт
1. Устанавливает Python и зависимости
2. Клонирует репозиторий
3. Запускает preflight проверку
4. Спрашивает сколько часов гонять (по умолчанию 72)
5. Запускает в фоне через screen — можно закрыть терминал, всё продолжит работать

## Наблюдать за процессом
```bash
screen -r cerberus          # подключиться
tail -f ~/project-cerberus/artifacts/paper/run.log  # только лог
```

## Получить отчёт
После окончания отчёт в:
```
~/project-cerberus/artifacts/paper/report_*.json
```
Скопировать и отправить:
```bash
cat ~/project-cerberus/artifacts/paper/report_*.json
```

## Для Android (Termux)
```bash
pkg update && pkg install python git curl screen
curl -fsSL https://raw.githubusercontent.com/VItaly0117/project-cerberus/main/install_and_run.sh | bash
```
