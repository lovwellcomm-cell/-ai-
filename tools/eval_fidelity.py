"""数字可溯源性检查 —— 实现已移到 rag/fidelity.py。

回答流程现在也用它给表格逐格核对（见 rag/fidelity.py:unverified_in_tables），
所以逻辑放进 rag 包，这里只保留旧的导入路径，让评估工具和测试不用改。
"""
from rag.fidelity import (  # noqa: F401
    UNIT_SCALE,
    _chunk_index,
    _derivable,
    _found,
    _near,
    _tolerance,
    check,
    extract_numbers,
)
