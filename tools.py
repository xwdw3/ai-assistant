import os
import datetime

from config import NOTES_DIR


def _safe_path(filename):
    """把模型给的文件名收敛到 notes 目录内：去掉路径部分、补 .txt、兜住空名。"""
    name = os.path.basename(str(filename)).strip() or "未命名"
    if not name.lower().endswith(".txt"):
        name += ".txt"
    return os.path.join(NOTES_DIR, name)


def _dedupe_path(path):
    """同名文件已存在时，依次尝试 name_1.txt、name_2.txt……避免覆盖。"""
    stem, suffix = os.path.splitext(path)
    for i in range(1, 100):
        candidate = f"{stem}_{i}{suffix}"
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError("同名笔记已有 100 个，请换一个文件名")


def save_note(filename,content,overwrite=False):
    """把内容保存成笔记文件。当需要把整理好的内容记录下来时使用"""
    os.makedirs(NOTES_DIR,exist_ok=True)
    path = _safe_path(filename)
    existed = os.path.exists(path)

    if existed and not overwrite:
        path = _dedupe_path(path)   # 默认不覆盖，另存一份

    with open(path,"w",encoding="utf-8") as f:
        f.write(content)

    if existed and not overwrite:
        prefix = "原文件已存在，另存为"
    elif existed:
        prefix = "已覆盖"
    else:
        prefix = "已保存到"
    return f"{prefix}{path},共{len(content)}字"

def read_note(filename):
    """读取笔记内容。当需要查看之前记录的笔记时使用"""
    path = _safe_path(filename)
    if not os.path.isfile(path):
        return f"没有找到笔记:{os.path.basename(path)}"
    with open(path,"r",encoding="utf-8") as f:
        return f.read()[:2000]

def list_notes():
    """列出所有已保存的笔记。当需要知道有哪些笔记时使用"""
    if not os.path.isdir(NOTES_DIR):
        return f"还没有任何笔记"
    files = sorted(f for f in os.listdir(NOTES_DIR) if f.endswith(".txt"))
    return "已有笔记:\n"+"\n".join(files) if files else "还没有任何笔记"

def get_date():
    """获取今天的日期。当需要知道当前日期时使用"""
    return datetime.date.today().strftime("%Y年%m月%d日")


# ============================================================
# 以下是"注册层"，被 agent.py 使用：
#   TOOL_MAP —— 名字到实现的映射，执行器靠它把模型的选择变成真实调用
#   TOOLS    —— 工具说明书，随请求发给模型，决定它何时调用、参数怎么填
# 新增工具的固定三步：写函数 → 加进 TOOL_MAP → 在 TOOLS 里补一段声明
# ============================================================

TOOL_MAP = {
    "save_note": save_note,
    "read_note": read_note,
    "list_notes": list_notes,
    "get_date": get_date,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "把文本内容保存成一篇笔记文件。文件名建议带 .txt 后缀，缺了会自动补上；"
                "同名文件默认不会被覆盖，而是自动另存为 名字_1.txt，不会丢内容。"
                "只有明确要替换原文件时才把 overwrite 设为 true。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "笔记文件名，例如 周报.txt（.txt 后缀可省略）",
                    },
                    "content": {
                        "type": "string",
                        "description": "要保存的完整文本内容（不支持追加）",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": (
                            "是否覆盖同名文件，默认 false。"
                            "false 时同名会自动另存为 名字_1.txt；true 才会替换原文件"
                        ),
                    },
                },
                "required": ["filename", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": (
                "读取一篇已存在的笔记。最多返回前 2000 字，"
                "超出的部分会被静默丢弃，不会提示截断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "笔记文件名，例如 周报.txt（.txt 后缀可省略）",
                    },
                },
                "required": ["filename"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": (
                "列出 notes 目录下所有 .txt 笔记的文件名，不含内容。"
                "不确定有哪些笔记时先调用它，再用 read_note 按名字读取。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_date",
            "description": (
                "获取今天的日期，返回格式如 2026年10月20日。"
                "任务里出现「今天」「本周」「这几天」等相对时间时，先调用它确认日期。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]
