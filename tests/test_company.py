"""目标识别（公司名 / 年份）的测试。"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.company import (  # noqa: E402
    extract_target, normalize, parse_target, same_company, year_in,
)


def client_returning(content=None, exc=None):
    class Completions:
        def create(self, **kw):
            if exc:
                raise exc
            msg = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class C:
        chat = SimpleNamespace(completions=Completions())

    return C()


# ---------------------------------------------------------------- 归一化

def test_normalize_strips_common_suffixes():
    assert normalize("精达股份有限公司") == "精达"
    assert normalize("精达股份") == "精达"
    assert normalize(" 七 匹 狼 ") == "七匹狼"
    assert normalize("") == ""


def test_same_company_handles_short_names():
    assert same_company("精达股份", "精达") is True
    assert same_company("七匹狼", "七匹狼") is True
    assert same_company("七匹狼", "精达股份") is False
    assert same_company("", "精达") is False


def test_year_in_only_accepts_four_digit_years():
    assert year_in("七匹狼2024年利润率") == 2024
    assert year_in("2025 年营业收入") == 2025
    assert year_in("近三年变化") is None
    assert year_in("25年营业利润") is None      # 两位年份不在这里换算


# ---------------------------------------------------------------- 解析

def test_parse_target_accepts_grounded_company():
    out = parse_target('{"company":"七匹狼","year":2024,"multiple":false}', "七匹狼2024年利润率")
    assert out == {"company": "七匹狼", "year": 2024, "multiple": False}


def test_parse_target_rejects_hallucinated_company():
    """问题里没出现的公司名不要 —— 否则会去下载一家毫不相干的公司。"""
    out = parse_target('{"company":"贵州茅台","year":null}', "七匹狼利润率变化如何")
    assert out["company"] == ""


def test_parse_target_accepts_short_form():
    """模型把“茅台”写成“贵州茅台”时，按后缀匹配仍然认。"""
    out = parse_target('{"company":"贵州茅台","year":null}', "茅台2025年利润率")
    assert out["company"] == "贵州茅台"


def test_parse_target_tolerates_bad_values():
    assert parse_target("not json", "七匹狼")["company"] == ""
    assert parse_target('{"company":"七匹狼","year":1800}', "七匹狼")["year"] is None
    assert parse_target('{"company":"七匹狼","year":"2024"}', "七匹狼")["year"] == 2024


# ---------------------------------------------------------------- 调用

def test_fast_path_when_question_names_current_company():
    out = extract_target("精达股份2025年利润率", client=None, current_company="精达股份")
    assert out ["company"] == "精达股份" and out["year"] == 2025


def test_fast_path_is_skipped_for_comparison_questions():
    """“精达和七匹狼对比”不能被快路径当成只问精达。"""
    out = extract_target("精达和七匹狼对比", client=None, current_company="精达股份")
    assert out["company"] == ""       # 没有 client，又没走快路径 → 识别不出


def test_extract_target_uses_the_model():
    out = extract_target("七匹狼利润率变化如何",
                         client=client_returning('{"company":"七匹狼","year":null}'),
                         current_company="精达股份")
    assert out["company"] == "七匹狼" and out["year"] is None


def test_extract_target_falls_back_on_error():
    out = extract_target("七匹狼2024年利润率", client=client_returning(exc=TimeoutError()),
                         current_company="精达股份")
    assert out == {"company": "", "year": 2024, "multiple": False}


def test_multiple_companies_is_reported():
    out = extract_target("七匹狼和精达的毛利率对比",
                         client=client_returning(
                             '{"company":"七匹狼","year":null,"multiple":true}'),
                         current_company="精达股份")
    assert out["multiple"] is True
