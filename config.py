"""集中配置：所有可调参数都在这里，支持通过环境变量或 .env 覆盖。"""

import os
from pathlib import Path

try:  # python-dotenv 是可选的，没装也能用系统环境变量
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# 项目根目录，避免依赖启动时的工作目录
BASE_DIR = Path(__file__).resolve().parent

# ---- 模型服务 ----
API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

# ---- 运行参数 ----
MAX_STEPS = int(os.getenv("MAX_STEPS", "8"))  # 执行阶段最多调用几轮模型
STALL_LIMIT = int(os.getenv("STALL_LIMIT", "2"))  # 连续重复几轮算「无进展」，触发干预

# ---- 成本与超时保护 ----
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "60000"))  # 单次运行累计 token 上限
STEP_TIMEOUT = float(os.getenv("STEP_TIMEOUT", "60"))  # 单次模型请求超时（秒）
MAX_TOOL_RESULT = int(os.getenv("MAX_TOOL_RESULT", "4000"))  # 单条工具结果进上下文的字数上限
# 单价按「元 / 百万 token」填；两项都为 0 时不做费用估算，只按 token 卡上限
PRICE_INPUT = float(os.getenv("PRICE_INPUT", "0"))
PRICE_OUTPUT = float(os.getenv("PRICE_OUTPUT", "0"))
MAX_COST = float(os.getenv("MAX_COST", "0"))  # 元，0 表示不限
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3"))

# ---- 存储 ----
NOTES_DIR = BASE_DIR / "notes"
