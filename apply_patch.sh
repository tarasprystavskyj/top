#!/bin/bash
# Автоматичне застосування патча до api_main.py
# Використання: ./apply_patch.sh

set -e

echo "🔧 Застосування Git Deploy патча до api_main.py"

API_MAIN="UI/backend/api_main.py"

if [ ! -f "$API_MAIN" ]; then
    echo "❌ Файл $API_MAIN не знайдено!"
    echo "Переконайся що ти в корені репозиторію /var/www/vps2.happyuser.info/top/top_1/obw_platform"
    exit 1
fi

# Перевіряємо чи патч вже застосовано
if grep -q "from git_deploy import router as deploy_router" "$API_MAIN"; then
    echo "✅ Патч вже застосовано!"
    exit 0
fi

# Створюємо backup
cp "$API_MAIN" "${API_MAIN}.backup_$(date +%Y%m%d_%H%M%S)"
echo "📦 Backup створено: ${API_MAIN}.backup_*"

# Знаходимо рядок з app = FastAPI()
LINE_NUM=$(grep -n "^app = FastAPI()" "$API_MAIN" | cut -d: -f1)

if [ -z "$LINE_NUM" ]; then
    echo "❌ Не знайдено рядок 'app = FastAPI()'"
    exit 1
fi

echo "📍 Знайдено 'app = FastAPI()' на рядку $LINE_NUM"

# Додаємо інтеграцію ПІСЛЯ app = FastAPI()
INTEGRATION_CODE="
# Git Deploy endpoints
from git_deploy import router as deploy_router
app.include_router(deploy_router, prefix=\"/api\", tags=[\"deploy\"])
"

# Використовуємо sed для вставки коду після знайденого рядка
sed -i "${LINE_NUM}a\\${INTEGRATION_CODE}" "$API_MAIN"

echo "✅ Патч успішно застосовано!"
echo ""
echo "📋 Наступні кроки:"
echo "1. Встанови python-dotenv: pip install python-dotenv"
echo "2. Додай на початок api_main.py (після імпортів):"
echo "   from dotenv import load_dotenv"
echo "   load_dotenv()"
echo "3. Перезапусти backend"
echo "4. Відкрий http://your-domain.com/deploy"
