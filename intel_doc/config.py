"""Settings, read from environment / .env. Everything has a free-tier default."""
import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://inteldoc:inteldoc@localhost:5433/inteldoc")

# Any OpenAI-compatible endpoint. Default: Groq free tier running OpenAI's open-weight
# gpt-oss-120b (no card, ~1,000 requests/day). Get a key at https://console.groq.com/keys
#   OpenAI directly: OPENAI_BASE_URL=https://api.openai.com/v1  OPENAI_MODEL=gpt-4o-mini  LLM_VISION=true
#   Gemini (free):   OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
#                    OPENAI_MODEL=gemini-2.5-flash  LLM_VISION=true
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "openai/gpt-oss-120b")
# Send page images to the model? Only for vision models. Text is always sent (text layer or OCR).
LLM_VISION = os.getenv("LLM_VISION", "false").lower() == "true"

# Validation policy
AMOUNT_TOLERANCE = Decimal(os.getenv("AMOUNT_TOLERANCE", "0.02"))        # absolute, per comparison
PO_OVERRUN_TOLERANCE = Decimal(os.getenv("PO_OVERRUN_TOLERANCE", "0.02"))  # 2% over PO allowed
DUPLICATE_WINDOW_DAYS = int(os.getenv("DUPLICATE_WINDOW_DAYS", "30"))
STALE_INVOICE_DAYS = int(os.getenv("STALE_INVOICE_DAYS", "365"))
DATE_ORDER = os.getenv("DATE_ORDER", "MDY")  # how to read ambiguous dates like 03/04/2025
