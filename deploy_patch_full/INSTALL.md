# 🚀 Git Deploy System - Повна Інструкція Встановлення

## 📦 Що в цьому патчі:

```
deploy_patch_full/
├── UI/
│   ├── backend/
│   │   ├── git_deploy.py       # API для деплою
│   │   └── .env                # Конфігурація (з твоїм ключем!)
│   └── frontend/
│       └── pages/
│           └── deploy.tsx      # Веб-інтерфейс
├── apply_patch.sh              # Автоматичний скрипт застосування
├── API_MAIN_PATCH.txt          # Ручний патч для api_main.py
├── INSTALL.md                  # Ця інструкція
└── requirements.txt            # Python залежності
```

---

## ⚡ ШВИДКЕ ВСТАНОВЛЕННЯ (3 хвилини)

### Крок 1: Завантаж і розпакуй на сервер

```bash
# На локальному комп'ютері завантаж deploy_patch_full.zip

# Залий на сервер
scp deploy_patch_full.zip simple_user@vps2.happyuser.info:/tmp/

# На сервері
ssh simple_user@vps2.happyuser.info
cd /var/www/vps2.happyuser.info/top/top_1/obw_platform

# Розпакуй (файли впадуть в правильні місця)
unzip -o /tmp/deploy_patch_full.zip
```

### Крок 2: Встанови залежності

```bash
cd /var/www/vps2.happyuser.info/top/top_1/obw_platform/UI/backend

# Активуй venv
source ../../.venv38/bin/activate

# Встанови python-dotenv
pip install python-dotenv

# Або через requirements.txt
pip install -r requirements.txt
```

### Крок 3: Застосуй патч до api_main.py

**Варіант А: Автоматично** (рекомендую)
```bash
cd /var/www/vps2.happyuser.info/top/top_1/obw_platform
chmod +x apply_patch.sh
./apply_patch.sh
```

**Варіант Б: Вручну**
```bash
nano UI/backend/api_main.py

# 1. Додай на початку файлу (після імпортів):
from dotenv import load_dotenv
load_dotenv()

# 2. Знайди рядок: app = FastAPI()
# 3. Додай ПІСЛЯ нього:
from git_deploy import router as deploy_router
app.include_router(deploy_router, prefix="/api", tags=["deploy"])

# Збережи: Ctrl+O, Enter, Ctrl+X
```

### Крок 4: Захисти .env файл

```bash
cd /var/www/vps2.happyuser.info/top/top_1/obw_platform/UI/backend
chmod 600 .env

# Додай в .gitignore
echo ".env" >> ../../.gitignore
```

### Крок 5: Перезапусти backend

```bash
# Якщо через systemd:
sudo systemctl restart your_backend_service

# Якщо вручну:
cd /var/www/vps2.happyuser.info/top/top_1/obw_platform/UI/backend
source ../../.venv38/bin/activate
uvicorn api_main:app --reload --host 0.0.0.0 --port 8000
```

### Крок 6: Тестуй!

```bash
# Перевір API
curl http://localhost:8000/api/health

# Тестуй deploy status (підстав свій ключ з .env)
curl "http://localhost:8000/api/deploy/status?secret=9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0"

# Тестуй git pull
curl "http://localhost:8000/api/deploy/pull?secret=9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0"
```

### Крок 7: Відкрий веб-інтерфейс

```
http://vps2.happyuser.info:3000/deploy
```

Введи секретний ключ з `.env` файлу:
```
9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0
```

---

## 🔐 Безпека

### Твій секретний ключ:
```
9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0
```

⚠️ **ЗБЕРЕЖИ ЙОГО В БЕЗПЕЧНОМУ МІСЦІ!**

### Захист доступу через Nginx (опціонально):

```nginx
# /etc/nginx/sites-available/your-site

location /api/deploy {
    # Дозволити тільки з локальної мережі
    allow 192.168.0.0/16;
    allow 10.0.0.0/8;
    deny all;
    
    proxy_pass http://localhost:8000;
}

location /deploy {
    # Та сама логіка для фронтенд сторінки
    allow 192.168.0.0/16;
    allow 10.0.0.0/8;
    deny all;
    
    proxy_pass http://localhost:3000;
}
```

---

## 📱 Використання

### Через веб-інтерфейс:
1. Відкрий `http://vps2.happyuser.info:3000/deploy`
2. Введи секретний ключ
3. Натисни "Execute git pull"
4. Готово! ✅

### Через прямий лінк (можна в закладки):
```
http://vps2.happyuser.info:8000/api/deploy/pull?secret=9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0
```

### Через curl:
```bash
# GET
curl "http://vps2.happyuser.info:8000/api/deploy/pull?secret=9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0"

# POST
curl -X POST "http://vps2.happyuser.info:8000/api/deploy/pull" \
  -H "Content-Type: application/json" \
  -d '{"secret":"9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0"}'
```

---

## ❓ Проблеми?

### Backend не стартує:
```bash
# Перевір логи
tail -f /var/log/your-app/error.log

# Перевір що dotenv встановлено
pip list | grep python-dotenv

# Перевір що .env існує
ls -la UI/backend/.env
```

### API повертає 403:
- Перевір що ключ правильний
- Подивись в `.env` файл
- Перезапусти backend після змін

### Git pull не працює:
```bash
# Перевір SSH ключ
ssh-add -l

# Перевір git
cd /var/www/vps2.happyuser.info/top/top_1/obw_platform
git pull
```

---

## 🎯 Структура файлів після встановлення:

```
/var/www/vps2.happyuser.info/top/top_1/obw_platform/
├── UI/
│   ├── backend/
│   │   ├── api_main.py          ← МОДИФІКОВАНО (додано git_deploy роутер)
│   │   ├── git_deploy.py        ← НОВИЙ
│   │   ├── .env                 ← НОВИЙ (з твоїм ключем)
│   │   └── requirements.txt     ← ОНОВЛЕНО (додано python-dotenv)
│   └── frontend/
│       └── pages/
│           └── deploy.tsx       ← НОВИЙ
├── .gitignore                   ← ОНОВЛЕНО (додано .env)
└── apply_patch.sh               ← Скрипт для застосування патча
```

---

## ✅ Чеклист

- [ ] Файли скопійовані в правильні місця
- [ ] python-dotenv встановлено
- [ ] api_main.py модифіковано (патч застосовано)
- [ ] .env файл захищено (chmod 600)
- [ ] .env додано в .gitignore
- [ ] Backend перезапущено
- [ ] API працює (curl тести пройшли)
- [ ] Веб-інтерфейс відкривається
- [ ] Git pull через API працює

---

## 🚀 Наступні кроки (опціонально)

1. **Логування** - додати запис всіх deploy операцій
2. **Webhook з GitHub** - автоматичний деплой при push
3. **Slack/Telegram** - сповіщення про деплой
4. **Rollback** - відкат до попереднього коміта
5. **Multi-repo** - деплой кількох репозиторіїв

---

Якщо щось не працює - напиши мені деталі! 💪
