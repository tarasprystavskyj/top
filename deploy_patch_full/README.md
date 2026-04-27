# 🚀 Git Deploy System - READY TO USE

## ⚡ Швидкий старт (30 секунд)

```bash
# 1. Розпакуй в корінь репозиторію
cd /var/www/vps2.happyuser.info/top/top_1/obw_platform
unzip -o /tmp/deploy_patch_full.zip

# 2. Застосуй патч
chmod +x apply_patch.sh && ./apply_patch.sh

# 3. Встанови залежності
pip install python-dotenv

# 4. Перезапусти backend
# (твій спосіб запуску backend)

# 5. ГОТОВО! Відкрий:
# http://vps2.happyuser.info:3000/deploy
```

---

## 🔑 Твій секретний ключ:

```
9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0
```

Введи його на сторінці `/deploy` для доступу.

---

## 📁 Що всередині:

- ✅ **UI/backend/git_deploy.py** - API ендпоінти
- ✅ **UI/backend/.env** - Конфігурація (З ТВОЇМ КЛЮЧЕМ!)
- ✅ **UI/frontend/pages/deploy.tsx** - Веб-інтерфейс
- ✅ **apply_patch.sh** - Автоматичне застосування патча
- ✅ **API_MAIN_PATCH.txt** - Ручний патч (якщо скрипт не спрацює)

---

## 📖 Детальна інструкція:

Читай **INSTALL.md** для повної інформації.

---

## 🧪 Швидкий тест:

```bash
# Тестуй API (підстав свій домен)
curl "http://localhost:8000/api/deploy/status?secret=9f078fc7470e358255cf18eeb6a84f7a11c834dedd618344d261e4432ff36af0"
```

---

Все готово до використання! 🎉
