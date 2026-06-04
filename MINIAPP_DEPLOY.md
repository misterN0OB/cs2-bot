# Деплой Telegram Mini App

SSH: `ssh -i C:\Users\mik-p\Documents\ssh-key.key ubuntu@138.2.228.123`

---

## Шаг 1 — Установить зависимости на сервере

```bash
pip install flask flask-cors --break-system-packages
```

## Шаг 2 — Задеплоить файлы

```powershell
# В PowerShell на своём компьютере
cd Z:\cs2-bot
git add .
git commit -m "add mini app and api"
git push
```

```bash
# На сервере
cd ~/cs2-bot && git pull
```

## Шаг 3 — Создать systemd сервис для API

```bash
sudo nano /etc/systemd/system/cs2api.service
```

Содержимое:
```ini
[Unit]
Description=CS2 Skin Tracker API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/cs2-bot
ExecStart=/usr/bin/python3 /home/ubuntu/cs2-bot/api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable cs2api
sudo systemctl start cs2api
sudo systemctl status cs2api
```

## Шаг 4 — Открыть порт 8001

```bash
sudo ufw allow 8001/tcp
```

В Oracle Cloud Console (важно!):
- VCN → Security Lists → добавить правило: TCP порт 8001, source 0.0.0.0/0

## Шаг 5 — GitHub Pages для index.html

Убедись что в репозитории включён GitHub Pages с папки `docs/`.
Settings → Pages → Source: Deploy from branch `main` → folder `/docs`

URL мини-приложения будет: `https://ТВОЙ_USERNAME.github.io/cs2-bot/`

## Шаг 6 — Зарегистрировать Mini App в BotFather

```
/newapp
```
Выбери своего бота → введи название → загрузи иконку 640×360 px →
укажи URL: `https://ТВОЙ_USERNAME.github.io/cs2-bot/`

## Шаг 7 — Добавить кнопку в бота

В bot.py добавь кнопку меню (открывается при нажатии на иконку рядом с полем ввода):

```python
from telegram import MenuButtonWebApp, WebAppInfo

async def post_init(app):
    await app.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="📊 Трекер",
            web_app=WebAppInfo(url="https://ТВОЙ_USERNAME.github.io/cs2-bot/")
        )
    )
```

Или добавь инлайн-кнопку в /start:
```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

keyboard = InlineKeyboardMarkup([[
    InlineKeyboardButton("📊 Открыть трекер", web_app=WebAppInfo(url="https://ТВОЙ_USERNAME.github.io/cs2-bot/"))
]])
```

## Проверка работы

```bash
# Проверить что API отвечает
curl http://138.2.228.123:8001/api/health

# Логи API
sudo journalctl -u cs2api -n 30 -f
```

---

## Важно про HTTPS

Telegram Mini App работает только через HTTPS. 
- `index.html` — хостится на GitHub Pages (HTTPS автоматически ✓)
- `api.py` — работает на HTTP порту 8001

Из-за mixed content (HTTPS страница → HTTP API) могут быть блокировки в некоторых браузерах.
Решение: настроить nginx + SSL на сервере и проксировать `/api/` на `localhost:8001`.

```nginx
server {
    listen 443 ssl;
    server_name ИМЯ_ДОМЕНА;
    
    location /api/ {
        proxy_pass http://127.0.0.1:8001/api/;
        proxy_set_header Host $host;
    }
}
```
