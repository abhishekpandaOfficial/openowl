#!/bin/bash
# ═══════════════════════════════════════════════════════════
# OpenOwl Quick Start Script
# Run: chmod +x scripts/start.sh && ./scripts/start.sh
# ═══════════════════════════════════════════════════════════

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo -e "${BLUE}🦉 OpenOwl — Personal Autonomous Agent${NC}"
echo -e "${BLUE}════════════════════════════════════════${NC}"
echo ""

# Check .env exists
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️  .env not found. Creating from template...${NC}"
    cp .env.example .env
    echo -e "${RED}❗ Please edit .env and fill in your API keys, then run this script again.${NC}"
    echo ""
    echo "  Required minimum:"
    echo "  1. TELEGRAM_BOT_TOKEN  (from @BotFather)"
    echo "  2. GROQ_API_KEY        (from console.groq.com — FREE)"
    echo "  3. TELEGRAM_WEBHOOK_URL (run: ngrok http 8000)"
    echo ""
    exit 1
fi

# Check Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker not found. Install from https://docker.com${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Starting services with Docker Compose...${NC}"
docker-compose up -d postgres redis

echo -e "${YELLOW}⏳ Waiting for databases...${NC}"
sleep 5

# Pull Ollama model if Ollama is running
echo -e "${GREEN}🤖 Setting up local AI model (Mistral-7B)...${NC}"
docker-compose up -d ollama
sleep 3
docker exec openowl-ollama ollama pull mistral:7b 2>/dev/null || \
    echo -e "${YELLOW}⚠️  Ollama model pull failed (will use Groq API instead)${NC}"

# Start the main app
echo -e "${GREEN}🚀 Starting OpenOwl...${NC}"
docker-compose up -d openowl

echo ""
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ OpenOwl is running!${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""
echo -e "  🌐 Dashboard:  ${BLUE}http://localhost:8000${NC}"
echo -e "  📋 API Docs:   ${BLUE}http://localhost:8000/docs${NC}"
echo -e "  ❤️  Health:    ${BLUE}http://localhost:8000/health${NC}"
echo ""
echo -e "${YELLOW}📡 To receive Telegram messages, expose port 8000:${NC}"
echo -e "   ${BLUE}ngrok http 8000${NC}"
echo -e "   Then copy the https URL to TELEGRAM_WEBHOOK_URL in .env"
echo -e "   Then restart: ${BLUE}docker-compose restart openowl${NC}"
echo ""
echo -e "📱 ${YELLOW}Test via Telegram:${NC} Message your bot and say 'hello'"
echo ""
